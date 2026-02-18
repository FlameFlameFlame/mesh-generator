"""Named-road proximity filter for P2P route finding."""

import logging
import math
from shapely.geometry import shape

logger = logging.getLogger(__name__)


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _min_dist_km(geom, lat, lon):
    """Minimum haversine distance from any coordinate in geom to (lat, lon)."""
    coords = list(geom.coords) if hasattr(geom, "coords") else []
    if not coords:
        # MultiLineString / GeometryCollection
        return min(_min_dist_km(part, lat, lon) for part in geom.geoms)
    return min(_haversine_km(c[1], c[0], lat, lon) for c in coords)


def find_p2p_roads(roads_geojson, site_pairs, proximity_km=30.0):
    """
    For each (site1, site2) pair find named roads (grouped by OSM ``ref``,
    falling back to ``name``) that pass within proximity_km of BOTH sites.

    Args:
        roads_geojson: GeoJSON FeatureCollection
        site_pairs: list of (site1_dict, site2_dict) — each dict has lat/lon/name
        proximity_km: max km from a site to a road to count as "connected"

    Returns:
        routes   — list of dicts:
            {
              "route_id":        str,
              "ref":             str,   # OSM ref tag (e.g. "A-1")
              "road_name":       str,   # OSM name tag (human label)
              "pair_idx":        int,
              "site1":           {"name", "lat", "lon"},
              "site2":           {"name", "lat", "lon"},
              "feature_indices": [int, ...],   # indices in roads_geojson
              "way_ids":         [int, ...],   # osm_way_id values
            }
        used_indices — set of feature indices appearing in any route
    """
    features = roads_geojson.get("features", [])

    # Group features by ref tag (fall back to name, then unnamed bucket)
    groups = {}   # key -> {"ref", "name", "indices"}
    for idx, feat in enumerate(features):
        props = feat.get("properties", {})
        ref  = (props.get("ref")  or "").strip()
        name = (props.get("name") or "").strip()
        key  = ref or name or "unnamed"
        if key not in groups:
            groups[key] = {"ref": ref, "name": name, "indices": []}
        groups[key]["indices"].append(idx)

    logger.info("find_p2p_roads: %d road groups from %d features",
                len(groups), len(features))

    routes = []
    used_indices = set()
    route_counter = 0

    for pair_idx, (s1, s2) in enumerate(site_pairs):
        pair_routes = 0
        for key, grp in groups.items():
            close1 = close2 = False
            for idx in grp["indices"]:
                geom_data = features[idx].get("geometry")
                if not geom_data:
                    continue
                try:
                    geom = shape(geom_data)
                    d1 = _min_dist_km(geom, s1["lat"], s1["lon"])
                    d2 = _min_dist_km(geom, s2["lat"], s2["lon"])
                    if d1 < proximity_km:
                        close1 = True
                    if d2 < proximity_km:
                        close2 = True
                except Exception:
                    continue
                if close1 and close2:
                    break

            if close1 and close2:
                way_ids = [
                    features[i].get("properties", {}).get("osm_way_id")
                    for i in grp["indices"]
                ]
                routes.append({
                    "route_id":        f"route_{route_counter}",
                    "ref":             grp["ref"] or grp["name"] or key,
                    "road_name":       grp["name"] or grp["ref"] or key,
                    "pair_idx":        pair_idx,
                    "site1":           {"name": s1["name"], "lat": s1["lat"], "lon": s1["lon"]},
                    "site2":           {"name": s2["name"], "lat": s2["lat"], "lon": s2["lon"]},
                    "feature_indices": grp["indices"],
                    "way_ids":         [w for w in way_ids if w is not None],
                })
                used_indices.update(grp["indices"])
                route_counter += 1
                pair_routes += 1

        logger.info("Pair %d (%s \u2194 %s): %d routes",
                    pair_idx, s1["name"], s2["name"], pair_routes)

    return routes, used_indices
