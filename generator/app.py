"""Site Generator — Flask + Leaflet web UI for placing mesh network sites."""

import json
import logging
import os
import sys
import webbrowser

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
from flask import Flask, jsonify, render_template, request

from generator.models import SiteModel, SiteStore
from generator.export import (
    export_sites_geojson, export_boundary_geojson,
    export_roads_geojson, export_config_yaml,
    export_city_boundaries_geojson,
)
from generator.roads import fetch_roads_cached
from generator.elevation import fetch_and_write_elevation_cached, render_elevation_image

logger = logging.getLogger(__name__)


def _get_cache_dir(output_dir: str | None = None) -> str:
    """Return the cache directory (inside output_dir, or a global fallback)."""
    if output_dir:
        return os.path.join(os.path.abspath(output_dir), "cache")
    return os.path.expanduser("~/.cache/lora-mesh")


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
_loaded_tower_coverage = None  # tower_coverage.geojson dict (lazy-served)
_elevation_path = None  # path to downloaded elevation GeoTIFF
_p2p_routes = []             # list of route dicts from find_p2p_roads
_p2p_all_route_features = {} # route_id → list of feature dicts (for select-routes)
_p2p_display_features = {}   # route_id → clipped feature dicts (frontend rendering)
_forced_waypoints = {}       # pair_key → list of osm_way_ids


@app.route("/")
def index():
    return render_template("index.html")


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
    global _loaded_coverage, _loaded_tower_coverage, _elevation_path, _p2p_routes, _p2p_all_route_features
    global _p2p_display_features, _forced_waypoints
    store._sites.clear()
    _counter = 0
    _roads_geojson = None
    _full_roads_geojson = None
    _loaded_layers = {}
    _loaded_report = None
    _loaded_coverage = None
    _loaded_tower_coverage = None
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
    logger.info("Project cleared")
    return jsonify({"ok": True})


@app.route("/api/clear-calculations", methods=["POST"])
def clear_calculations():
    """Delete mesh_calculator output files from disk and reset server-side layer state."""
    global _loaded_layers, _loaded_report, _loaded_coverage, _loaded_tower_coverage
    data = request.json or {}
    output_dir = os.path.abspath(data.get("output_dir", "output"))
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
    _loaded_tower_coverage = None
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
    """Serve cached tower radial coverage GeoJSON (lazy-loaded by frontend on toggle)."""
    if _loaded_tower_coverage is None:
        return jsonify({"error": "No tower coverage data loaded"}), 404
    return jsonify(_loaded_tower_coverage)


@app.route("/api/elevation", methods=["POST"])
def download_elevation():
    """Download SRTM elevation tiles for the site bounding box."""
    import tempfile
    global _elevation_path
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
        output_dir_for_cache = payload.get("output_dir")
        fetch_and_write_elevation_cached(
            south, west, north, east, path,
            cache_dir=_get_cache_dir(output_dir_for_cache),
        )
        _elevation_path = path
        size_mb = os.path.getsize(path) / (1024 * 1024)
        from generator.elevation import _tiles_for_bbox
        tile_count = len(_tiles_for_bbox(south, west, north, east))
        logger.info("Downloaded elevation: %d tiles, %.1f MB -> %s", tile_count, size_mb, path)
        return jsonify({
            "tiles": tile_count,
            "size_mb": round(size_mb, 1),
            "path": path,
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


@app.route("/api/path-profile", methods=["POST"])
def path_profile():
    """Sample terrain elevation along a P2P route for a path-profile chart.

    Accepts route_id (from filter-p2p routes). Chains the route's road
    LineString features into an ordered polyline, then samples elevation
    every ~200 m along it. Returns points with dist_m, elevation_m, lat, lon
    so the frontend can map hover positions back to the map.
    """
    import math

    if not _elevation_path or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data. Download Elevation first."}), 400

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

    s1 = route_meta["site1"]
    s2 = route_meta["site2"]
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
        if site.boundary_geojson is None:
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

    output_dir_for_cache = payload.get("output_dir")
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
        d = {"name": s.name, "lat": s.lat, "lon": s.lon}
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

    Body (JSON, all optional):
        max_towers_per_route: int  (default 8)
        parameters: dict           (MeshConfig overrides, e.g. h3_resolution, mast_height_m)

    Returns JSON:
        { towers: GeoJSON FeatureCollection,
          edges:  GeoJSON FeatureCollection,
          coverage: GeoJSON FeatureCollection,
          summary: { total_towers, visibility_edges, total_cells, ... } }

    mesh_calculator must be importable (install it in the same venv).
    Elevation must have been downloaded first.
    """
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

    if not _p2p_routes:
        return jsonify({"error": "No routes found. Run Filter P2P first."}), 400

    if not _elevation_path or not os.path.isfile(_elevation_path):
        return jsonify({"error": "No elevation data. Download Elevation first."}), 400

    body = request.json or {}
    max_towers = int(body.get("max_towers_per_route", 8))
    param_overrides = body.get("parameters", {})
    output_dir = body.get("output_dir", "")

    # Build MeshConfig (with optional overrides from request body)
    valid_fields = MeshConfig.__dataclass_fields__
    mesh_config = MeshConfig(**{k: v for k, v in param_overrides.items() if k in valid_fields})

    # Build RouteSpec list from currently selected routes + their features
    route_specs = []
    for r in _p2p_routes:
        # Only include routes whose features are in the currently selected roads
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

    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="mesh_opt_")

    try:
        logger.info(
            "run_optimization: %d routes, max_towers=%d, output=%s",
            len(route_specs), max_towers, tmp_dir,
        )
        summary = run_route_pipeline(
            routes=route_specs,
            mesh_config=mesh_config,
            elevation_path=_elevation_path,
            city_boundaries_geojson=city_boundaries_geojson,
            output_dir=tmp_dir,
        )

        # Load and return the output GeoJSON files
        import json as _json
        result = {"summary": summary}
        for key, fname in [("towers", "towers.geojson"),
                           ("edges", "visibility_edges.geojson"),
                           ("coverage", "coverage.geojson"),
                           ("tower_coverage", "tower_coverage.geojson")]:
            fpath = os.path.join(tmp_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath) as f:
                    result[key] = _json.load(f)
                _loaded_layers[key] = result[key]

        logger.info(
            "run_optimization complete: %d towers, %d edges",
            summary.get("total_towers", 0), summary.get("visibility_edges", 0),
        )

        # Persist results to project output_dir if provided
        if output_dir:
            import shutil
            os.makedirs(output_dir, exist_ok=True)
            for fname in ["towers.geojson", "visibility_edges.geojson",
                          "coverage.geojson", "tower_coverage.geojson", "report.json"]:
                src = os.path.join(tmp_dir, fname)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(output_dir, fname))
            # Update status.json to reflect optimization is done
            status_path = os.path.join(output_dir, "status.json")
            if os.path.isfile(status_path):
                with open(status_path) as f:
                    status = _json.load(f)
                status["has_optimization"] = True
                with open(status_path, "w") as f:
                    _json.dump(status, f, indent=2)
            logger.info("Saved optimization results to %s", output_dir)

        return jsonify(result)

    except Exception as exc:
        logger.exception("run_optimization failed")
        return jsonify({"error": str(exc)}), 500


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
    output_dir = os.path.abspath(data.get("output_dir", "output"))
    max_towers_per_route = int(data.get("max_towers_per_route", 8))
    os.makedirs(output_dir, exist_ok=True)

    sites_path = os.path.join(output_dir, "sites.geojson")
    boundary_path = os.path.join(output_dir, "boundary.geojson")
    roads_path = os.path.join(output_dir, "roads.geojson")

    sites = list(store)
    export_sites_geojson(sites, sites_path)
    export_boundary_geojson(sites, boundary_path, roads_geojson=_roads_geojson)

    # Export roads if available
    roads_export_path = ""
    if _roads_geojson:
        export_roads_geojson(_roads_geojson, roads_path)
        roads_export_path = roads_path

    # Copy pre-downloaded elevation to output directory
    elevation_export_path = ""
    if _elevation_path and os.path.isfile(_elevation_path):
        import shutil
        elevation_dest = os.path.join(output_dir, "elevation.tif")
        shutil.copy2(_elevation_path, elevation_dest)
        elevation_export_path = elevation_dest
        logger.info("Copied elevation to %s", elevation_dest)

    # Export city boundaries if any site has one
    city_boundaries_path = ""
    if any(s.boundary_geojson for s in sites):
        city_boundaries_path = os.path.join(
            output_dir, "city_boundaries.geojson")
        export_city_boundaries_geojson(sites, city_boundaries_path)

    # Collect parameters from request for config export
    req_params = data.get("parameters", {})
    export_config_yaml(
        output_dir, sites_path, boundary_path,
        roads_path=roads_export_path,
        elevation_path=elevation_export_path,
        city_boundaries_path=city_boundaries_path,
        parameters=req_params,
    )

    # Export routes.json for standalone CLI use with mesh-calculator-routes
    routes_path = ""
    if _p2p_routes:
        routes_path = os.path.join(output_dir, "routes.json")
        routes_export = {
            "parameters": {
                "h3_resolution": 8,
                "frequency_hz": 868_000_000,
                "mast_height_m": 28,
                "max_visibility_m": 70_000,
            },
            "routes": [
                {
                    "route_id": r["route_id"],
                    "site1": r.get("site1", {}),
                    "site2": r.get("site2", {}),
                    "max_towers_per_route": max_towers_per_route,
                    "features": _p2p_all_route_features.get(r["route_id"], []),
                }
                for r in _p2p_routes
            ],
        }
        with open(routes_path, "w") as f:
            json.dump(routes_export, f, indent=2)
        logger.info("Exported routes to %s (%d routes)", routes_path, len(_p2p_routes))

    # Write status.json to capture project state
    status = {
        "has_roads": bool(_roads_geojson),
        "has_elevation": bool(_elevation_path and os.path.isfile(_elevation_path)),
        "has_routes": bool(_p2p_routes),
        "has_optimization": bool(_loaded_layers.get("towers")),
        "elevation_path": _elevation_path or "",
        "parameters": req_params,
    }
    status_path = os.path.join(output_dir, "status.json")
    with open(status_path, "w") as f:
        json.dump(status, f, indent=2)
    logger.info("Exported status to %s", status_path)

    logger.info("Exported %d sites to %s", len(sites), output_dir)
    return jsonify({
        "count": len(sites),
        "output_dir": output_dir,
        "files": [sites_path, boundary_path, roads_path,
                  elevation_export_path, os.path.join(output_dir, "config.yaml"),
                  routes_path, status_path],
    })


@app.route("/api/load", methods=["POST"])
def load_project():
    """Load a project from a config.yaml path (or directory containing one)."""
    global _counter, _loaded_layers, _roads_geojson, _full_roads_geojson, _loaded_report, _loaded_coverage, _loaded_tower_coverage, _elevation_path
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

    def resolve(p):
        if not p:
            return None
        if os.path.isabs(p):
            return p
        return os.path.join(config_dir, p)

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

    # Load tower radial coverage (cached for lazy serving via /api/tower-coverage)
    _loaded_tower_coverage = None
    tower_coverage_path = resolve(outputs.get("tower_coverage"))
    if tower_coverage_path and os.path.isfile(tower_coverage_path):
        with open(tower_coverage_path) as f:
            _loaded_tower_coverage = json.load(f)
        logger.info("Loaded tower coverage from %s (%d features)",
                     tower_coverage_path, len(_loaded_tower_coverage.get("features", [])))

    # Load elevation if available
    elevation_file = resolve(inputs.get("elevation"))
    if elevation_file and os.path.isfile(elevation_file):
        _elevation_path = elevation_file
        logger.info("Loaded elevation from %s", elevation_file)

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
        "has_elevation": _elevation_path is not None,
        "project_status": project_status,
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


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting Mesh Site Generator at http://127.0.0.1:5050")
    webbrowser.open("http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
