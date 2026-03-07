"""Site Generator — Flask + Leaflet web UI for placing mesh network sites."""

import json
import logging
import os
import queue
import sys
import threading
import webbrowser

import h3
# Ensure mesh_calculator (sibling package) and its dependencies are importable
# regardless of how this app is launched (e.g. outside the poetry venv).
# Find the poetry venv that has mesh_calculator installed (contains a .pth file
# pointing to the mesh_calculator source tree) and add its site-packages.
def _ensure_mesh_calc_importable():
    try:
        import mesh_calculator  # noqa: F401
        return
    except ImportError:
        pass
    import glob
    _src = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "mesh_calculator")
    )
    # Search all pypoetry venvs for one that has mesh_calculator installed
    _cache = os.path.expanduser("~/Library/Caches/pypoetry/virtualenvs")
    for _sp in glob.glob(os.path.join(_cache, "*", "lib", "python3*", "site-packages")):
        if os.path.isfile(os.path.join(_sp, "mesh_calculator.pth")):
            if _sp not in sys.path:
                sys.path.insert(0, _sp)
            break
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)

_ensure_mesh_calc_importable()

import yaml
from flask import Flask, Response, jsonify, render_template, request

from generator.models import SiteModel, SiteStore
from generator.export import (
    export_sites_geojson, export_boundary_geojson,
    export_roads_geojson, export_config_yaml,
    export_city_boundaries_geojson,
)
from generator.roads import fetch_roads_cached
from generator.elevation import fetch_and_write_elevation_cached, render_elevation_image

logger = logging.getLogger(__name__)

_WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_OUTPUT_DIR = os.path.join(_WORKSPACE_ROOT, "projects")


def _resolve_output_dir(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        raw = DEFAULT_OUTPUT_DIR
    return os.path.abspath(raw)


def _get_cache_dir(output_dir: str | None = None) -> str:
    """Return the cache directory (inside output_dir, or a global fallback)."""
    if output_dir:
        return os.path.join(os.path.abspath(output_dir), "cache")
    return os.path.expanduser("~/.cache/lora-mesh")


def _write_status_json(output_dir: str, **kwargs) -> None:
    """Incrementally update status.json in output_dir with given key-value pairs."""
    if not output_dir:
        return
    output_dir = os.path.abspath(output_dir)
    if not os.path.isdir(output_dir):
        return
    path = os.path.join(output_dir, "status.json")
    try:
        existing = {}
        if os.path.isfile(path):
            with open(path) as f:
                existing = json.load(f)
        existing.update(kwargs)
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        logger.debug("Failed to write status.json to %s", output_dir, exc_info=True)


# SSE log queue for streaming optimization output to browser
_opt_log_queue = queue.Queue()
_opt_result = {}  # stores final optimization result for the stream endpoint
_opt_running = False
_thread_local = threading.local()  # per-thread strategy label for log prefixing
_LOW_MAST_WARN_THRESHOLD_M = 5.0


class _QueueLogHandler(logging.Handler):
    """Forward log records to the SSE queue when optimization is running."""
    def emit(self, record):
        if _opt_running:
            msg = self.format(record)
            label = getattr(_thread_local, 'strategy_label', '')
            if label:
                msg = f'[{label}] {msg}'
            _opt_log_queue.put(msg)


# Attach queue handler to mesh_calculator logger at module load time
_queue_handler = _QueueLogHandler()
_queue_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger("mesh_calculator").addHandler(_queue_handler)


app = Flask(__name__)


@app.errorhandler(500)
def _handle_500(e):
    logger.exception("Internal server error")
    return jsonify({"error": f"Internal server error: {e}"}), 500
store = SiteStore()
_counter = 0
_loaded_layers = {}  # key -> geojson dict (roads, towers, boundary, edges)
_roads_geojson = None  # stored roads from Generate or Load
_full_roads_geojson = None  # full downloaded roads (never filtered by P2P)
_loaded_report = None  # report.json dict from mesh-engine output
_loaded_coverage = None  # coverage.geojson dict (lazy-served)
_runtime_tower_coverage = None  # runtime-only tower coverage FeatureCollection
_elevation_path = None  # path to downloaded elevation GeoTIFF
_grid_bundle_path = None  # path to persisted grid bundle JSON
_grid_provider = None  # in-memory mesh_calculator GridProvider
_grid_provider_summary = ""  # short status text for UI
_p2p_routes = []             # list of route dicts from find_p2p_roads
_p2p_all_route_features = {} # route_id → list of feature dicts (for select-routes)
_p2p_display_features = {}   # route_id → clipped feature dicts (frontend rendering)
_forced_waypoints = {}       # pair_key → list of osm_way_ids


def _close_grid_provider():
    global _grid_provider
    gp = _grid_provider
    _grid_provider = None
    if gp is not None:
        try:
            gp.close()
        except Exception:
            logger.debug("Failed to close grid provider", exc_info=True)


def _grid_cells_to_geojson(
    cells: set[str],
    *,
    road_cells: set[str],
    metadata_by_cell: dict,
    base_resolution: int,
    effective_min: int | None,
    effective_max: int | None,
) -> dict:
    """Serialize adaptive H3 cells to GeoJSON for frontend grid rendering."""
    features = []
    effective = (
        int(effective_max)
        if effective_max is not None
        else (int(effective_min) if effective_min is not None else int(base_resolution))
    )
    for h3_idx in sorted(cells):
        boundary = h3.cell_to_boundary(h3_idx)
        coords = [[lon, lat] for lat, lon in boundary]
        coords.append(coords[0])
        meta = metadata_by_cell.get(h3_idx, {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {
                "h3_index": h3_idx,
                "elevation": meta.get("elevation"),
                "has_road": h3_idx in road_cells,
                "is_in_unfit_area": bool(meta.get("is_in_unfit_area", False)),
                "h3_resolution": int(meta.get("h3_resolution", h3.get_resolution(h3_idx))),
                "base_h3_resolution": int(meta.get("base_h3_resolution", base_resolution)),
                "target_h3_resolution": int(meta.get("target_h3_resolution", meta.get("h3_resolution", h3.get_resolution(h3_idx)))),
                "gradient_m_per_km": float(meta.get("gradient_m_per_km", 0.0) or 0.0),
                "adaptive_refined": bool(meta.get("adaptive_refined", False)),
                "effective_h3_resolution": int(meta.get("effective_h3_resolution", effective)),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _cells_in_bbox(cells: set[str], viewport: dict | None, max_cells: int | None = None) -> set[str]:
    """Filter H3 cells by viewport bbox and optionally cap count."""
    if not viewport:
        if max_cells and len(cells) > max_cells:
            return set(list(sorted(cells))[:max_cells])
        return set(cells)
    try:
        south = float(viewport.get("south"))
        west = float(viewport.get("west"))
        north = float(viewport.get("north"))
        east = float(viewport.get("east"))
    except Exception:
        return set(cells)

    if north < south:
        south, north = north, south

    out: set[str] = set()
    for h3_idx in cells:
        lat, lon = h3.cell_to_latlng(h3_idx)
        in_lon = (west <= lon <= east) if west <= east else (lon >= west or lon <= east)
        if south <= lat <= north and in_lon:
            out.add(h3_idx)
    if max_cells and len(out) > max_cells:
        # Deterministic cap keeps payload bounded for UI rendering.
        return set(list(sorted(out))[:max_cells])
    return out


def _load_grid_provider_from_bundle(bundle_path: str, elevation_path: str | None = None):
    """Load grid provider from persisted bundle."""
    from mesh_calculator.core.grid_provider import GridProvider

    provider = GridProvider.from_bundle(bundle_path, elevation_path=elevation_path)
    return provider


def _build_grid_bundle_for_current_state(output_dir: str | None = None):
    """
    Build a multi-resolution (8..9) grid bundle for current boundary/roads/elevation.

    Returns:
        dict: {bundle_path, resolutions, summary}
    """
    if not _elevation_path or not os.path.isfile(_elevation_path):
        raise ValueError("Elevation is not available")
    boundary_geojson = (_loaded_layers or {}).get("boundary")
    if not boundary_geojson:
        raise ValueError("Boundary is not available")

    roads_geojson = _full_roads_geojson or _roads_geojson or (_loaded_layers or {}).get("roads")
    bundle_dir = os.path.abspath(output_dir) if output_dir else os.path.dirname(os.path.abspath(_elevation_path))
    os.makedirs(bundle_dir, exist_ok=True)
    bundle_path = os.path.join(bundle_dir, "grid_bundle.json")

    from mesh_calculator.core.grid_provider import GridProvider

    payload = GridProvider.build_bundle(
        bundle_path=bundle_path,
        elevation_path=_elevation_path,
        boundary_geojson=boundary_geojson,
        roads_geojson=roads_geojson,
        resolutions=(8, 9),
    )
    res = sorted(int(r) for r in (payload.get("resolutions") or {}).keys())
    return {
        "bundle_path": bundle_path,
        "resolutions": res,
        "summary": f"res={','.join(str(r) for r in res)}",
    }


def _hydrate_grid_provider(bundle_path: str, elevation_path: str | None = None):
    """(Re)load in-memory provider from bundle path and update globals."""
    global _grid_provider, _grid_bundle_path, _grid_provider_summary
    _close_grid_provider()
    _grid_provider = _load_grid_provider_from_bundle(bundle_path, elevation_path=elevation_path)
    _grid_bundle_path = os.path.abspath(bundle_path)
    res = _grid_provider.available_resolutions()
    _grid_provider_summary = f"res={','.join(str(r) for r in res)}" if res else ""


def _normalize_mesh_parameters(param_overrides: dict | None) -> dict:
    """
    Normalize optimization parameter overrides from UI.

    mesh-generator default is strict LOS unless explicitly overridden.
    """
    params = dict(param_overrides or {})
    if "min_fresnel_clearance_m" not in params:
        params["min_fresnel_clearance_m"] = 0.0
    return params


@app.route("/")
def index():
    return render_template("index.html", default_output_dir=DEFAULT_OUTPUT_DIR)


@app.route("/api/sites", methods=["GET"])
def get_sites():
    return jsonify(store.to_list())


@app.route("/api/sites", methods=["POST"])
def add_site():
    global _counter
    data = request.json
    _counter += 1
    site = SiteModel(
        name=data["name"],
        lat=data["lat"],
        lon=data["lon"],
        priority=data.get("priority", 1),
        site_height_m=float(data.get("site_height_m", 0.0) or 0.0),
        fetch_city=bool(data.get("fetch_city", True)),
    )
    store.add(site)
    logger.info("Added site %s at (%.4f, %.4f) priority=%d", site.name, site.lat, site.lon, site.priority)
    return jsonify(store.to_list())


@app.route("/api/sites/<int:idx>", methods=["PUT"])
def update_site(idx):
    data = request.json
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    site = store.get(idx)
    if "name" in data:
        site.name = data["name"]
    if "priority" in data:
        store.update_priority(idx, data["priority"])
    if "site_height_m" in data:
        site.site_height_m = float(data.get("site_height_m", 0.0) or 0.0)
    if "fetch_city" in data:
        site.fetch_city = bool(data["fetch_city"])
        if not site.fetch_city:
            site.boundary_geojson = None
            site.boundary_name = ""
    logger.info("Updated site %d: name=%s priority=%d", idx, site.name, site.priority)
    return jsonify(store.to_list())


@app.route("/api/sites/<int:idx>", methods=["DELETE"])
def delete_site(idx):
    if idx < 0 or idx >= len(store):
        return jsonify({"error": "invalid index"}), 400
    name = store.get(idx).name
    store.remove(idx)
    logger.info("Deleted site %d (%s)", idx, name)
    return jsonify(store.to_list())


@app.route("/api/sites/<int:idx>/detect-city", methods=["POST"])
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


@app.route("/api/clear", methods=["POST"])
def clear_project():
    """Clear all sites and loaded layers."""
    global _counter, _roads_geojson, _full_roads_geojson, _loaded_layers, _loaded_report
    global _loaded_coverage, _runtime_tower_coverage, _elevation_path, _p2p_routes, _p2p_all_route_features
    global _p2p_display_features, _forced_waypoints, _grid_bundle_path, _grid_provider_summary
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
    if _elevation_path and os.path.isfile(_elevation_path):
        try:
            os.unlink(_elevation_path)
        except OSError:
            pass
    _elevation_path = None
    _close_grid_provider()
    _grid_bundle_path = None
    _grid_provider_summary = ""
    logger.info("Project cleared")
    return jsonify({"ok": True})


@app.route("/api/clear-calculations", methods=["POST"])
def clear_calculations():
    """Delete mesh_calculator output files from disk and reset server-side layer state."""
    global _loaded_layers, _loaded_report, _loaded_coverage, _runtime_tower_coverage
    data = request.json or {}
    output_dir = _resolve_output_dir(data.get("output_dir"))
    files_to_delete = [
        "towers.geojson", "coverage.geojson", "visibility_edges.geojson",
        "report.json", "status.json", "tower_coverage.geojson",
    ]
    deleted = 0
    for fname in files_to_delete:
        fpath = os.path.join(output_dir, fname)
        if os.path.isfile(fpath):
            os.remove(fpath)
            deleted += 1
    _loaded_layers.pop("towers", None)
    _loaded_layers.pop("edges", None)
    _loaded_layers.pop("coverage", None)
    _loaded_report = None
    _loaded_coverage = None
    _runtime_tower_coverage = None
    logger.info("Cleared %d calculation file(s) from %s", deleted, output_dir)
    return jsonify({"deleted": deleted, "output_dir": output_dir})


@app.route("/api/coverage", methods=["GET"])
def get_coverage():
    """Serve cached coverage GeoJSON (lazy-loaded by frontend on toggle)."""
    if _loaded_coverage is None:
        return jsonify({"error": "No coverage data loaded"}), 404
    return jsonify(_loaded_coverage)


@app.route("/api/tower-coverage", methods=["GET"])
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


@app.route("/api/tower-coverage/calculate", methods=["POST"])
def calculate_tower_coverage_single():
    body = request.json or {}
    source = body.get("source")
    sources = body.get("sources")
    if source is not None and sources is None:
        sources = [source]
    if not isinstance(sources, list) or not sources:
        return jsonify({"error": "Provide source or non-empty sources list"}), 400
    return _run_runtime_tower_coverage(sources, body)


@app.route("/api/tower-coverage/calculate-batch", methods=["POST"])
def calculate_tower_coverage_batch():
    body = request.json or {}
    sources = body.get("sources")
    if not isinstance(sources, list) or not sources:
        return jsonify({"error": "Provide non-empty sources list"}), 400
    return _run_runtime_tower_coverage(sources, body)


@app.route("/api/elevation", methods=["POST"])
def download_elevation():
    """Download SRTM elevation tiles for the site bounding box."""
    import tempfile
    global _elevation_path, _grid_bundle_path, _grid_provider_summary
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
        output_dir_for_cache = _resolve_output_dir(payload.get("output_dir"))
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
        grid_info = _build_grid_bundle_for_current_state(output_dir_for_cache)
        _hydrate_grid_provider(grid_info["bundle_path"], elevation_path=_elevation_path)
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


@app.route("/api/elevation-image", methods=["GET"])
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


@app.route("/api/grid-layers", methods=["POST"])
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


@app.route("/api/path-profile", methods=["POST"])
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


@app.route("/api/link-analysis", methods=["POST"])
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


@app.route("/api/generate", methods=["POST"])
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

    output_dir_for_cache = _resolve_output_dir(payload.get("output_dir"))
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


@app.route("/api/roads/filter-p2p", methods=["POST"])
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


@app.route("/api/roads/select-routes", methods=["POST"])
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


@app.route("/api/roads/reroute-with-waypoints", methods=["POST"])
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


@app.route("/api/run-optimization", methods=["POST"])
def run_optimization():
    """
    Run the mesh_calculator route pipeline on the currently selected routes.
    Launches the pipeline in a background thread and returns immediately.
    Results are streamed via /api/optimization-stream (SSE).
    """
    global _opt_running, _opt_result
    try:
        from mesh_calculator.core.config import MeshConfig, RouteSpec
        from mesh_calculator.optimization.route_pipeline import run_route_pipeline
    except ImportError as exc:
        return jsonify({
            "error": (
                "mesh_calculator is not installed in this Python environment. "
                f"Install it with: cd mesh_calculator && poetry install  ({exc})"
            )
        }), 500

    if _opt_running:
        return jsonify({"error": "Optimization already running."}), 409

    if not _p2p_routes:
        return jsonify({"error": "No routes found. Run Filter P2P first."}), 400

    if not _elevation_path or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data. Download Elevation first."}), 400

    body = request.json or {}
    max_towers = int(body.get("max_towers_per_route", 8))
    param_overrides = _normalize_mesh_parameters(body.get("parameters", {}))
    output_dir = body.get("output_dir", "")

    # Build MeshConfig (with optional overrides from request body)
    valid_fields = MeshConfig.__dataclass_fields__
    mesh_config = MeshConfig(**{k: v for k, v in param_overrides.items() if k in valid_fields})
    low_mast_warning = None
    if mesh_config.mast_height_m < _LOW_MAST_WARN_THRESHOLD_M:
        low_mast_warning = (
            f"Low mast height ({mesh_config.mast_height_m:.1f} m) strongly increases "
            "NLOS/disconnected outcomes. Consider raising mast height or max_towers_per_route."
        )
        logger.warning(low_mast_warning)

    # Build RouteSpec list from currently selected routes + their features
    route_specs = []
    for r in _p2p_routes:
        feats = _p2p_all_route_features.get(r["route_id"], [])
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

    # Collect city boundaries from sites that have boundary_geojson
    city_boundaries_geojson = None
    city_features = [
        {"type": "Feature", "geometry": s.boundary_geojson, "properties": {"name": s.boundary_name}}
        for s in store
        if s.boundary_geojson is not None
    ]
    if city_features:
        city_boundaries_geojson = {"type": "FeatureCollection", "features": city_features}

    # Drain any stale messages from previous runs
    while not _opt_log_queue.empty():
        try:
            _opt_log_queue.get_nowait()
        except queue.Empty:
            break
    if low_mast_warning:
        _opt_log_queue.put(f"WARNING: {low_mast_warning}")

    _opt_result = {}

    def _run_pipeline():
        import copy
        import shutil
        import tempfile

        global _opt_running, _opt_result, _loaded_layers, _loaded_coverage, _runtime_tower_coverage
        _opt_running = True
        try:
            tmp_dir_dp = tempfile.mkdtemp(prefix="mesh_opt_dp_")

            logger.info(
                "run_optimization: %d routes, max_towers=%d (DP)",
                len(route_specs), max_towers,
            )
            boundary_geojson = (_loaded_layers or {}).get("boundary")

            def _make_progress_callback():
                def _callback(event: dict):
                    if isinstance(event, dict):
                        _opt_log_queue.put({"progress": dict(event)})
                return _callback

            def _run_one(out_dir):
                _thread_local.strategy_label = "dp"
                config_copy = copy.deepcopy(mesh_config)
                return run_route_pipeline(
                    routes=route_specs,
                    mesh_config=config_copy,
                    grid_provider=_grid_provider,
                    city_boundaries_geojson=city_boundaries_geojson,
                    boundary_geojson=boundary_geojson,
                    output_dir=out_dir,
                    progress_callback=_make_progress_callback(),
                )

            summary = _run_one(tmp_dir_dp)

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
            _opt_log_queue.put({
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

            _loaded_layers.pop("coverage", None)
            _loaded_coverage = None
            for key, _ in output_keys:
                if key in result:
                    _loaded_layers[key] = result[key]

            logger.info(
                "run_optimization complete: DP=%d towers/%d edges",
                summary.get("total_towers", 0),
                summary.get("visibility_edges", 0),
            )

            # Persist DP results to project output_dir if provided
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                for fname in ["towers.geojson", "visibility_edges.geojson",
                              "grid_cells.geojson", "grid_cells_full.geojson",
                              "gap_repair_hexes.geojson",
                              "report.json"]:
                    src = os.path.join(tmp_dir_dp, fname)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(output_dir, fname))
                # Write full project state so the directory can be reopened
                _save_project_to_dir(output_dir, parameters=param_overrides)
                logger.info("Saved optimization results to %s", output_dir)

            _runtime_tower_coverage = None

            _opt_result = result
            _opt_log_queue.put({"done": True, "summary": summary})

        except Exception as exc:
            logger.exception("run_optimization failed")
            _opt_log_queue.put({"error": str(exc)})
        finally:
            _opt_running = False

    threading.Thread(target=_run_pipeline, daemon=True).start()
    return jsonify({"started": True, "warning": low_mast_warning})


@app.route("/api/optimization-result", methods=["GET"])
def get_optimization_result():
    """Return the result from the last completed optimization run."""
    if not _opt_result:
        return jsonify({"error": "No optimization result available."}), 404
    return jsonify(_opt_result)


def _save_project_to_dir(output_dir, parameters=None, active_routes=None, forced_waypoints=None):
    """Write config.yaml, routes.json, and status.json to output_dir.

    Writes geojson files (sites, boundary, roads) only if they are not already
    present — so calling this from the optimizer (which runs after export) does
    not overwrite freshly-exported data.
    """
    import shutil

    default_params = {
        "h3_resolution": 8,
        "frequency_hz": 868_000_000,
        "mast_height_m": 5,
        "tx_power_mw": 500,
        "antenna_gain_dbi": 2.0,
        "receiver_sensitivity_dbm": -137,
        "max_towers_per_route": 10,
        "road_buffer_m": 100,
        "max_coverage_radius_m": 15000,
    }
    export_params = dict(default_params)
    if parameters:
        export_params.update(parameters)

    sites = list(store)
    sites_path = os.path.join(output_dir, "sites.geojson")
    boundary_path = os.path.join(output_dir, "boundary.geojson")
    roads_path = os.path.join(output_dir, "roads.geojson")

    if sites:
        if not os.path.isfile(sites_path):
            export_sites_geojson(sites, sites_path)
        if not os.path.isfile(boundary_path):
            export_boundary_geojson(sites, boundary_path, roads_geojson=_roads_geojson)
        if _roads_geojson and not os.path.isfile(roads_path):
            export_roads_geojson(_roads_geojson, roads_path)

        roads_export_path = roads_path if os.path.isfile(roads_path) else ""
        elev_dest = os.path.join(output_dir, "elevation.tif")
        elevation_export_path = elev_dest if os.path.isfile(elev_dest) else (_elevation_path or "")
        grid_bundle_export_path = ""
        if _grid_bundle_path and os.path.isfile(_grid_bundle_path):
            grid_dest = os.path.join(output_dir, "grid_bundle.json")
            try:
                if os.path.abspath(_grid_bundle_path) != os.path.abspath(grid_dest):
                    shutil.copy2(_grid_bundle_path, grid_dest)
                grid_bundle_export_path = grid_dest
            except Exception:
                logger.warning("Failed to copy grid bundle to project output", exc_info=True)
                grid_bundle_export_path = _grid_bundle_path
        city_boundaries_path = os.path.join(output_dir, "city_boundaries.geojson")
        city_boundaries_export = city_boundaries_path if os.path.isfile(city_boundaries_path) else ""
        export_config_yaml(
            output_dir, sites_path, boundary_path,
            roads_path=roads_export_path,
            elevation_path=elevation_export_path,
            grid_bundle_path=grid_bundle_export_path,
            city_boundaries_path=city_boundaries_export,
            parameters=export_params,
        )
        logger.info("Saved config.yaml to %s", output_dir)

    if _p2p_routes:
        routes_path = os.path.join(output_dir, "routes.json")
        max_towers = export_params.get("max_towers_per_route", 8)
        routes_export = {
            "parameters": export_params,
            "routes": [
                dict(r,
                     max_towers_per_route=max_towers,
                     features=_p2p_all_route_features.get(r["route_id"], []))
                for r in _p2p_routes
            ],
        }
        with open(routes_path, "w") as f:
            json.dump(routes_export, f, indent=2)
        logger.info("Saved routes.json to %s (%d routes)", output_dir, len(_p2p_routes))

    status = {
        "has_roads": bool(_roads_geojson),
        "has_elevation": bool(_elevation_path and os.path.isfile(_elevation_path)),
        "has_grid_provider": bool(_grid_provider),
        "has_routes": bool(_p2p_routes),
        "has_optimization": bool(_loaded_layers.get("towers")),
        "elevation_path": _elevation_path or "",
        "grid_bundle_path": (
            os.path.join(output_dir, "grid_bundle.json")
            if os.path.isfile(os.path.join(output_dir, "grid_bundle.json"))
            else (_grid_bundle_path or "")
        ),
        "grid_provider_summary": _grid_provider_summary or "",
        "parameters": export_params,
    }
    if active_routes:
        status["active_routes"] = active_routes
    if forced_waypoints:
        status["forced_waypoints"] = forced_waypoints
    with open(os.path.join(output_dir, "status.json"), "w") as f:
        json.dump(status, f, indent=2)
    logger.info("Saved status.json to %s", output_dir)


@app.route("/api/export", methods=["POST"])
def export():
    if len(store) == 0:
        return jsonify({"error": "No sites to export."})

    try:
        store.validate_priorities()
    except ValueError as e:
        logger.warning("Priority validation failed: %s", e)
        return jsonify({"error": str(e)})

    data = request.json
    output_dir = _resolve_output_dir(data.get("output_dir"))
    max_towers_per_route = int(data.get("max_towers_per_route", 8))
    os.makedirs(output_dir, exist_ok=True)

    sites = list(store)

    # Always write geojson files fresh on explicit export
    sites_path = os.path.join(output_dir, "sites.geojson")
    boundary_path = os.path.join(output_dir, "boundary.geojson")
    roads_path = os.path.join(output_dir, "roads.geojson")
    export_sites_geojson(sites, sites_path)
    export_boundary_geojson(sites, boundary_path, roads_geojson=_roads_geojson)
    if _roads_geojson:
        export_roads_geojson(_roads_geojson, roads_path)

    # Copy pre-downloaded elevation to output directory
    if _elevation_path and os.path.isfile(_elevation_path):
        import shutil
        elevation_dest = os.path.join(output_dir, "elevation.tif")
        if os.path.abspath(_elevation_path) != os.path.abspath(elevation_dest):
            shutil.copy2(_elevation_path, elevation_dest)
            logger.info("Copied elevation to %s", elevation_dest)
        else:
            logger.info("Elevation already at destination, skipping copy")

    # Export city boundaries if any site has one
    if any(s.boundary_geojson for s in sites):
        city_boundaries_path = os.path.join(output_dir, "city_boundaries.geojson")
        export_city_boundaries_geojson(sites, city_boundaries_path)

    req_params = data.get("parameters", {})
    req_params.setdefault("max_towers_per_route", max_towers_per_route)
    active_routes = data.get("active_routes", {})
    forced_waypoints = data.get("forced_waypoints", {})

    # Write config.yaml + status.json via helper
    _save_project_to_dir(
        output_dir,
        parameters=req_params,
        active_routes=active_routes,
        forced_waypoints=forced_waypoints,
    )

    config_path = os.path.join(output_dir, "config.yaml")
    logger.info("Exported %d sites to %s", len(sites), output_dir)
    return jsonify({
        "count": len(sites),
        "output_dir": output_dir,
        "config_path": config_path,
    })


@app.route("/api/load", methods=["POST"])
def load_project():
    """Load a project from a config.yaml path (or directory containing one)."""
    global _counter, _loaded_layers, _roads_geojson, _full_roads_geojson
    global _loaded_report, _loaded_coverage, _runtime_tower_coverage
    global _elevation_path, _p2p_routes, _p2p_all_route_features
    global _grid_bundle_path, _grid_provider_summary
    data = request.json
    path = data.get("path", "").strip()

    if os.path.isdir(path):
        path = os.path.join(path, "config.yaml")
    if not os.path.isfile(path):
        logger.error("Config file not found: %s", path)
        return jsonify({"error": f"File not found: {path}"})

    config_dir = os.path.dirname(os.path.abspath(path))

    with open(path) as f:
        config = yaml.safe_load(f)
    logger.info("Loaded config from %s", path)

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
        # Try relative to config_dir first (correct case)
        candidate = os.path.join(config_dir, p)
        if os.path.exists(candidate):
            return candidate
        # Fallback: path may be relative to config_dir's parent (legacy configs
        # written when output_dir was the parent directory).
        parent_candidate = os.path.join(os.path.dirname(config_dir), p)
        if os.path.exists(parent_candidate):
            return parent_candidate
        # Return the primary candidate so callers get the original (non-existent) path
        return candidate

    # Load sites into the store
    store._sites.clear()
    _counter = 0
    sites_path = resolve(inputs.get("target_sites"))
    if sites_path and os.path.isfile(sites_path):
        with open(sites_path) as f:
            sites_data = json.load(f)
        for feat in sites_data.get("features", []):
            props = feat.get("properties", {})
            coords = feat["geometry"]["coordinates"]
            site = SiteModel(
                name=props.get("name", f"Site_{_counter + 1}"),
                lat=coords[1],
                lon=coords[0],
                priority=props.get("priority", 1),
                site_height_m=float(props.get("site_height_m", 0.0) or 0.0),
                fetch_city=props.get("fetch_city", True),
                boundary_name=props.get("boundary_name", ""),
            )
            store.add(site)
            _counter += 1
        logger.info("Loaded %d sites from %s", len(store), sites_path)

    # Load GeoJSON layers for visualization
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
            logger.info("Loaded layer '%s' from %s", key, fpath)
        else:
            if fpath:
                logger.warning("Layer '%s' file not found: %s", key, fpath)

    _loaded_layers = layers
    _roads_geojson = layers.get("roads")
    _full_roads_geojson = layers.get("roads")
    _close_grid_provider()
    _grid_bundle_path = None
    _grid_provider_summary = ""

    # Load report
    _loaded_report = None
    report_path = resolve(outputs.get("report"))
    if report_path and os.path.isfile(report_path):
        with open(report_path) as f:
            _loaded_report = json.load(f)
        logger.info("Loaded report from %s", report_path)

    # Load coverage (cached for lazy serving via /api/coverage)
    _loaded_coverage = None
    coverage_path = resolve(outputs.get("coverage"))
    if coverage_path and os.path.isfile(coverage_path):
        with open(coverage_path) as f:
            _loaded_coverage = json.load(f)
        logger.info("Loaded coverage from %s (%d features)",
                     coverage_path, len(_loaded_coverage.get("features", [])))

    # Runtime tower coverage is always computed on demand (not loaded from disk)
    _runtime_tower_coverage = None

    # Load elevation if available
    elevation_file = resolve(inputs.get("elevation"))
    if elevation_file and os.path.isfile(elevation_file):
        _elevation_path = elevation_file
        logger.info("Loaded elevation from %s", elevation_file)
    else:
        _elevation_path = None

    # Resolve grid bundle path from config inputs first
    configured_grid_bundle = resolve(inputs.get("grid_bundle"))
    if configured_grid_bundle and os.path.isfile(configured_grid_bundle):
        try:
            _hydrate_grid_provider(configured_grid_bundle, elevation_path=_elevation_path)
            logger.info("Loaded grid bundle from config: %s", configured_grid_bundle)
        except Exception:
            logger.warning("Failed to load configured grid bundle: %s", configured_grid_bundle, exc_info=True)

    # Derive output directory from config outputs section
    output_dir = None
    for out_key in ("towers", "coverage", "report", "visibility_edges"):
        out_path = resolve(outputs.get(out_key))
        if out_path:
            output_dir = os.path.dirname(out_path)
            break
    if not output_dir:
        output_dir = config_dir

    # Load status.json if present
    project_status = {}
    status_path = os.path.join(config_dir, "status.json")
    if os.path.isfile(status_path):
        with open(status_path) as f:
            project_status = json.load(f)
        logger.info("Loaded project status from %s", status_path)
        # Restore elevation path from status if not already found
        if not _elevation_path and project_status.get("elevation_path"):
            ep = project_status["elevation_path"]
            if os.path.isfile(ep):
                _elevation_path = ep
                logger.info("Restored elevation path from status: %s", ep)
        if _grid_provider is None and project_status.get("grid_bundle_path"):
            gb = project_status["grid_bundle_path"]
            if gb and not os.path.isabs(gb):
                gb = os.path.join(config_dir, gb)
            if os.path.isfile(gb):
                try:
                    _hydrate_grid_provider(gb, elevation_path=_elevation_path)
                    logger.info("Restored grid bundle from status: %s", gb)
                except Exception:
                    logger.warning("Failed to restore grid bundle from status: %s", gb, exc_info=True)

    # Keep config.yaml parameters as canonical on load. status.json can be stale
    # when users edit config manually or reuse older optimization outputs.
    if config_parameters:
        merged_params = {}
        if isinstance(project_status.get("parameters"), dict):
            merged_params.update(project_status["parameters"])
        merged_params.update(config_parameters)
        project_status["parameters"] = merged_params

    # Backward compatibility: build provider on demand when bundle is missing.
    if _grid_provider is None and _elevation_path and layers.get("boundary"):
        try:
            rebuilt = _build_grid_bundle_for_current_state(config_dir)
            _hydrate_grid_provider(rebuilt["bundle_path"], elevation_path=_elevation_path)
            _grid_bundle_path = rebuilt["bundle_path"]
            _grid_provider_summary = rebuilt["summary"]
            project_status["has_grid_provider"] = True
            project_status["grid_bundle_path"] = _grid_bundle_path
            project_status["grid_provider_summary"] = _grid_provider_summary
            logger.info("Rebuilt grid bundle for loaded project: %s", _grid_bundle_path)
        except Exception:
            logger.warning("Could not build grid bundle for loaded project", exc_info=True)

    # Restore routes from routes.json if present
    _p2p_routes = []
    _p2p_all_route_features = {}
    routes_file = os.path.join(config_dir, "routes.json")
    if os.path.isfile(routes_file):
        with open(routes_file) as f:
            routes_data = json.load(f)
        for r in routes_data.get("routes", []):
            meta = {k: v for k, v in r.items() if k != "features"}
            _p2p_routes.append(meta)
            _p2p_all_route_features[r["route_id"]] = r.get("features", [])
        logger.info("Restored %d routes from %s", len(_p2p_routes), routes_file)
        project_status["has_routes"] = True

    # Compute bounds for map fit
    bounds = _compute_bounds(layers, store)

    return jsonify({
        "config_path": os.path.abspath(path),
        "output_dir": output_dir,
        "sites": store.to_list(),
        "layers": layers,
        "bounds": bounds,
        "report": _loaded_report,
        "has_coverage": _loaded_coverage is not None,
        "has_elevation": _elevation_path is not None and os.path.isfile(_elevation_path),
        "has_grid_provider": _grid_provider is not None,
        "grid_provider_summary": _grid_provider_summary or "",
        "project_status": project_status,
        "routes": [
            dict(r, features=_p2p_all_route_features.get(r["route_id"], []))
            for r in _p2p_routes
        ],
        "active_routes": project_status.get("active_routes", {}),
        "forced_waypoints": project_status.get("forced_waypoints", {}),
    })


def _compute_bounds(layers, store):
    """Compute [[south, west], [north, east]] from all loaded data."""
    lats, lons = [], []
    for site in store:
        lats.append(site.lat)
        lons.append(site.lon)
    for key in ("roads", "boundary", "towers"):
        geojson = layers.get(key)
        if not geojson:
            continue
        for feat in geojson.get("features", []):
            _collect_coords(feat.get("geometry", {}), lats, lons)
    if not lats:
        return None
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def _collect_coords(geometry, lats, lons):
    """Recursively extract lat/lon from a GeoJSON geometry."""
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])
    if gtype == "Point":
        lons.append(coords[0])
        lats.append(coords[1])
    elif gtype in ("LineString", "MultiPoint"):
        for c in coords:
            lons.append(c[0])
            lats.append(c[1])
    elif gtype in ("Polygon", "MultiLineString"):
        for ring in coords:
            for c in ring:
                lons.append(c[0])
                lats.append(c[1])
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    lons.append(c[0])
                    lats.append(c[1])


@app.route("/api/pick-file", methods=["POST"])
def pick_file():
    """Open a native OS file picker and return the selected path."""
    import platform
    import subprocess

    try:
        if platform.system() == "Darwin":
            # macOS: use AppleScript choose folder.
            # Do NOT use "tell application X to activate" — that brings focus to
            # the calling process (Python) which crashes Flask on some macOS versions.
            script = (
                'set f to POSIX path of '
                '(choose folder with prompt "Select project directory (must contain config.yaml)")'
            )
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                return jsonify({"path": result.stdout.strip()})
            stderr = (result.stderr or "").strip()
            # User cancelled (osascript exits 1 with "User canceled" / -128)
            if "User canceled" in stderr or "(-128)" in stderr:
                return jsonify({"path": ""})
            logger.warning("macOS file picker failed: rc=%s stderr=%s", result.returncode, stderr)
            return jsonify({"error": f"Native picker failed: {stderr}", "path": ""})
        else:
            # Linux/Windows: try tkinter
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            path = filedialog.askopenfilename(
                title="Open project config",
                filetypes=[("YAML config", "*.yaml *.yml"), ("All files", "*")],
            )
            root.destroy()
            return jsonify({"path": path or ""})
    except Exception as e:
        logger.warning("File picker failed: %s", e)
        return jsonify({"error": str(e), "path": ""})


@app.route("/api/optimization-stream")
def optimization_stream():
    """SSE endpoint that streams optimization log lines to the browser."""
    def generate():
        while True:
            try:
                item = _opt_log_queue.get(timeout=30)
            except queue.Empty:
                yield "data: {}\n\n"  # keepalive
                continue
            if isinstance(item, dict):
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("done") or item.get("error"):
                    break
            else:
                yield f"data: {json.dumps({'log': item})}\n\n"
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting Mesh Site Generator at http://127.0.0.1:5050")
    webbrowser.open("http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)


if __name__ == "__main__":
    main()
