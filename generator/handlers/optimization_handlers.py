import copy
import json
import os
import tempfile
import threading

from flask import Response, jsonify, request


def _probe_lat_lon_for_grid_provider(app_mod):
    for route in (app_mod._p2p_routes or []):
        for key in ("site1", "site2"):
            site = route.get(key) or {}
            lat = site.get("lat")
            lon = site.get("lon")
            try:
                return float(lat), float(lon)
            except (TypeError, ValueError):
                continue
    for site in app_mod.store:
        try:
            return float(site.lat), float(site.lon)
        except Exception:
            continue
    return None


def _grid_provider_is_usable(app_mod) -> bool:
    gp = app_mod._grid_provider
    if gp is None:
        return False
    probe = _probe_lat_lon_for_grid_provider(app_mod)
    if probe is None:
        return True
    lat, lon = probe
    probe_fn = None
    if callable(getattr(gp, "get_elevation_bilinear", None)):
        probe_fn = gp.get_elevation_bilinear
    elif callable(getattr(gp, "get_elevation", None)):
        probe_fn = gp.get_elevation
    if probe_fn is None:
        return True
    try:
        probe_fn(lat, lon)
        return True
    except Exception as exc:
        app_mod.logger.warning("Grid provider probe failed before optimization: %s", exc)
        return False


def _ensure_grid_provider_ready(app_mod) -> bool:
    if _grid_provider_is_usable(app_mod):
        return True
    bundle_path = app_mod._grid_bundle_path
    if not bundle_path or not os.path.isfile(bundle_path):
        return False
    try:
        app_mod._hydrate_grid_provider(bundle_path, elevation_path=app_mod._elevation_path)
    except Exception:
        app_mod.logger.warning("Failed to hydrate grid provider from bundle before optimization", exc_info=True)
        return False
    return _grid_provider_is_usable(app_mod)


def run_optimization(app_mod):
    """
    Run the mesh_calculator route pipeline on the currently selected routes.
    Launches the pipeline in a background thread and returns immediately.
    Results are streamed via /api/optimization-stream (SSE).
    """
    try:
        from mesh_calculator.core.config import MeshConfig, RouteSpec
        from mesh_calculator.optimization.route_pipeline import run_route_pipeline
    except ImportError as exc:
        return jsonify({
            "error": (
                "mesh_calculator is not installed in this Python environment. "
                f"Install it with: cd mesh_calculator && uv sync --group dev  ({exc})"
            )
        }), 500

    if app_mod._job_manager.is_running:
        return jsonify({"error": "Optimization already running."}), 409

    if not app_mod._p2p_routes:
        return jsonify({"error": "No routes found. Run Filter P2P first."}), 400

    if not app_mod._elevation_path or not os.path.isfile(app_mod._elevation_path):
        return jsonify({"error": "No elevation data. Download Elevation first."}), 400

    if not _ensure_grid_provider_ready(app_mod):
        return jsonify({"error": "Grid provider is not ready. Download elevation first."}), 400

    body = request.json or {}
    max_towers = int(body.get("max_towers_per_route", 8))
    param_overrides = app_mod._normalize_mesh_parameters(body.get("parameters", {}))
    app_mod._active_mesh_parameters = dict(param_overrides)
    persist_outputs = bool((body.get("project_name") or "").strip() or (body.get("output_dir") or "").strip())
    try:
        output_dir = app_mod._resolve_project_output_dir(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not persist_outputs:
        output_dir = None

    valid_fields = MeshConfig.__dataclass_fields__
    mesh_config = MeshConfig(**{k: v for k, v in param_overrides.items() if k in valid_fields})
    low_mast_warning = None
    if mesh_config.mast_height_m < app_mod._LOW_MAST_WARN_THRESHOLD_M:
        low_mast_warning = (
            f"Low mast height ({mesh_config.mast_height_m:.1f} m) strongly increases "
            "NLOS/disconnected outcomes. Consider raising mast height or max_towers_per_route."
        )
        app_mod.logger.warning(low_mast_warning)

    route_specs = []
    for r in app_mod._p2p_routes:
        feats = app_mod._p2p_all_route_features.get(r["route_id"], [])
        if not feats:
            continue
        route_specs.append(RouteSpec(
            route_id=r["route_id"],
            features=feats,
            site1=r.get("site1", {}),
            site2=r.get("site2", {}),
            max_towers_per_route=max_towers,
        ))

    if not route_specs:
        return jsonify({"error": "No route features available. Run Filter P2P and select routes first."}), 400

    city_boundaries_geojson = None
    city_features = [
        {"type": "Feature", "geometry": s.boundary_geojson, "properties": {"name": s.boundary_name}}
        for s in app_mod.store
        if s.boundary_geojson is not None
    ]
    if city_features:
        city_boundaries_geojson = {"type": "FeatureCollection", "features": city_features}

    app_mod._job_manager.prepare_new_job()
    if low_mast_warning:
        app_mod._job_manager.put(f"WARNING: {low_mast_warning}")

    app_mod._opt_result = {}
    app_mod._opt_cancel_requested = False

    def _run_pipeline():
        app_mod._job_manager.mark_running()
        app_mod._opt_running = True
        try:
            tmp_dir_dp = tempfile.mkdtemp(prefix="mesh_opt_dp_")

            app_mod.logger.info(
                "run_optimization: %d routes, max_towers=%d (DP)",
                len(route_specs), max_towers,
            )
            boundary_geojson = (app_mod._loaded_layers or {}).get("boundary")

            def _make_progress_callback():
                def _callback(event: dict):
                    if app_mod._job_manager.cancel_requested:
                        raise app_mod._OptimizationCanceled("Optimization canceled by user.")
                    if isinstance(event, dict):
                        app_mod._job_manager.put({"progress": dict(event)})
                return _callback

            def _run_one(out_dir):
                app_mod._thread_local.strategy_label = "dp"
                config_copy = copy.deepcopy(mesh_config)
                return run_route_pipeline(
                    routes=route_specs,
                    mesh_config=config_copy,
                    grid_provider=app_mod._grid_provider,
                    city_boundaries_geojson=city_boundaries_geojson,
                    boundary_geojson=boundary_geojson,
                    output_dir=out_dir,
                    progress_callback=_make_progress_callback(),
                )

            summary = _run_one(tmp_dir_dp)
            if app_mod._job_manager.cancel_requested:
                raise app_mod._OptimizationCanceled("Optimization canceled by user.")

            output_keys = [
                ("towers", "towers.geojson"),
                ("edges", "visibility_edges.geojson"),
                ("grid_cells", "grid_cells.geojson"),
                ("grid_cells_full", "grid_cells_full.geojson"),
                ("gap_repair_hexes", "gap_repair_hexes.geojson"),
            ]

            result = {"summary": summary}
            for key, fname in output_keys:
                fpath = os.path.join(tmp_dir_dp, fname)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        result[key] = json.load(f)
            report_path = os.path.join(tmp_dir_dp, "report.json")
            if os.path.isfile(report_path):
                with open(report_path) as f:
                    result["report"] = json.load(f)
            app_mod._job_manager.put({
                "progress": {
                    "stage": "done",
                    "step": (
                        f"Done • {summary.get('total_towers', 0)} towers • "
                        f"{summary.get('visibility_edges', 0)} links"
                    ),
                    "percent": 100.0,
                    "route_index": len(route_specs),
                    "route_total": len(route_specs),
                    "route_id": None,
                    "route_label": None,
                }
            })

            app_mod._loaded_layers.pop("coverage", None)
            app_mod._loaded_coverage = None
            for key, _ in output_keys:
                if key in result:
                    app_mod._loaded_layers[key] = result[key]
            app_mod._loaded_report = result.get("report")

            app_mod.logger.info(
                "run_optimization complete: DP=%d towers/%d edges",
                summary.get("total_towers", 0),
                summary.get("visibility_edges", 0),
            )

            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                run_meta = app_mod._persist_current_calculation_outputs(
                    output_dir,
                    parameters=param_overrides,
                    summary=summary,
                    source="optimization",
                )
                app_mod._save_project_to_dir(
                    output_dir,
                    parameters=param_overrides,
                    optimization_run=run_meta,
                )
                app_mod.logger.info("Saved optimization results to %s", output_dir)

            app_mod._runtime_tower_coverage = None

            app_mod._opt_result = result
            app_mod._job_manager.set_result(result)
            app_mod._job_manager.put({"done": True, "summary": summary})

        except app_mod._OptimizationCanceled as exc:
            app_mod.logger.info("run_optimization canceled")
            app_mod._job_manager.put({"canceled": True, "message": str(exc)})
        except Exception as exc:
            app_mod.logger.exception("run_optimization failed")
            app_mod._job_manager.put({"error": str(exc)})
        finally:
            app_mod._job_manager.mark_finished()
            app_mod._opt_running = False

    threading.Thread(target=_run_pipeline, daemon=True).start()
    return jsonify({"started": True, "warning": low_mast_warning})


def cancel_optimization(app_mod):
    """Request cooperative cancellation of the running optimization."""
    if not app_mod._job_manager.request_cancel():
        return jsonify({"error": "No optimization is running."}), 409
    app_mod._opt_cancel_requested = True
    app_mod.logger.info("Optimization cancel requested by user")
    return jsonify({"cancel_requested": True})


def get_optimization_result(app_mod):
    """Return the result from the last completed optimization run."""
    current_result = app_mod._job_manager.get_result()
    if not current_result:
        current_result = app_mod._opt_result
    if not current_result:
        return jsonify({"error": "No optimization result available."}), 404
    return jsonify(current_result)


def optimization_stream(app_mod):
    """SSE endpoint that streams optimization log lines to the browser."""
    return Response(
        app_mod._job_manager.iter_sse_events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
