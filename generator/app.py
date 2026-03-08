"""Site Generator — Flask + Leaflet web UI for placing mesh network sites."""

import json
import logging
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone

import h3
# Ensure mesh_calculator (sibling package) is importable even when running from
# source without an editable install.
def _ensure_mesh_calc_importable():
    try:
        import mesh_calculator  # noqa: F401
        return
    except ImportError:
        pass
    _src = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "mesh_calculator")
    )
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)

_ensure_mesh_calc_importable()

import yaml
from flask import Response, jsonify, render_template, request

from generator.app_factory import create_app as _create_base_app
from generator.models import SiteModel, SiteStore
from generator.optimization_manager import OptimizationJobManager
from generator.routes import register_blueprints
from generator.runtime_state import AppState
from generator.handlers import (
    file_picker_handlers,
    optimization_handlers,
    pipeline_site_handlers,
    project_handlers,
)
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
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9 _().-]{1,120}$")

def _resolve_output_dir(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        raw = DEFAULT_OUTPUT_DIR
    resolved = os.path.abspath(raw)
    projects_root = os.path.abspath(DEFAULT_OUTPUT_DIR)
    if not resolved.startswith(projects_root + os.sep) and resolved != projects_root:
        raise ValueError(f"Path must be inside projects root: {projects_root}")
    return resolved

def _project_dir(project_name: str) -> str:
    name = (project_name or "").strip()
    if not _PROJECT_NAME_RE.match(name):
        raise ValueError("Invalid project name")
    return os.path.join(os.path.abspath(DEFAULT_OUTPUT_DIR), name)

def _project_name_from_dir(path: str) -> str:
    root = os.path.abspath(DEFAULT_OUTPUT_DIR)
    p = os.path.abspath(path)
    if not p.startswith(root + os.sep):
        raise ValueError("Project path outside projects root")
    return os.path.basename(p)

def _resolve_project_output_dir(payload: dict | None) -> str:
    payload = payload or {}
    project_name = (payload.get("project_name") or "").strip()
    if project_name:
        return _project_dir(project_name)
    return _resolve_output_dir(payload.get("output_dir"))

def _list_project_names() -> list[str]:
    root = os.path.abspath(DEFAULT_OUTPUT_DIR)
    os.makedirs(root, exist_ok=True)
    out = []
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            out.append(entry)
    out.sort(key=lambda x: x.lower())
    return out

def _read_json_if_exists(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def _collect_project_runs(project_dir: str) -> list[dict]:
    status = _read_json_if_exists(os.path.join(project_dir, "status.json")) or {}
    runs_meta = status.get("optimization_runs") or []
    by_id = {}
    for item in runs_meta:
        run_id = (item or {}).get("run_id")
        if run_id:
            by_id[str(run_id)] = dict(item)

    runs_dir = os.path.join(project_dir, "runs")
    if os.path.isdir(runs_dir):
        for run_id in os.listdir(runs_dir):
            run_path = os.path.join(runs_dir, run_id)
            if not os.path.isdir(run_path):
                continue
            settings = _read_json_if_exists(os.path.join(run_path, "run_settings.json")) or {}
            if run_id not in by_id:
                by_id[run_id] = {
                    "run_id": run_id,
                    "saved_at_utc": settings.get("saved_at_utc"),
                    "parameters": settings.get("parameters", {}),
                    "summary": settings.get("summary", {}),
                    "files": settings.get("files", []),
                    "source": settings.get("source", "optimization"),
                }
            else:
                if not by_id[run_id].get("saved_at_utc"):
                    by_id[run_id]["saved_at_utc"] = settings.get("saved_at_utc")
                if not by_id[run_id].get("parameters"):
                    by_id[run_id]["parameters"] = settings.get("parameters", {})
                if not by_id[run_id].get("summary"):
                    by_id[run_id]["summary"] = settings.get("summary", {})
                if not by_id[run_id].get("files"):
                    by_id[run_id]["files"] = settings.get("files", [])
                if not by_id[run_id].get("source"):
                    by_id[run_id]["source"] = settings.get("source", "optimization")
    runs = list(by_id.values())
    runs.sort(key=lambda r: str(r.get("run_id", "")), reverse=True)
    return runs

def _load_run_outputs(project_dir: str, run_id: str) -> dict:
    global _loaded_layers, _loaded_report, _loaded_coverage, _runtime_tower_coverage
    run_dir = os.path.join(project_dir, "runs", str(run_id))
    if not os.path.isdir(run_dir):
        raise FileNotFoundError(f"Run not found: {run_id}")

    loaded_layers = {}
    for layer_key, fname in _CALC_LAYER_TO_FILENAME.items():
        fpath = os.path.join(run_dir, fname)
        data = _read_json_if_exists(fpath)
        if data is not None:
            _loaded_layers[layer_key] = data
            loaded_layers[layer_key] = data
        elif layer_key in ("towers", "edges", "grid_cells", "grid_cells_full", "gap_repair_hexes", "coverage"):
            _loaded_layers.pop(layer_key, None)

    report = _read_json_if_exists(os.path.join(run_dir, "report.json"))
    _loaded_report = report
    _loaded_coverage = loaded_layers.get("coverage")
    _runtime_tower_coverage = None

    return {
        "run_id": str(run_id),
        "layers": loaded_layers,
        "report": report,
        "has_coverage": _loaded_coverage is not None,
    }

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

# SSE state for streaming optimization output to browser
_opt_result = {}  # stores final optimization result for the stream endpoint
_opt_running = False
_opt_cancel_requested = False
_thread_local = threading.local()  # per-thread strategy label for log prefixing
_LOW_MAST_WARN_THRESHOLD_M = 5.0

class _OptimizationCanceled(Exception):
    """Raised when the user requests optimization cancellation."""

class _QueueLogHandler(logging.Handler):
    """Forward log records to the SSE queue when optimization is running."""
    def emit(self, record):
        if _job_manager.is_running:
            msg = self.format(record)
            label = getattr(_thread_local, 'strategy_label', '')
            if label:
                msg = f'[{label}] {msg}'
            _job_manager.put(msg)

# Attach queue handler to mesh_calculator logger at module load time
_queue_handler = _QueueLogHandler()
_queue_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger("mesh_calculator").addHandler(_queue_handler)

app = _create_base_app()
_app_state: AppState = app.extensions["app_state"]
_job_manager: OptimizationJobManager = app.extensions["optimization_manager"]
store = _app_state.store
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
_active_mesh_parameters = {} # last loaded/applied mesh parameters for runtime coverage

_CALC_LAYER_TO_FILENAME = {
    "towers": "towers.geojson",
    "edges": "visibility_edges.geojson",
    "grid_cells": "grid_cells.geojson",
    "grid_cells_full": "grid_cells_full.geojson",
    "gap_repair_hexes": "gap_repair_hexes.geojson",
    "coverage": "coverage.geojson",
}

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

def _build_grid_bundle_for_current_state(
    output_dir: str | None = None,
    elevation_path: str | None = None,
    boundary_geojson: dict | None = None,
    roads_geojson: dict | None = None,
):
    """
    Build a multi-resolution (8..9) grid bundle for current boundary/roads/elevation.

    Returns:
        dict: {bundle_path, resolutions, summary}
    """
    effective_elevation_path = elevation_path or _elevation_path
    effective_boundary_geojson = boundary_geojson or ((_loaded_layers or {}).get("boundary"))
    effective_roads_geojson = roads_geojson or _full_roads_geojson or _roads_geojson or ((_loaded_layers or {}).get("roads"))

    if not effective_elevation_path or not os.path.isfile(effective_elevation_path):
        raise ValueError("Elevation is not available")
    if not effective_boundary_geojson:
        raise ValueError("Boundary is not available")

    bundle_dir = os.path.abspath(output_dir) if output_dir else os.path.dirname(os.path.abspath(effective_elevation_path))
    os.makedirs(bundle_dir, exist_ok=True)
    bundle_path = os.path.join(bundle_dir, "grid_bundle.json")

    from mesh_calculator.core.grid_provider import GridProvider

    payload = GridProvider.build_bundle(
        bundle_path=bundle_path,
        elevation_path=effective_elevation_path,
        boundary_geojson=effective_boundary_geojson,
        roads_geojson=effective_roads_geojson,
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
    # Base planning resolution is fixed to 8 in mesh-generator UI/workflow.
    params["h3_resolution"] = 8
    if "min_fresnel_clearance_m" not in params:
        params["min_fresnel_clearance_m"] = 0.0
    return params

def index():
    return project_handlers.index(app_mod=sys.modules[__name__])

def list_projects():
    return project_handlers.list_projects(app_mod=sys.modules[__name__])

def create_project():
    return project_handlers.create_project(app_mod=sys.modules[__name__])

def rename_project():
    return project_handlers.rename_project(app_mod=sys.modules[__name__])

def list_project_runs():
    return project_handlers.list_project_runs(app_mod=sys.modules[__name__])

def load_project_run():
    return project_handlers.load_project_run(app_mod=sys.modules[__name__])

def delete_project_run():
    return project_handlers.delete_project_run(app_mod=sys.modules[__name__])

def open_project():
    return project_handlers.open_project(app_mod=sys.modules[__name__])

def get_sites():
    return pipeline_site_handlers.call(sys.modules[__name__], "get_sites")

def add_site():
    return pipeline_site_handlers.call(sys.modules[__name__], "add_site")

def update_site(idx):
    return pipeline_site_handlers.call(sys.modules[__name__], "update_site", idx)

def delete_site(idx):
    return pipeline_site_handlers.call(sys.modules[__name__], "delete_site", idx)

def detect_city_boundary(idx):
    return pipeline_site_handlers.call(sys.modules[__name__], "detect_city_boundary", idx)

def clear_project():
    return pipeline_site_handlers.call(sys.modules[__name__], "clear_project")

def clear_calculations():
    return pipeline_site_handlers.call(sys.modules[__name__], "clear_calculations")

def get_coverage():
    return pipeline_site_handlers.call(sys.modules[__name__], "get_coverage")

def get_tower_coverage():
    return pipeline_site_handlers.call(sys.modules[__name__], "get_tower_coverage")

def _persist_current_calculation_outputs(
    output_dir: str,
    *,
    parameters: dict | None = None,
    summary: dict | None = None,
    source: str = "manual_save",
) -> dict | None:
    return pipeline_site_handlers.call(
        sys.modules[__name__],
        "_persist_current_calculation_outputs",
        output_dir,
        parameters=parameters,
        summary=summary,
        source=source,
    )

def _run_runtime_tower_coverage(sources_payload: list, body: dict):
    return pipeline_site_handlers.call(
        sys.modules[__name__],
        "_run_runtime_tower_coverage",
        sources_payload,
        body,
    )

def calculate_tower_coverage_single():
    return pipeline_site_handlers.call(sys.modules[__name__], "calculate_tower_coverage_single")

def calculate_tower_coverage_batch():
    return pipeline_site_handlers.call(sys.modules[__name__], "calculate_tower_coverage_batch")

def download_elevation():
    return pipeline_site_handlers.call(sys.modules[__name__], "download_elevation")

def get_elevation_image():
    return pipeline_site_handlers.call(sys.modules[__name__], "get_elevation_image")

def get_grid_layers():
    return pipeline_site_handlers.call(sys.modules[__name__], "get_grid_layers")

def path_profile():
    return pipeline_site_handlers.call(sys.modules[__name__], "path_profile")

def link_analysis():
    return pipeline_site_handlers.call(sys.modules[__name__], "link_analysis")

def generate():
    return pipeline_site_handlers.call(sys.modules[__name__], "generate")

def filter_p2p():
    return pipeline_site_handlers.call(sys.modules[__name__], "filter_p2p")

def select_routes():
    return pipeline_site_handlers.call(sys.modules[__name__], "select_routes")

def reroute_with_waypoints():
    return pipeline_site_handlers.call(sys.modules[__name__], "reroute_with_waypoints")

def run_optimization():
    return optimization_handlers.run_optimization(app_mod=sys.modules[__name__])

def cancel_optimization():
    return optimization_handlers.cancel_optimization(app_mod=sys.modules[__name__])

def get_optimization_result():
    return optimization_handlers.get_optimization_result(app_mod=sys.modules[__name__])

def _save_project_to_dir(
    output_dir,
    parameters=None,
    active_routes=None,
    forced_waypoints=None,
    optimization_run=None,
):
    """Write config.yaml, routes.json, and status.json to output_dir.

    Writes geojson files (sites, boundary, roads) only if they are not already
    present — so calling this from the optimizer (which runs after export) does
    not overwrite freshly-exported data.
    """
    import shutil

    default_params = {
        "frequency_hz": 868_000_000,
        "mast_height_m": 5,
        "tx_power_mw": 500,
        "antenna_gain_dbi": 2.0,
        "receiver_sensitivity_dbm": -137,
        "max_towers_per_route": 10,
        "road_buffer_m": 100,
    }
    export_params = dict(default_params)
    if parameters:
        export_params.update(parameters)
    export_params.pop("h3_resolution", None)
    export_params.pop("max_coverage_radius_m", None)

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

    existing_status = {}
    status_path = os.path.join(output_dir, "status.json")
    if os.path.isfile(status_path):
        try:
            with open(status_path) as f:
                existing_status = json.load(f)
        except Exception:
            existing_status = {}

    run_history = list(existing_status.get("optimization_runs", []))
    if optimization_run:
        run_history.append(optimization_run)
    # Keep status bounded.
    if len(run_history) > 200:
        run_history = run_history[-200:]

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
        "optimization_runs": run_history,
        "last_optimization_run": optimization_run or existing_status.get("last_optimization_run"),
    }
    if active_routes:
        status["active_routes"] = active_routes
    if forced_waypoints:
        status["forced_waypoints"] = forced_waypoints
    with open(status_path, "w") as f:
        json.dump(status, f, indent=2)
    logger.info("Saved status.json to %s", output_dir)

def export():
    return project_handlers.export(app_mod=sys.modules[__name__])

def load_project():
    return project_handlers.load_project(app_mod=sys.modules[__name__])

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

def pick_file():
    return file_picker_handlers.pick_file(app_mod=sys.modules[__name__])

def optimization_stream():
    return optimization_handlers.optimization_stream(app_mod=sys.modules[__name__])

register_blueprints(
    app,
    {
        "index": index,
        "list_projects": list_projects,
        "create_project": create_project,
        "rename_project": rename_project,
        "list_project_runs": list_project_runs,
        "load_project_run": load_project_run,
        "delete_project_run": delete_project_run,
        "open_project": open_project,
        "get_sites": get_sites,
        "add_site": add_site,
        "update_site": update_site,
        "delete_site": delete_site,
        "detect_city_boundary": detect_city_boundary,
        "clear_project": clear_project,
        "clear_calculations": clear_calculations,
        "get_coverage": get_coverage,
        "get_tower_coverage": get_tower_coverage,
        "calculate_tower_coverage_single": calculate_tower_coverage_single,
        "calculate_tower_coverage_batch": calculate_tower_coverage_batch,
        "download_elevation": download_elevation,
        "get_elevation_image": get_elevation_image,
        "get_grid_layers": get_grid_layers,
        "path_profile": path_profile,
        "link_analysis": link_analysis,
        "generate": generate,
        "filter_p2p": filter_p2p,
        "select_routes": select_routes,
        "reroute_with_waypoints": reroute_with_waypoints,
        "run_optimization": run_optimization,
        "cancel_optimization": cancel_optimization,
        "get_optimization_result": get_optimization_result,
        "export": export,
        "load_project": load_project,
        "pick_file": pick_file,
        "optimization_stream": optimization_stream,
    },
)

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    flask_app = create_app()
    logger.info("Starting Mesh Site Generator at http://127.0.0.1:5050")
    webbrowser.open("http://127.0.0.1:5050")
    flask_app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)

def create_app(config: dict | None = None):
    """Compatibility app factory used by tests and script entrypoints."""
    if config:
        app.config.update(config)
    return app

if __name__ == "__main__":
    main()
