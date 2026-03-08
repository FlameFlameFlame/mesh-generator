import functools
from datetime import datetime, timezone


def _inject_globals(app_mod):
    names = [
        "store",
        "_counter",
        "_loaded_layers",
        "_roads_geojson",
        "_full_roads_geojson",
        "_loaded_report",
        "_loaded_coverage",
        "_runtime_tower_coverage",
        "_elevation_path",
        "_grid_bundle_path",
        "_grid_provider",
        "_grid_provider_summary",
        "_p2p_routes",
        "_p2p_all_route_features",
        "_p2p_display_features",
        "_forced_waypoints",
        "_active_mesh_parameters",
        "_opt_result",
        "logger",
        "DEFAULT_OUTPUT_DIR",
        "_PROJECT_NAME_RE",
        "_close_grid_provider",
        "_resolve_project_output_dir",
        "_build_grid_bundle_for_current_state",
        "_hydrate_grid_provider",
        "_normalize_mesh_parameters",
        "_save_project_to_dir",
        "_project_dir",
        "_project_name_from_dir",
        "_collect_project_runs",
        "_get_cache_dir",
        "_write_status_json",
        "_compute_bounds",
        "_collect_coords",
        "_cells_in_bbox",
        "_grid_cells_to_geojson",
        "_CALC_LAYER_TO_FILENAME",
    ]
    for name in names:
        if hasattr(app_mod, name):
            globals()[name] = getattr(app_mod, name)


def _flush_globals(app_mod):
    names = [
        "store",
        "_counter",
        "_loaded_layers",
        "_roads_geojson",
        "_full_roads_geojson",
        "_loaded_report",
        "_loaded_coverage",
        "_runtime_tower_coverage",
        "_elevation_path",
        "_grid_bundle_path",
        "_grid_provider",
        "_grid_provider_summary",
        "_p2p_routes",
        "_p2p_all_route_features",
        "_p2p_display_features",
        "_forced_waypoints",
        "_active_mesh_parameters",
        "_opt_result",
    ]
    for name in names:
        if name in globals():
            setattr(app_mod, name, globals()[name])


def call(app_mod, fn_name, *args, **kwargs):
    _inject_globals(app_mod)
    try:
        fn = globals()[fn_name]
        return fn(*args, **kwargs)
    finally:
        _flush_globals(app_mod)

import json
import os

import h3
from flask import jsonify, request

from generator.app_context import get_app_context
from generator.models import SiteModel
from generator import graph as graph_mod
from generator.roads import fetch_roads_cached
from generator.elevation import fetch_and_write_elevation_cached, render_elevation_image


def _normalized_site_name(name: str) -> str:
    return str(name or "").strip().lower()


def _site_name_exists(name: str, exclude_idx: int | None = None) -> bool:
    target = _normalized_site_name(name)
    if not target:
        return False
    for i, site in enumerate(store):
        if exclude_idx is not None and i == exclude_idx:
            continue
        if _normalized_site_name(getattr(site, "name", "")) == target:
            return True
    return False

def get_sites():
    return jsonify(store.to_list())


def add_site():
    global _counter
    data = request.json
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "site name cannot be empty"}), 400
    if _site_name_exists(name):
        return jsonify({"error": f'site name "{name}" already exists'}), 400
    _counter += 1
    site = SiteModel(
        name=name,
        lat=data["lat"],
        lon=data["lon"],
        priority=data.get("priority", 1),
        site_height_m=float(data.get("site_height_m", 0.0) or 0.0),
        fetch_city=bool(data.get("fetch_city", True)),
    )
    store.add(site)
    logger.info("Added site %s at (%.4f, %.4f) priority=%d", site.name, site.lat, site.lon, site.priority)
    return jsonify(store.to_list())


def update_site(idx):
    data = request.json
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    site = store.get(idx)
    moved = False
    if "name" in data:
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"error": "site name cannot be empty"}), 400
        if _site_name_exists(name, exclude_idx=idx):
            return jsonify({"error": f'site name "{name}" already exists'}), 400
        site.name = name
    if "lat" in data:
        site.lat = float(data["lat"])
        moved = True
    if "lon" in data:
        site.lon = float(data["lon"])
        moved = True
    if "priority" in data:
        store.update_priority(idx, data["priority"])
    if "site_height_m" in data:
        site.site_height_m = float(data.get("site_height_m", 0.0) or 0.0)
    if "fetch_city" in data:
        site.fetch_city = bool(data["fetch_city"])
    if moved or not site.fetch_city:
        site.boundary_geojson = None
        site.boundary_name = ""
    logger.info(
        "Updated site %d: name=%s priority=%d lat=%.6f lon=%.6f",
        idx, site.name, site.priority, site.lat, site.lon,
    )
    return jsonify(store.to_list())


def delete_site(idx):
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    name = store.get(idx).name
    store.remove(idx)
    logger.info("Deleted site %d (%s)", idx, name)
    return jsonify(store.to_list())


def detect_city_boundary(idx):
    """Detect and store city/town boundary for a site."""
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "Invalid site index"}), 400

    site = store.get(idx)
    from generator.boundaries import detect_city

    result = detect_city(site.lat, site.lon)

    if not result:
        return jsonify({"found": False})

    site.boundary_geojson = result["geometry"]
    site.boundary_name = result["name"]

    logger.info("Detected city '%s' for site %s", result["name"], site.name)

    return jsonify({
        "found": True,
        "name": result["name"],
        "geometry": result["geometry"],
    })


def clear_project():
    """Clear in-memory project state (non-destructive; files on disk are preserved)."""
    global _counter, _roads_geojson, _full_roads_geojson, _loaded_layers, _loaded_report
    global _loaded_coverage, _runtime_tower_coverage, _elevation_path, _p2p_routes, _p2p_all_route_features
    global _p2p_display_features, _forced_waypoints, _grid_bundle_path, _grid_provider_summary
    global _active_mesh_parameters
    store._sites.clear()
    _counter = 0
    _roads_geojson = None
    _full_roads_geojson = None
    _loaded_layers = {}
    _loaded_report = None
    _loaded_coverage = None
    _runtime_tower_coverage = None
    _p2p_routes = []
    _p2p_all_route_features = {}
    _p2p_display_features = {}
    _forced_waypoints = {}
    _active_mesh_parameters = {}
    _elevation_path = None
    _close_grid_provider()
    _grid_bundle_path = None
    _grid_provider_summary = ""
    logger.info("Project cleared in-memory (no files deleted)")
    return jsonify({"ok": True})


def clear_calculations():
    """Clear loaded calculation layers from memory without deleting files from disk."""
    global _loaded_layers, _loaded_report, _loaded_coverage, _runtime_tower_coverage
    data = request.json or {}
    try:
        output_dir = _resolve_project_output_dir(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    _loaded_layers.pop("towers", None)
    _loaded_layers.pop("edges", None)
    _loaded_layers.pop("coverage", None)
    _loaded_report = None
    _loaded_coverage = None
    _runtime_tower_coverage = None
    logger.info("Cleared in-memory calculation layers (files preserved) for %s", output_dir)
    return jsonify({"cleared_map_only": True, "output_dir": output_dir})


def get_coverage():
    """Serve cached road coverage; build runtime fallback from loaded layers if needed."""
    global _loaded_coverage
    if _loaded_coverage is None:
        try:
            _loaded_coverage = _build_runtime_road_coverage_from_layers()
        except Exception:
            logger.warning("Failed to build runtime road coverage fallback", exc_info=True)
            _loaded_coverage = None
    if _loaded_coverage is None:
        return jsonify({"error": "No coverage data loaded"}), 404
    return jsonify(_loaded_coverage)


def get_tower_coverage():
    """Serve last runtime tower coverage GeoJSON."""
    if _runtime_tower_coverage is None:
        return jsonify({"error": "No tower coverage data calculated"}), 404
    return jsonify(_runtime_tower_coverage)


def _coverage_results_to_geojson(results: list[dict]) -> dict:
    features = []
    for rec in results:
        boundary = h3.cell_to_boundary(rec["h3_index"])
        coords = [[lon, lat] for lat, lon in boundary]
        coords.append(coords[0])
        props = {k: v for k, v in rec.items() if k not in ("lat", "lon")}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


def _build_runtime_road_coverage_from_layers() -> dict | None:
    """
    Build road coverage lazily from loaded towers + grid cells.

    This keeps road coverage available after pipeline-time coverage export removal.
    """
    grid_geojson = (_loaded_layers or {}).get("grid_cells")
    towers_geojson = (_loaded_layers or {}).get("towers")
    if not isinstance(grid_geojson, dict) or not isinstance(towers_geojson, dict):
        return None
    grid_features = grid_geojson.get("features") or []
    tower_features = towers_geojson.get("features") or []
    if not grid_features or not tower_features:
        return None

    try:
        from mesh_calculator.core.config import MeshConfig
        from mesh_calculator.core.grid import H3Cell
        from mesh_calculator.data.cache import LOSCache
        from mesh_calculator.network.graph import MeshSurface
    except ImportError:
        logger.warning("mesh_calculator imports unavailable for runtime coverage fallback")
        return None

    valid = MeshConfig.__dataclass_fields__
    cfg = MeshConfig(**{
        k: v for k, v in (_active_mesh_parameters or {}).items()
        if k in valid
    })

    cells = {}
    road_cells = set()
    for feat in grid_features:
        props = feat.get("properties") or {}
        h3_idx = props.get("h3_index")
        if not h3_idx:
            continue
        try:
            lat, lon = h3.cell_to_latlng(h3_idx)
        except Exception:
            continue
        elev = props.get("elevation")
        if elev is None and _grid_provider is not None:
            try:
                elev = float(_grid_provider.get_h3_cell_max_elevation(h3_idx))
            except Exception:
                elev = 0.0
        if elev is None:
            elev = 0.0
        has_road = bool(props.get("has_road", True))
        cell = H3Cell(
            h3_index=h3_idx,
            lat=float(lat),
            lon=float(lon),
            elevation=float(elev),
            has_road=has_road,
            is_in_boundary=True,
        )
        cells[h3_idx] = cell
        if has_road:
            road_cells.add(h3_idx)
    if not cells:
        return None

    surface = MeshSurface(cells, cfg, _grid_provider)
    for feat in tower_features:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        tower_h3 = props.get("h3_index")
        if not tower_h3 and len(coords) >= 2:
            try:
                tower_h3 = h3.latlng_to_cell(float(coords[1]), float(coords[0]), cfg.h3_resolution)
            except Exception:
                tower_h3 = None
        if not tower_h3:
            continue
        if tower_h3 not in surface.cells and _grid_provider is not None:
            try:
                materialized = _grid_provider.materialize_cells(
                    [tower_h3], cfg, road_cells=road_cells, is_in_boundary=True
                )
                surface.cells.update(materialized)
            except Exception:
                logger.debug("Could not materialize tower cell %s for coverage fallback", tower_h3, exc_info=True)
        if tower_h3 not in surface.cells:
            continue
        source = props.get("source", "site")
        tower = surface.place_tower(tower_h3, source=source)
        tower.coverage_radius_m = cfg.max_coverage_radius_m

    if not surface.towers:
        return None

    surface.compute_cell_coverage(LOSCache())

    features = []
    for h3_idx, cell in surface.cells.items():
        if not getattr(cell, "has_road", False):
            continue
        boundary = h3.cell_to_boundary(h3_idx)
        coords = [[lon, lat] for lat, lon in boundary]
        coords.append(coords[0])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {
                "h3_index": h3_idx,
                "elevation": float(cell.elevation),
                "has_road": bool(cell.has_road),
                "has_tower": bool(cell.has_tower),
                "visible_tower_count": int(cell.visible_tower_count),
                "distance_to_closest_tower": (
                    None
                    if cell.distance_to_closest_tower == float("inf")
                    else float(cell.distance_to_closest_tower)
                ),
                "clearance": (
                    float(cell.clearance)
                    if cell.clearance is not None and cell.clearance != float("inf")
                    else None
                ),
                "path_loss": (
                    float(cell.path_loss)
                    if cell.path_loss is not None
                    else None
                ),
                "received_power_dbm": (
                    float(cell.received_power_dbm)
                    if cell.received_power_dbm is not None
                    else None
                ),
                "is_covered": bool(cell.is_covered),
            },
        })

    if not features:
        return None

    logger.info(
        "Built runtime road coverage fallback: cells=%d towers=%d",
        len(features),
        len(surface.towers),
    )
    return {"type": "FeatureCollection", "features": features}


def _persist_current_calculation_outputs(
    output_dir: str,
    *,
    parameters: dict | None = None,
    summary: dict | None = None,
    source: str = "manual_save",
) -> dict | None:
    """
    Persist currently loaded calculation outputs and archive them as a run snapshot.

    Returns:
        Optimization run metadata dict if outputs existed and were persisted, else None.
    """
    os.makedirs(output_dir, exist_ok=True)

    payloads = {}
    for layer_key, fname in _CALC_LAYER_TO_FILENAME.items():
        data = (_loaded_layers or {}).get(layer_key)
        if data is not None:
            payloads[fname] = data

    if _loaded_report is not None:
        payloads["report.json"] = _loaded_report

    if not payloads:
        return None

    # Persist latest outputs at project root for compatibility/open-by-default.
    for fname, data in payloads.items():
        with open(os.path.join(output_dir, fname), "w") as f:
            json.dump(data, f, indent=2)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = os.path.join(output_dir, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    for fname, data in payloads.items():
        with open(os.path.join(run_dir, fname), "w") as f:
            json.dump(data, f, indent=2)

    run_settings = {
        "run_id": run_id,
        "source": source,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "parameters": dict(parameters or {}),
        "summary": dict(summary or {}),
        "files": sorted(payloads.keys()),
    }
    with open(os.path.join(run_dir, "run_settings.json"), "w") as f:
        json.dump(run_settings, f, indent=2)

    return {
        "run_id": run_id,
        "source": source,
        "saved_at_utc": run_settings["saved_at_utc"],
        "parameters": run_settings["parameters"],
        "summary": run_settings["summary"],
        "run_dir": run_dir,
        "files": run_settings["files"],
    }


def _parse_coverage_sources(payload: list, mesh_config, grid_provider=None) -> list:
    from mesh_calculator.network.tower_coverage import CoverageSource

    sources = []
    for idx, raw in enumerate(payload):
        if not isinstance(raw, dict):
            raise ValueError("Each source must be an object")

        source_id = raw.get("source_id")
        h3_index = raw.get("h3_index")
        lat = raw.get("lat")
        lon = raw.get("lon")

        # If lat/lon is provided, snap using adaptive provider when available.
        if lat is not None and lon is not None:
            lat = float(lat)
            lon = float(lon)
            if grid_provider is not None and callable(
                getattr(type(grid_provider), "locate_adaptive_cell", None)
            ):
                h3_index = grid_provider.locate_adaptive_cell(
                    lat,
                    lon,
                    mesh_config.h3_resolution,
                    mesh_config,
                )
            else:
                h3_index = h3.latlng_to_cell(lat, lon, mesh_config.h3_resolution)
        else:
            if h3_index is None:
                raise ValueError("Each source requires h3_index or lat/lon")
            if grid_provider is not None and callable(
                getattr(type(grid_provider), "locate_adaptive_cell", None)
            ):
                lat0, lon0 = h3.cell_to_latlng(h3_index)
                h3_index = grid_provider.locate_adaptive_cell(
                    float(lat0),
                    float(lon0),
                    mesh_config.h3_resolution,
                    mesh_config,
                )
            lat, lon = h3.cell_to_latlng(h3_index)

        if source_id is None:
            source_id = raw.get("tower_id")
        if source_id is None:
            source_id = f"source_{idx}"

        sources.append(CoverageSource(
            source_id=source_id,
            h3_index=h3_index,
            lat=lat,
            lon=lon,
        ))
    return sources


def _run_runtime_tower_coverage(sources_payload: list, body: dict):
    global _runtime_tower_coverage

    if not _elevation_path or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data. Download Elevation first."}), 400
    if _grid_provider is None:
        return jsonify({"error": "Grid provider is not ready. Download data/elevation first."}), 400

    try:
        from mesh_calculator.core.config import MeshConfig
        from mesh_calculator.network.tower_coverage import compute_h3_tower_coverage
    except ImportError as exc:
        return jsonify({"error": f"mesh_calculator import failed: {exc}"}), 500

    params = body.get("parameters", {}) or {}
    mesh_config = MeshConfig(**{
        k: v for k, v in params.items()
        if k in MeshConfig.__dataclass_fields__
    })
    coverage_h3_resolution = body.get("coverage_h3_resolution")
    if coverage_h3_resolution is not None:
        try:
            coverage_h3_resolution = int(coverage_h3_resolution)
        except (TypeError, ValueError):
            return jsonify({"error": "coverage_h3_resolution must be an integer"}), 400
        if coverage_h3_resolution < 6 or coverage_h3_resolution > 9:
            return jsonify({"error": "coverage_h3_resolution must be between 6 and 9"}), 400
        mesh_config.h3_resolution = coverage_h3_resolution

    try:
        sources = _parse_coverage_sources(sources_payload, mesh_config, grid_provider=_grid_provider)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    max_radius_m = body.get("max_radius_m")
    if max_radius_m is not None:
        try:
            max_radius_m = float(max_radius_m)
        except (TypeError, ValueError):
            return jsonify({"error": "max_radius_m must be numeric"}), 400

    results = compute_h3_tower_coverage(
        sources=sources,
        base_cells={},
        config=mesh_config,
        grid_provider=_grid_provider,
        los_cache=None,
        max_radius_m=max_radius_m,
    )

    geojson = _coverage_results_to_geojson(results)
    _runtime_tower_coverage = geojson
    covered_count = sum(
        1 for f in geojson.get("features", [])
        if (f.get("properties") or {}).get("is_covered")
    )
    total_count = len(geojson.get("features", []))
    by_res = {}
    for f in geojson.get("features", []):
        idx = ((f.get("properties") or {}).get("h3_index"))
        if not idx:
            continue
        try:
            res = int(h3.get_resolution(idx))
        except Exception:
            continue
        by_res[res] = by_res.get(res, 0) + 1
    return jsonify({
        "coverage": geojson,
        "source_count": len(sources),
        "feature_count": total_count,
        "radius_cell_count": total_count,
        "covered_count": covered_count,
        "uncovered_count": max(total_count - covered_count, 0),
        "cells_by_resolution": dict(sorted(by_res.items())),
        "h3_resolution": mesh_config.h3_resolution,
        "coverage_h3_resolution": mesh_config.h3_resolution,
        "max_radius_m": max_radius_m if max_radius_m is not None else mesh_config.max_coverage_radius_m,
    })


def calculate_tower_coverage_single():
    body = request.json or {}
    source = body.get("source")
    sources = body.get("sources")
    if source is not None and sources is None:
        sources = [source]
    if not isinstance(sources, list) or not sources:
        return jsonify({"error": "Provide source or non-empty sources list"}), 400
    return _run_runtime_tower_coverage(sources, body)


def calculate_tower_coverage_batch():
    body = request.json or {}
    sources = body.get("sources")
    if not isinstance(sources, list) or not sources:
        return jsonify({"error": "Provide non-empty sources list"}), 400
    return _run_runtime_tower_coverage(sources, body)


def download_elevation():
    """Download SRTM elevation tiles for the site bounding box."""
    import tempfile
    global _elevation_path, _grid_bundle_path, _grid_provider, _grid_provider_summary
    if len(store) < 2:
        return jsonify({"error": "Need at least 2 sites."})

    payload = request.get_json(silent=True) or {}
    user_bbox = payload.get("bbox")
    if user_bbox and isinstance(user_bbox, list) and len(user_bbox) == 2:
        south = float(user_bbox[0][0])
        west = float(user_bbox[0][1])
        north = float(user_bbox[1][0])
        east = float(user_bbox[1][1])
        logger.info(
            "Elevation: using user-drawn bbox S=%.4f W=%.4f N=%.4f E=%.4f",
            south, west, north, east,
        )
    else:
        sites = list(store)
        lats = [s.lat for s in sites]
        lons = [s.lon for s in sites]
        buffer = 0.15
        south, north = min(lats) - buffer, max(lats) + buffer
        west, east = min(lons) - buffer, max(lons) + buffer
        logger.info(
            "Elevation: auto bbox S=%.4f W=%.4f N=%.4f E=%.4f",
            south, west, north, east,
        )

    try:
        fd, path = tempfile.mkstemp(suffix=".tif", prefix="elevation_")
        os.close(fd)
        output_dir_for_cache = _resolve_project_output_dir(payload)
        os.makedirs(output_dir_for_cache, exist_ok=True)
        fetch_and_write_elevation_cached(
            south, west, north, east, path,
            cache_dir=_get_cache_dir(output_dir_for_cache),
        )
        _elevation_path = path
        size_mb = os.path.getsize(path) / (1024 * 1024)
        from generator.elevation import _tiles_for_bbox
        tile_count = len(_tiles_for_bbox(south, west, north, east))
        logger.info("Downloaded elevation: %d tiles, %.1f MB -> %s", tile_count, size_mb, path)
        grid_info = _build_grid_bundle_for_current_state(
            output_dir_for_cache,
            elevation_path=_elevation_path,
            boundary_geojson=(_loaded_layers or {}).get("boundary"),
            roads_geojson=_full_roads_geojson or _roads_geojson or (_loaded_layers or {}).get("roads"),
        )
        _hydrate_grid_provider(grid_info["bundle_path"], elevation_path=_elevation_path)
        # _hydrate_grid_provider is owned by generator.app and mutates app-module globals.
        # Sync local handler globals before call() flushes them back to app.
        app_globals = getattr(_hydrate_grid_provider, "__globals__", {})
        _grid_provider = app_globals.get("_grid_provider", _grid_provider)
        _grid_bundle_path = grid_info["bundle_path"]
        _grid_provider_summary = grid_info["summary"]
        _write_status_json(
            output_dir_for_cache,
            has_elevation=True,
            has_grid_provider=True,
            grid_bundle_path=_grid_bundle_path,
            grid_provider_summary=_grid_provider_summary,
        )
        return jsonify({
            "tiles": tile_count,
            "size_mb": round(size_mb, 1),
            "path": path,
            "grid_provider_ready": True,
            "grid_provider": {
                "bundle_path": _grid_bundle_path,
                "resolutions": grid_info["resolutions"],
                "summary": _grid_provider_summary,
            },
        })
    except Exception as e:
        logger.error("Failed to download elevation: %s", e)
        return jsonify({"error": f"Failed to download elevation: {e}"})


def get_elevation_image():
    """Return a colorized PNG of the elevation data as base64 + bounds."""
    import base64
    if _elevation_path is None or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data available"}), 404
    try:
        png_bytes, metadata = render_elevation_image(_elevation_path)
        return jsonify({
            "image": base64.b64encode(png_bytes).decode("ascii"),
            **metadata,
        })
    except Exception as e:
        logger.error("Failed to render elevation image: %s", e)
        return jsonify({"error": f"Failed to render elevation: {e}"}), 500


def get_grid_layers():
    """Return adaptive grid layers built from the active GridProvider."""
    if _grid_provider is None:
        return jsonify({"error": "Grid provider is not ready. Download elevation first."}), 400

    body = request.json or {}
    params = body.get("parameters") or {}
    viewport = body.get("viewport") if isinstance(body.get("viewport"), dict) else None
    include_full = bool(body.get("include_full", True))
    try:
        max_cells = int(body.get("max_cells", 12000))
    except Exception:
        max_cells = 12000
    max_cells = max(1000, min(max_cells, 50000))
    try:
        from mesh_calculator.core.config import MeshConfig
    except Exception:
        logger.exception("mesh_calculator import failed in /api/grid-layers")
        return jsonify({"error": "mesh_calculator import failed"}), 500

    valid = MeshConfig.__dataclass_fields__
    cfg = MeshConfig(**{k: v for k, v in params.items() if k in valid})
    base_res = int(params.get("h3_resolution", cfg.h3_resolution))
    cfg.h3_resolution = base_res

    summary = _grid_provider.adaptive_resolution_summary(base_res, cfg)
    road_cells_all = _grid_provider.get_adaptive_road_cells(base_res, cfg)
    full_cells_all = _grid_provider.get_adaptive_full_cells(base_res, cfg)
    road_cells = _cells_in_bbox(road_cells_all, viewport, max_cells=max_cells)
    full_cells = (
        _cells_in_bbox(full_cells_all, viewport, max_cells=max_cells)
        if include_full
        else set()
    )
    all_cells = set(full_cells) | set(road_cells)
    metadata_by_cell = {}
    for h3_idx in all_cells:
        meta = _grid_provider.get_adaptive_cell_metadata(h3_idx, base_res, cfg)
        meta["elevation"] = float(_grid_provider.get_h3_cell_max_elevation(h3_idx))
        metadata_by_cell[h3_idx] = meta

    grid_cells = _grid_cells_to_geojson(
        road_cells,
        road_cells=road_cells,
        metadata_by_cell=metadata_by_cell,
        base_resolution=base_res,
        effective_min=summary.get("effective_h3_resolution_min"),
        effective_max=summary.get("effective_h3_resolution_max"),
    )
    grid_cells_full = _grid_cells_to_geojson(
        full_cells,
        road_cells=road_cells,
        metadata_by_cell=metadata_by_cell,
        base_resolution=base_res,
        effective_min=summary.get("effective_h3_resolution_min"),
        effective_max=summary.get("effective_h3_resolution_max"),
    )
    return jsonify({
        "layers": {
            "grid_cells": grid_cells,
            "grid_cells_full": grid_cells_full,
        },
        "summary": summary,
        "viewport_filtered": bool(viewport is not None),
        "include_full": include_full,
        "grid_cells_count": len(grid_cells.get("features", [])),
        "grid_cells_full_count": len(grid_cells_full.get("features", [])),
    })


def path_profile():
    """Sample terrain elevation along a P2P route for a path-profile chart.

    Accepts route_id (from filter-p2p routes). Chains the route's road
    LineString features into an ordered polyline, then samples elevation
    every ~200 m along it. Returns points with dist_m, elevation_m, lat, lon
    so the frontend can map hover positions back to the map.
    """
    import math

    if _grid_provider is None:
        return jsonify({"error": "Grid provider is not ready. Download data/elevation first."}), 400

    body = request.json or {}
    route_id = body.get("route_id")
    if not route_id:
        return jsonify({"error": "route_id required"}), 400

    # Look up the route metadata and its features
    route_meta = next((r for r in _p2p_routes if r["route_id"] == route_id), None)
    if route_meta is None:
        return jsonify({"error": f"Route '{route_id}' not found. Run Filter P2P first."}), 400

    features = _p2p_all_route_features.get(route_id, [])
    if not features:
        return jsonify({"error": f"No road features for route '{route_id}'."}), 400

    try:
        from mesh_calculator.core.elevation import ElevationProvider
        elev_provider = ElevationProvider(_elevation_path)
    except Exception as e:
        return jsonify({"error": f"Could not open elevation data: {e}"}), 500

    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return 2 * R * math.asin(math.sqrt(a))

    # ── Chain LineString features into one ordered polyline ──────────────
    # Each feature geometry may be LineString or MultiLineString.
    # Strategy: greedily connect segments end-to-end (flip if needed).
    def _coords_of(feat):
        geom = feat.get("geometry", {})
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if gtype == "LineString":
            return [coords]
        if gtype == "MultiLineString":
            return coords
        return []

    # Collect all segment coordinate lists [[lon,lat], ...]
    segments = []
    for feat in features:
        for seg in _coords_of(feat):
            if len(seg) >= 2:
                segments.append(seg)

    if not segments:
        return jsonify({"error": "Route has no geometry."}), 400

    # Greedily chain segments: pick the segment whose start or end is closest
    # to the current chain tail and append (flipping if necessary).
    chain = list(segments[0])  # [lon, lat] list
    remaining = segments[1:]
    while remaining:
        tail = chain[-1]
        best_idx, best_dist, best_flip = 0, float("inf"), False
        for i, seg in enumerate(remaining):
            d_start = _haversine(tail[1], tail[0], seg[0][1], seg[0][0])
            d_end = _haversine(tail[1], tail[0], seg[-1][1], seg[-1][0])
            if d_start < best_dist:
                best_dist, best_idx, best_flip = d_start, i, False
            if d_end < best_dist:
                best_dist, best_idx, best_flip = d_end, i, True
        seg = remaining.pop(best_idx)
        if best_flip:
            seg = list(reversed(seg))
        # Skip duplicate first point if it matches the tail
        start = 1 if _haversine(tail[1], tail[0], seg[0][1], seg[0][0]) < 10 else 0
        chain.extend(seg[start:])

    # ── Ensure chain runs s1→s2 (not reversed) ───────────────────────────
    # The greedy assembler connects segments by proximity, which can produce a
    # chain that runs from s2 to s1.  Compare chain endpoints to the route
    # site coordinates and flip if necessary.
    s1 = route_meta["site1"]
    s2 = route_meta["site2"]

    def _dist_to_site(chain_pt, site):
        return _haversine(chain_pt[1], chain_pt[0], site["lat"], site["lon"])

    if _dist_to_site(chain[-1], s1) < _dist_to_site(chain[0], s1):
        chain = list(reversed(chain))
        logger.debug(
            "path_profile: reversed chain to match s1→s2 direction",
        )

    # ── Resample every ~200 m along the chain ────────────────────────────
    # First compute cumulative distances at each chain vertex.
    cum_dists = [0.0]
    for i in range(1, len(chain)):
        lon0, lat0 = chain[i - 1]
        lon1, lat1 = chain[i]
        cum_dists.append(cum_dists[-1] + _haversine(lat0, lon0, lat1, lon1))

    total_dist_m = cum_dists[-1]
    sample_interval = 200.0  # metres between samples
    n_samples = max(2, int(total_dist_m / sample_interval))
    points = []

    def _interp_at(dist):
        """Interpolate (lat, lon) at cumulative distance dist along chain."""
        if dist <= 0:
            return chain[0][1], chain[0][0]
        if dist >= total_dist_m:
            return chain[-1][1], chain[-1][0]
        for i in range(1, len(cum_dists)):
            if cum_dists[i] >= dist:
                seg_len = cum_dists[i] - cum_dists[i - 1]
                t = (dist - cum_dists[i - 1]) / seg_len if seg_len > 0 else 0
                lon = chain[i - 1][0] + t * (chain[i][0] - chain[i - 1][0])
                lat = chain[i - 1][1] + t * (chain[i][1] - chain[i - 1][1])
                return lat, lon
        return chain[-1][1], chain[-1][0]

    for i in range(n_samples + 1):
        d = i / n_samples * total_dist_m
        lat, lon = _interp_at(d)
        elev = elev_provider.get_elevation(lat, lon)
        points.append({
            "dist_m": round(d, 1),
            "elevation_m": round(elev, 1),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        })

    # Snap endpoints to the road polyline start/end (not city-center coords)
    road_start_lon, road_start_lat = chain[0]
    road_end_lon, road_end_lat = chain[-1]
    s1_elev = elev_provider.get_elevation(road_start_lat, road_start_lon)
    s2_elev = elev_provider.get_elevation(road_end_lat, road_end_lon)

    logger.info(
        "Path profile (route): %s → %s via %s, dist=%.1f m, %d samples",
        s1["name"], s2["name"], route_id, total_dist_m, n_samples,
    )
    return jsonify({
        "distance_m": round(total_dist_m, 1),
        "points": points,
        "route_id": route_id,
        "site1": {
            "name": s1["name"],
            "lat": round(road_start_lat, 6),
            "lon": round(road_start_lon, 6),
            "dist_m": 0,
            "elevation_m": round(s1_elev, 1),
        },
        "site2": {
            "name": s2["name"],
            "lat": round(road_end_lat, 6),
            "lon": round(road_end_lon, 6),
            "dist_m": round(total_dist_m, 1),
            "elevation_m": round(s2_elev, 1),
        },
    })


def link_analysis():
    """Return terrain profile along a straight line between two tower endpoints.

    Accepts: source_lat, source_lon, target_lat, target_lon, clearance_m,
             source_label, target_label, source_height_m/target_height_m
             (or legacy mast_height_m fallback).
    Returns: points [{dist_m, elevation_m, lat, lon}], tower1/tower2 info,
             distance_m, clearance_m.
    """
    import math

    if not _elevation_path or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data. Download Elevation first."}), 400

    body = request.json or {}
    lat1 = body.get("source_lat")
    lon1 = body.get("source_lon")
    lat2 = body.get("target_lat")
    lon2 = body.get("target_lon")
    source_h3 = body.get("source_h3")
    target_h3 = body.get("target_h3")
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return jsonify({"error": "source_lat/lon and target_lat/lon required"}), 400

    clearance_m = body.get("clearance_m")
    mast_height_m = float(body.get("mast_height_m", 5) or 5)
    source_height_m = float(body.get("source_height_m", mast_height_m) or mast_height_m)
    target_height_m = float(body.get("target_height_m", mast_height_m) or mast_height_m)
    label1 = body.get("source_label", "Tower A")
    label2 = body.get("target_label", "Tower B")

    try:
        from mesh_calculator.core.elevation import ElevationProvider
        elev_provider = ElevationProvider(_elevation_path)
    except Exception as e:
        return jsonify({"error": f"Could not open elevation data: {e}"}), 500

    def _hav(a_lat, a_lon, b_lat, b_lon):
        R = 6371000.0
        phi1, phi2 = math.radians(a_lat), math.radians(b_lat)
        dphi = math.radians(b_lat - a_lat)
        dlam = math.radians(b_lon - a_lon)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return 2 * R * math.asin(math.sqrt(a))

    def _parse_float(value):
        try:
            if value is None:
                return None
            v = float(value)
            return v if math.isfinite(v) else None
        except Exception:
            return None

    total_dist_m = _hav(lat1, lon1, lat2, lon2)
    sample_interval = 100.0
    n_samples = max(2, int(total_dist_m / sample_interval))

    points = []
    for i in range(n_samples + 1):
        frac = i / n_samples
        lat = lat1 + frac * (lat2 - lat1)
        lon = lon1 + frac * (lon2 - lon1)
        elev = elev_provider.get_elevation(lat, lon)
        points.append({
            "dist_m": round(frac * total_dist_m, 1),
            "elevation_m": round(elev, 1),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        })

    source_cell_elev = _parse_float(body.get("source_elevation_m"))
    target_cell_elev = _parse_float(body.get("target_elevation_m"))
    if source_cell_elev is None and source_h3 and callable(
        getattr(type(elev_provider), "get_h3_cell_max_elevation", None)
    ):
        try:
            source_cell_elev = float(elev_provider.get_h3_cell_max_elevation(source_h3))
        except Exception:
            source_cell_elev = None
    if target_cell_elev is None and target_h3 and callable(
        getattr(type(elev_provider), "get_h3_cell_max_elevation", None)
    ):
        try:
            target_cell_elev = float(elev_provider.get_h3_cell_max_elevation(target_h3))
        except Exception:
            target_cell_elev = None
    if source_cell_elev is None:
        source_cell_elev = float(elev_provider.get_elevation(lat1, lon1))
    if target_cell_elev is None:
        target_cell_elev = float(elev_provider.get_elevation(lat2, lon2))

    return jsonify({
        "distance_m": round(total_dist_m, 1),
        "clearance_m": clearance_m,
        "mast_height_m": mast_height_m,
        "source_height_m": source_height_m,
        "target_height_m": target_height_m,
        "points": points,
        "tower1": {
            "label": label1,
            "lat": round(lat1, 6),
            "lon": round(lon1, 6),
            "dist_m": 0,
            "elevation_m": round(source_cell_elev, 1),
        },
        "tower2": {
            "label": label2,
            "lat": round(lat2, 6),
            "lon": round(lon2, 6),
            "dist_m": round(total_dist_m, 1),
            "elevation_m": round(target_cell_elev, 1),
        },
    })


def generate():
    """Compute boundary from sites, fetch roads from OSM, return layers for visualization."""
    global _roads_geojson, _full_roads_geojson, _loaded_layers
    if len(store) < 2:
        return jsonify({"error": "Need at least 2 sites."})

    # Compute bounding box with buffer for road fetching
    sites = list(store)

    # Auto-detect city boundaries for sites that don't have one yet
    from generator.boundaries import detect_city as _detect_city
    newly_detected = []
    for site in sites:
        if site.boundary_geojson is None and site.fetch_city:
            try:
                result = _detect_city(site.lat, site.lon)
                if result:
                    site.boundary_geojson = result["geometry"]
                    site.boundary_name = result["name"]
                    newly_detected.append(site.name)
                    logger.info(
                        "Auto-detected city '%s' for site %s",
                        result["name"], site.name)
            except Exception as e:
                logger.warning(
                    "City auto-detection failed for site %s: %s",
                    site.name, e)

    # Use user-drawn bbox if provided, otherwise auto-compute from sites
    payload = request.get_json(silent=True) or {}
    user_bbox = payload.get("bbox")  # [[south,west],[north,east]] or None
    if (user_bbox and isinstance(user_bbox, list) and len(user_bbox) == 2):
        south = float(user_bbox[0][0])
        west = float(user_bbox[0][1])
        north = float(user_bbox[1][0])
        east = float(user_bbox[1][1])
        logger.info(
            "Using user-drawn bbox: S=%.4f W=%.4f N=%.4f E=%.4f",
            south, west, north, east,
        )
    else:
        lats = [s.lat for s in sites]
        lons = [s.lon for s in sites]
        buffer = 0.15  # ~16 km buffer around sites
        south, north = min(lats) - buffer, max(lats) + buffer
        west, east = min(lons) - buffer, max(lons) + buffer
        logger.info(
            "Auto bbox (%.2f° buffer): S=%.4f W=%.4f N=%.4f E=%.4f",
            buffer, south, west, north, east,
        )

    # Validate that every site (or its full city boundary) lies inside the bbox
    if user_bbox:
        from shapely.geometry import box as _box, Point as _Point, shape as _shape
        bbox_poly = _box(west, south, east, north)
        outside = []
        for site in sites:
            if site.boundary_geojson:
                try:
                    site_geom = _shape(site.boundary_geojson)
                    if not bbox_poly.contains(site_geom):
                        outside.append(
                            site.name
                            + (f" [{site.boundary_name}]"
                               if site.boundary_name else "")
                        )
                except Exception:
                    pass  # malformed boundary — skip validation for this site
            else:
                pt = _Point(site.lon, site.lat)
                if not bbox_poly.contains(pt):
                    outside.append(site.name)
        if outside:
            return jsonify({
                "error": (
                    "The following sites or their city boundaries are outside"
                    " the drawn bounding box: "
                    + ", ".join(outside)
                    + ". Please redraw the bounding box to include them,"
                    " or clear the bounding box to use the automatic area."
                )
            })

    output_dir_for_cache = _resolve_project_output_dir(payload)
    os.makedirs(output_dir_for_cache, exist_ok=True)
    try:
        roads = fetch_roads_cached(
            south, west, north, east,
            cache_dir=_get_cache_dir(output_dir_for_cache),
        )
    except Exception as e:
        logger.error("Failed to fetch roads: %s", e)
        return jsonify({"error": f"Failed to fetch roads: {e}"})

    logger.info("Fetched: %d road features", len(roads.get("features", [])))

    # Exclude road segments that lie entirely within city boundaries
    from shapely.geometry import shape as _shape_geom
    from shapely.ops import unary_union as _unary_union
    city_polygons = []
    for site in sites:
        if site.boundary_geojson:
            try:
                city_polygons.append(_shape_geom(site.boundary_geojson))
            except Exception:
                pass
    if city_polygons:
        exclusion_zone = _unary_union(city_polygons)
        original_count = len(roads.get("features", []))
        filtered_features = []
        for feat in roads.get("features", []):
            try:
                geom = _shape_geom(feat["geometry"])
                if not exclusion_zone.contains(geom):
                    filtered_features.append(feat)
            except Exception:
                filtered_features.append(feat)
        roads = {"type": "FeatureCollection", "features": filtered_features}
        excluded = original_count - len(filtered_features)
        logger.info(
            "Excluded %d road segments within city boundaries (kept %d)",
            excluded, len(filtered_features))

    _roads_geojson = roads
    _full_roads_geojson = roads
    logger.info("Generated: %d road features", len(roads.get("features", [])))

    # Build boundary from the road fetch bbox (encompasses all roads)
    from shapely.geometry import box, mapping
    boundary_poly = box(west, south, east, north)
    boundary_geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": mapping(boundary_poly),
            "properties": {},
        }],
    }

    layers = {"roads": roads, "boundary": boundary_geojson}
    _loaded_layers = layers

    bounds = _compute_bounds(layers, store)

    # Include all detected city boundaries in response
    city_boundaries = [
        {
            "name": s.name,
            "boundary_name": s.boundary_name,
            "geometry": s.boundary_geojson,
        }
        for s in sites
        if s.boundary_geojson is not None
    ]

    _write_status_json(output_dir_for_cache, has_roads=True)

    return jsonify({
        "road_count": len(roads.get("features", [])),
        "layers": layers,
        "bounds": bounds,
        "city_boundaries": city_boundaries,
    })


def filter_p2p():
    """Filter roads to named routes that connect site pairs."""
    global _roads_geojson, _loaded_layers, _p2p_routes, _p2p_all_route_features
    global _p2p_display_features, _forced_waypoints
    _forced_waypoints = {}

    from generator.graph import find_p2p_roads
    from math import atan2, cos, radians, sin, sqrt

    if not _roads_geojson:
        return jsonify({"error": "No roads loaded. Run Generate first."})

    sites = list(store)
    if len(sites) < 2:
        return jsonify({"error": "Need at least 2 sites."})

    original_count = len(_roads_geojson.get("features", []))

    # ── Build site pairs (priority hierarchy) ────────────────────────
    def _dist(s1, s2):
        R = 6_371_000
        la1, la2 = radians(s1.lat), radians(s2.lat)
        dlat, dlon = la2 - la1, radians(s2.lon - s1.lon)
        a = sin(dlat / 2) ** 2 + cos(la1) * cos(la2) * sin(dlon / 2) ** 2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    by_priority = {}
    for s in sites:
        by_priority.setdefault(s.priority, []).append(s)

    pairs_raw = []   # list of (SiteModel, SiteModel)
    # P1: full mesh (all-pairs)
    p1 = by_priority.get(1, [])
    for i, s1 in enumerate(p1):
        for j in range(i + 1, len(p1)):
            pairs_raw.append((s1, p1[j]))
    # P2+: nearest higher-priority
    for pri in sorted(by_priority):
        if pri == 1:
            continue
        higher = [s for s in sites if s.priority < pri]
        if not higher:
            continue
        for s in by_priority[pri]:
            pairs_raw.append((s, min(higher, key=lambda h: _dist(s, h))))

    logger.info("filter_p2p: %d pairs", len(pairs_raw))

    # Convert to plain dicts for find_p2p_roads.
    # Include boundary_geojson so the router can use city border nodes
    # as Dijkstra endpoints instead of the site centre coordinate.
    def _site_dict(s):
        d = {
            "name": s.name,
            "lat": s.lat,
            "lon": s.lon,
            "site_height_m": float(getattr(s, "site_height_m", 0.0) or 0.0),
        }
        if s.boundary_geojson:
            d["boundary_geojson"] = s.boundary_geojson
        return d

    site_pairs = [(_site_dict(s1), _site_dict(s2)) for s1, s2 in pairs_raw]

    # ── Find named routes ────────────────────────────────────────────
    routes, used_indices = find_p2p_roads(
        _roads_geojson, site_pairs, n_alternatives=3
    )

    # ── Store routes for later selection ────────────────────────────
    all_features = _roads_geojson.get("features", [])

    # Build boundary polygons for clipping (one per site that has a boundary).
    # Roads that cross into a city's own boundary are clipped to remove the
    # interior portion so routes visually start/end at the city border.
    def _clip_to_boundaries(features, s1_dict, s2_dict):
        """Clip feature geometries to exclude city boundary interiors."""
        try:
            from shapely.geometry import shape, mapping
        except ImportError:
            return features

        polys = []
        for sd in (s1_dict, s2_dict):
            if sd and sd.get("boundary_geojson"):
                try:
                    polys.append(shape(sd["boundary_geojson"]))
                except Exception:
                    pass
        if not polys:
            return features

        clipped = []
        for feat in features:
            try:
                geom = shape(feat["geometry"])
                for poly in polys:
                    geom = geom.difference(poly)
                if geom.is_empty:
                    continue
                clipped.append({**feat, "geometry": mapping(geom)})
            except Exception:
                clipped.append(feat)
        return clipped

    # Build pair site dicts lookup by pair_idx for clipping
    pair_site_dicts = {i: (s1, s2) for i, (s1, s2) in enumerate(site_pairs)}

    _p2p_routes = routes
    # _p2p_all_route_features: UNCLIPPED — used for export and graph re-use.
    # _p2p_display_features:   CLIPPED   — used only for frontend rendering.
    # Keeping them separate ensures clipping doesn't sever routing into cities.
    _p2p_all_route_features = {}
    _p2p_display_features.clear()
    for r in routes:
        raw_feats = [all_features[i] for i in r["feature_indices"]]
        s1d, s2d = pair_site_dicts.get(r["pair_idx"], ({}, {}))
        _p2p_all_route_features[r["route_id"]] = raw_feats
        _p2p_display_features[r["route_id"]] = _clip_to_boundaries(raw_feats, s1d, s2d)

    # Embed display (clipped) features so routes visually start/end at city borders
    for r in routes:
        r["features"] = _p2p_display_features[r["route_id"]]

    # ── Filter roads GeoJSON to used features only (all selected initially) ──
    # Use UNCLIPPED features so the graph stays intact for re-routing and export.
    filtered_features = []
    seen_route_ids = set()
    for r in routes:
        if r["route_id"] not in seen_route_ids:
            seen_route_ids.add(r["route_id"])
            filtered_features.extend(_p2p_all_route_features[r["route_id"]])
    filtered = {"type": "FeatureCollection", "features": filtered_features}
    _roads_geojson = filtered
    _loaded_layers["roads"] = filtered

    # Build way_id -> route_id mapping for frontend coloring
    route_groups = {}
    for r in routes:
        for wid in r["way_ids"]:
            if wid not in route_groups:   # first-match wins
                route_groups[wid] = r["route_id"]

    pairs_out = [
        {"s1": r["site1"], "s2": r["site2"]}
        for r in routes
    ]

    logger.info("filter_p2p done: %d routes, %d road features",
                len(routes), len(filtered_features))

    return jsonify({
        "road_count":     len(filtered_features),
        "original_count": original_count,
        "layers":         {"roads": filtered},
        "routes":         routes,
        "route_groups":   route_groups,
        "pairs":          pairs_out,
    })


def select_routes():
    """Update roads layer to include only the selected routes."""
    global _roads_geojson, _loaded_layers
    data = request.json or {}
    selected_ids = set(data.get("route_ids", []))

    selected_features = []
    for r in _p2p_routes:
        if r["route_id"] in selected_ids:
            selected_features.extend(_p2p_all_route_features.get(r["route_id"], []))

    filtered = {"type": "FeatureCollection", "features": selected_features}
    _roads_geojson = filtered
    _loaded_layers["roads"] = filtered

    logger.info("select_routes: %d routes selected, %d features",
                len(selected_ids), len(selected_features))
    return jsonify({"layers": {"roads": filtered}, "road_count": len(selected_features)})


def reroute_with_waypoints():
    """Re-route a site pair's roads via forced waypoint segments."""
    global _p2p_routes, _p2p_all_route_features, _p2p_display_features
    global _roads_geojson, _full_roads_geojson, _loaded_layers, _forced_waypoints

    from generator.graph import find_route_via_waypoints

    data = request.json or {}
    pair_key = data.get("pair_key", "")          # e.g. "Yerevan↔Gyumri"
    forced_way_ids = [int(w) for w in data.get("forced_way_ids", [])]

    if not _p2p_routes:
        return jsonify({"error": "No routes. Run Filter P2P first."}), 400
    routing_roads = _full_roads_geojson or _roads_geojson
    if not routing_roads:
        return jsonify({"error": "No roads loaded."}), 400

    # Find s1, s2 and pair_idx for this pair_key
    target_route = None
    for r in _p2p_routes:
        rk = r["site1"]["name"] + "\u2194" + r["site2"]["name"]
        if rk == pair_key:
            target_route = r
            break
    if target_route is None:
        return jsonify({"error": f"Pair '{pair_key}' not found."}), 400

    s1 = target_route["site1"]
    s2 = target_route["site2"]
    pair_idx = target_route["pair_idx"]

    # Store forced waypoints for this pair
    _forced_waypoints[pair_key] = forced_way_ids

    # Determine a unique route_id for the waypoint route
    route_id = f"route_wp_{pair_key.replace(chr(8596), '_')}"

    if not forced_way_ids:
        # No waypoints: restore original routes for this pair from _p2p_routes
        # (already stored — just keep whatever was there; caller will re-render)
        original = [r for r in _p2p_routes
                    if r["site1"]["name"] + "\u2194" + r["site2"]["name"] == pair_key]
        for r in original:
            r["features"] = _p2p_display_features.get(r["route_id"], [])
        _rebuild_roads_geojson()
        return jsonify({"routes": original, "road_count": len(_roads_geojson.get("features", []))})

    # Add site boundary info back (not stored in route dict; look up from store)
    site_map = {s.name: s for s in store}
    for sd in (s1, s2):
        site = site_map.get(sd["name"])
        if site and site.boundary_geojson:
            sd["boundary_geojson"] = site.boundary_geojson

    new_route = find_route_via_waypoints(
        routing_roads, s1, s2, forced_way_ids,
        pair_idx=pair_idx, route_id=route_id,
    )

    if new_route is None:
        return jsonify({"error": "Could not find a route through the selected segments. Try a different segment."}), 400

    # Replace routes for this pair in _p2p_routes
    _p2p_routes = [r for r in _p2p_routes
                   if r["site1"]["name"] + "\u2194" + r["site2"]["name"] != pair_key]

    # Build features for the new route (indices refer to routing_roads, the full set)
    all_features = routing_roads.get("features", [])
    raw_feats = [all_features[i] for i in new_route["feature_indices"]
                 if i < len(all_features)]
    _p2p_all_route_features[new_route["route_id"]] = raw_feats
    _p2p_display_features[new_route["route_id"]] = raw_feats  # no clipping for waypoint routes
    new_route["features"] = raw_feats

    _p2p_routes.append(new_route)

    _rebuild_roads_geojson()

    logger.info("reroute_with_waypoints: pair=%s waypoints=%d route=%s features=%d",
                pair_key, len(forced_way_ids), route_id, len(raw_feats))
    return jsonify({"routes": [new_route], "road_count": len(_roads_geojson.get("features", []))})


def _rebuild_roads_geojson():
    """Rebuild _roads_geojson and _loaded_layers['roads'] from current _p2p_routes."""
    global _roads_geojson, _loaded_layers
    all_feats = []
    seen = set()
    for r in _p2p_routes:
        for feat in _p2p_all_route_features.get(r["route_id"], []):
            wid = (feat.get("properties") or {}).get("osm_way_id")
            key = wid if wid is not None else id(feat)
            if key not in seen:
                seen.add(key)
                all_feats.append(feat)
    _roads_geojson = {"type": "FeatureCollection", "features": all_feats}
    _loaded_layers["roads"] = _roads_geojson
