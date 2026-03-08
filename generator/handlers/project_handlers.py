import json
import os
import shutil

import yaml
from flask import jsonify, render_template, request


def index(app_mod):
    return render_template("index.html", default_output_dir=app_mod.DEFAULT_OUTPUT_DIR)


def list_projects(app_mod):
    root = os.path.abspath(app_mod.DEFAULT_OUTPUT_DIR)
    os.makedirs(root, exist_ok=True)
    projects = []
    for name in app_mod._list_project_names():
        pdir = os.path.join(root, name)
        status_raw = app_mod._read_json_if_exists(os.path.join(pdir, "status.json"))
        status = status_raw if isinstance(status_raw, dict) else {}
        last_optimization_run = status.get("last_optimization_run")
        if not isinstance(last_optimization_run, dict):
            last_optimization_run = {}
        runs = app_mod._collect_project_runs(pdir)
        projects.append({
            "name": name,
            "path": pdir,
            "updated_at_utc": last_optimization_run.get("saved_at_utc"),
            "run_count": len(runs),
            "last_run": runs[0] if runs else None,
            "has_config": os.path.isfile(os.path.join(pdir, "config.yaml")),
        })
    return jsonify({"projects": projects, "root": root})


def create_project(app_mod):
    data = request.get_json(silent=True) or {}
    requested = (data.get("name") or "").strip()
    names = set(app_mod._list_project_names())
    if requested:
        if not app_mod._PROJECT_NAME_RE.match(requested):
            return jsonify({"error": "Invalid project name"}), 400
        if requested in names:
            return jsonify({"error": "Project with this name already exists"}), 409
        name = requested
    else:
        base = "New project"
        if base not in names:
            name = base
        else:
            i = 1
            while True:
                cand = f"{base} ({i})"
                if cand not in names:
                    name = cand
                    break
                i += 1
    pdir = app_mod._project_dir(name)
    os.makedirs(pdir, exist_ok=False)
    return jsonify({"name": name, "path": pdir})


def rename_project(app_mod):
    data = request.get_json(silent=True) or {}
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "old_name and new_name are required"}), 400
    if not app_mod._PROJECT_NAME_RE.match(new_name):
        return jsonify({"error": "Invalid new project name"}), 400
    try:
        old_dir = app_mod._project_dir(old_name)
        new_dir = app_mod._project_dir(new_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(old_dir):
        return jsonify({"error": "Project not found"}), 404
    if os.path.exists(new_dir):
        return jsonify({"error": "Project with this name already exists"}), 409
    os.rename(old_dir, new_dir)
    return jsonify({"old_name": old_name, "new_name": new_name, "path": new_dir})


def list_project_runs(app_mod):
    project_name = (request.args.get("project_name") or "").strip()
    if not project_name:
        return jsonify({"error": "project_name is required"}), 400
    try:
        pdir = app_mod._project_dir(project_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(pdir):
        return jsonify({"error": "Project not found"}), 404
    runs = app_mod._collect_project_runs(pdir)
    return jsonify({"project_name": project_name, "runs": runs})


def load_project_run(app_mod):
    data = request.get_json(silent=True) or {}
    project_name = (data.get("project_name") or "").strip()
    run_id = (data.get("run_id") or "").strip()
    if not project_name or not run_id:
        return jsonify({"error": "project_name and run_id are required"}), 400
    try:
        pdir = app_mod._project_dir(project_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(pdir):
        return jsonify({"error": "Project not found"}), 404
    try:
        payload = app_mod._load_run_outputs(pdir, run_id)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(payload)


def delete_project_run(app_mod):
    data = request.get_json(silent=True) or {}
    project_name = (data.get("project_name") or "").strip()
    run_id = (data.get("run_id") or "").strip()
    if not project_name or not run_id:
        return jsonify({"error": "project_name and run_id are required"}), 400
    try:
        pdir = app_mod._project_dir(project_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(pdir):
        return jsonify({"error": "Project not found"}), 404

    run_dir = os.path.join(pdir, "runs", run_id)
    if not os.path.isdir(run_dir):
        return jsonify({"error": f"Run not found: {run_id}"}), 404

    try:
        shutil.rmtree(run_dir)
    except Exception as exc:
        return jsonify({"error": f"Failed to delete run {run_id}: {exc}"}), 500

    runs = app_mod._collect_project_runs(pdir)
    status_payload = {
        "optimization_runs": runs,
        "last_optimization_run": runs[0] if runs else {},
    }
    app_mod._write_status_json(pdir, **status_payload)
    return jsonify({"project_name": project_name, "deleted_run_id": run_id, "runs": runs})


def open_project(app_mod):
    data = request.get_json(silent=True) or {}
    project_name = (data.get("project_name") or "").strip()
    if not project_name:
        return jsonify({"error": "project_name is required"}), 400
    try:
        pdir = app_mod._project_dir(project_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(pdir):
        return jsonify({"error": "Project not found"}), 404
    cfg = os.path.join(pdir, "config.yaml")
    runs = app_mod._collect_project_runs(pdir)
    return jsonify({
        "project_name": project_name,
        "project_path": pdir,
        "config_path": cfg if os.path.isfile(cfg) else "",
        "runs": runs,
        "latest_run_id": runs[0]["run_id"] if runs else None,
    })


def export(app_mod):
    if len(app_mod.store) == 0:
        return jsonify({"error": "No sites to export."})

    try:
        app_mod.store.validate_priorities()
    except ValueError as e:
        app_mod.logger.warning("Priority validation failed: %s", e)
        return jsonify({"error": str(e)})

    data = request.json
    try:
        output_dir = app_mod._resolve_project_output_dir(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    max_towers_per_route = int(data.get("max_towers_per_route", 8))
    os.makedirs(output_dir, exist_ok=True)

    sites = list(app_mod.store)

    sites_path = os.path.join(output_dir, "sites.geojson")
    boundary_path = os.path.join(output_dir, "boundary.geojson")
    roads_path = os.path.join(output_dir, "roads.geojson")
    app_mod.export_sites_geojson(sites, sites_path)
    app_mod.export_boundary_geojson(sites, boundary_path, roads_geojson=app_mod._roads_geojson)
    if app_mod._roads_geojson:
        app_mod.export_roads_geojson(app_mod._roads_geojson, roads_path)

    if app_mod._elevation_path and os.path.isfile(app_mod._elevation_path):
        import shutil

        elevation_dest = os.path.join(output_dir, "elevation.tif")
        if os.path.abspath(app_mod._elevation_path) != os.path.abspath(elevation_dest):
            shutil.copy2(app_mod._elevation_path, elevation_dest)
            app_mod.logger.info("Copied elevation to %s", elevation_dest)
        else:
            app_mod.logger.info("Elevation already at destination, skipping copy")

    if any(s.boundary_geojson for s in sites):
        city_boundaries_path = os.path.join(output_dir, "city_boundaries.geojson")
        app_mod.export_city_boundaries_geojson(sites, city_boundaries_path)

    req_params = data.get("parameters", {})
    req_params.setdefault("max_towers_per_route", max_towers_per_route)
    active_routes = data.get("active_routes", {})
    forced_waypoints = data.get("forced_waypoints", {})
    app_mod._active_mesh_parameters = dict(req_params)

    run_meta = app_mod._persist_current_calculation_outputs(
        output_dir,
        parameters=req_params,
        summary=(app_mod._opt_result or {}).get("summary", {}),
        source="manual_save",
    )

    app_mod._save_project_to_dir(
        output_dir,
        parameters=req_params,
        active_routes=active_routes,
        forced_waypoints=forced_waypoints,
        optimization_run=run_meta,
    )

    config_path = os.path.join(output_dir, "config.yaml")
    app_mod.logger.info("Exported %d sites to %s", len(sites), output_dir)
    return jsonify({
        "count": len(sites),
        "output_dir": output_dir,
        "config_path": config_path,
    })


def load_project(app_mod):
    data = request.json
    path = data.get("path", "").strip()

    if os.path.isdir(path):
        path = os.path.join(path, "config.yaml")
    if not os.path.isfile(path):
        app_mod.logger.error("Config file not found: %s", path)
        return jsonify({"error": f"File not found: {path}"})

    config_dir = os.path.dirname(os.path.abspath(path))

    with open(path) as f:
        config = yaml.safe_load(f)
    app_mod.logger.info("Loaded config from %s", path)

    inputs = config.get("inputs", {})
    outputs = config.get("outputs", {})
    config_parameters = config.get("parameters", {})
    if not isinstance(config_parameters, dict):
        config_parameters = {}

    def resolve(p):
        if not p:
            return None
        if os.path.isabs(p):
            return p
        candidate = os.path.join(config_dir, p)
        if os.path.exists(candidate):
            return candidate
        parent_candidate = os.path.join(os.path.dirname(config_dir), p)
        if os.path.exists(parent_candidate):
            return parent_candidate
        return candidate

    app_mod.store._sites.clear()
    app_mod._counter = 0
    sites_path = resolve(inputs.get("target_sites"))
    if sites_path and os.path.isfile(sites_path):
        with open(sites_path) as f:
            sites_data = json.load(f)
        for feat in sites_data.get("features", []):
            props = feat.get("properties", {})
            coords = feat["geometry"]["coordinates"]
            site = app_mod.SiteModel(
                name=props.get("name", f"Site_{app_mod._counter + 1}"),
                lat=coords[1],
                lon=coords[0],
                priority=props.get("priority", 1),
                site_height_m=float(props.get("site_height_m", 0.0) or 0.0),
                fetch_city=props.get("fetch_city", True),
                boundary_name=props.get("boundary_name", ""),
            )
            app_mod.store.add(site)
            app_mod._counter += 1
        app_mod.logger.info("Loaded %d sites from %s", len(app_mod.store), sites_path)

    layers = {}
    layer_files = {
        "roads": resolve(inputs.get("roads")),
        "boundary": resolve(inputs.get("boundary")),
        "towers": resolve(outputs.get("towers")),
        "edges": resolve(outputs.get("visibility_edges")),
        "city_boundaries": resolve(inputs.get("city_boundaries")),
        "grid_cells": os.path.join(config_dir, "grid_cells.geojson"),
        "grid_cells_full": os.path.join(config_dir, "grid_cells_full.geojson"),
        "gap_repair_hexes": os.path.join(config_dir, "gap_repair_hexes.geojson"),
    }
    for key, fpath in layer_files.items():
        if fpath and os.path.isfile(fpath):
            with open(fpath) as f:
                layers[key] = json.load(f)
            app_mod.logger.info("Loaded layer '%s' from %s", key, fpath)
        elif fpath:
            app_mod.logger.warning("Layer '%s' file not found: %s", key, fpath)

    app_mod._loaded_layers = layers
    app_mod._roads_geojson = layers.get("roads")
    app_mod._full_roads_geojson = layers.get("roads")
    app_mod._close_grid_provider()
    app_mod._grid_bundle_path = None
    app_mod._grid_provider_summary = ""

    app_mod._loaded_report = None
    report_path = resolve(outputs.get("report"))
    if report_path and os.path.isfile(report_path):
        with open(report_path) as f:
            app_mod._loaded_report = json.load(f)
        app_mod.logger.info("Loaded report from %s", report_path)

    app_mod._loaded_coverage = None
    coverage_path = resolve(outputs.get("coverage"))
    if coverage_path and os.path.isfile(coverage_path):
        with open(coverage_path) as f:
            app_mod._loaded_coverage = json.load(f)
        app_mod.logger.info(
            "Loaded coverage from %s (%d features)",
            coverage_path,
            len(app_mod._loaded_coverage.get("features", [])),
        )

    app_mod._runtime_tower_coverage = None

    elevation_file = resolve(inputs.get("elevation"))
    if elevation_file and os.path.isfile(elevation_file):
        app_mod._elevation_path = elevation_file
        app_mod.logger.info("Loaded elevation from %s", elevation_file)
    else:
        fallback_elevation = os.path.join(config_dir, "elevation.tif")
        if os.path.isfile(fallback_elevation):
            app_mod._elevation_path = fallback_elevation
            app_mod.logger.info("Loaded fallback elevation from %s", fallback_elevation)
        else:
            app_mod._elevation_path = None

    configured_grid_bundle = resolve(inputs.get("grid_bundle"))
    if app_mod._elevation_path and configured_grid_bundle and os.path.isfile(configured_grid_bundle):
        try:
            app_mod._hydrate_grid_provider(configured_grid_bundle, elevation_path=app_mod._elevation_path)
            app_mod.logger.info("Loaded grid bundle from config: %s", configured_grid_bundle)
        except Exception:
            app_mod.logger.warning(
                "Failed to load configured grid bundle: %s",
                configured_grid_bundle,
                exc_info=True,
            )

    output_dir = None
    for out_key in ("towers", "coverage", "report", "visibility_edges"):
        out_path = resolve(outputs.get(out_key))
        if out_path:
            output_dir = os.path.dirname(out_path)
            break
    if not output_dir:
        output_dir = config_dir

    project_status = {}
    status_path = os.path.join(config_dir, "status.json")
    if os.path.isfile(status_path):
        with open(status_path) as f:
            project_status = json.load(f)
        app_mod.logger.info("Loaded project status from %s", status_path)
        if not app_mod._elevation_path and project_status.get("elevation_path"):
            ep = project_status["elevation_path"]
            if os.path.isfile(ep):
                app_mod._elevation_path = ep
                app_mod.logger.info("Restored elevation path from status: %s", ep)
        if app_mod._grid_provider is None and project_status.get("grid_bundle_path"):
            gb = project_status["grid_bundle_path"]
            if gb and not os.path.isabs(gb):
                gb = os.path.join(config_dir, gb)
            if os.path.isfile(gb):
                try:
                    app_mod._hydrate_grid_provider(gb, elevation_path=app_mod._elevation_path)
                    app_mod.logger.info("Restored grid bundle from status: %s", gb)
                except Exception:
                    app_mod.logger.warning(
                        "Failed to restore grid bundle from status: %s",
                        gb,
                        exc_info=True,
                    )

    if app_mod._grid_provider is None and configured_grid_bundle and os.path.isfile(configured_grid_bundle):
        try:
            app_mod._hydrate_grid_provider(configured_grid_bundle, elevation_path=app_mod._elevation_path)
            app_mod.logger.info("Loaded grid bundle from config on retry: %s", configured_grid_bundle)
        except Exception:
            app_mod.logger.warning(
                "Failed to load configured grid bundle on retry: %s",
                configured_grid_bundle,
                exc_info=True,
            )

    if config_parameters:
        merged_params = {}
        if isinstance(project_status.get("parameters"), dict):
            merged_params.update(project_status["parameters"])
        merged_params.update(config_parameters)
        project_status["parameters"] = merged_params
        app_mod._active_mesh_parameters = dict(merged_params)
    else:
        app_mod._active_mesh_parameters = {}

    if app_mod._grid_provider is None and app_mod._elevation_path and layers.get("boundary"):
        try:
            rebuilt = app_mod._build_grid_bundle_for_current_state(config_dir)
            app_mod._hydrate_grid_provider(rebuilt["bundle_path"], elevation_path=app_mod._elevation_path)
            app_mod._grid_bundle_path = rebuilt["bundle_path"]
            app_mod._grid_provider_summary = rebuilt["summary"]
            project_status["has_grid_provider"] = True
            project_status["grid_bundle_path"] = app_mod._grid_bundle_path
            project_status["grid_provider_summary"] = app_mod._grid_provider_summary
            app_mod.logger.info("Rebuilt grid bundle for loaded project: %s", app_mod._grid_bundle_path)
        except Exception:
            app_mod.logger.warning("Could not build grid bundle for loaded project", exc_info=True)

    app_mod._p2p_routes = []
    app_mod._p2p_all_route_features = {}
    routes_file = os.path.join(config_dir, "routes.json")
    if os.path.isfile(routes_file):
        with open(routes_file) as f:
            routes_data = json.load(f)
        for r in routes_data.get("routes", []):
            meta = {k: v for k, v in r.items() if k != "features"}
            app_mod._p2p_routes.append(meta)
            app_mod._p2p_all_route_features[r["route_id"]] = r.get("features", [])
        app_mod.logger.info("Restored %d routes from %s", len(app_mod._p2p_routes), routes_file)
        project_status["has_routes"] = True

    # Normalize returned status flags to current in-memory truth so stale status.json
    # does not disable frontend actions after successful project load/hydration.
    project_status["has_elevation"] = bool(app_mod._elevation_path and os.path.isfile(app_mod._elevation_path))
    project_status["has_grid_provider"] = app_mod._grid_provider is not None
    project_status["has_routes"] = bool(app_mod._p2p_routes)
    project_status["grid_provider_summary"] = app_mod._grid_provider_summary or project_status.get("grid_provider_summary", "")

    bounds = app_mod._compute_bounds(layers, app_mod.store)
    project_name = None
    try:
        project_name = app_mod._project_name_from_dir(config_dir)
    except Exception:
        project_name = None
    runs = app_mod._collect_project_runs(config_dir) if project_name else []

    return jsonify({
        "config_path": os.path.abspath(path),
        "output_dir": output_dir,
        "project_name": project_name,
        "sites": app_mod.store.to_list(),
        "layers": layers,
        "bounds": bounds,
        "report": app_mod._loaded_report,
        "has_coverage": app_mod._loaded_coverage is not None,
        "has_elevation": app_mod._elevation_path is not None and os.path.isfile(app_mod._elevation_path),
        "has_grid_provider": app_mod._grid_provider is not None,
        "grid_provider_summary": app_mod._grid_provider_summary or "",
        "project_status": project_status,
        "routes": [
            dict(r, features=app_mod._p2p_all_route_features.get(r["route_id"], []))
            for r in app_mod._p2p_routes
        ],
        "active_routes": project_status.get("active_routes", {}),
        "forced_waypoints": project_status.get("forced_waypoints", {}),
        "runs": runs,
        "latest_run_id": runs[0]["run_id"] if runs else None,
    })
