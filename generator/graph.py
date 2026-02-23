"""P2P road route finder — Dijkstra with highway-quality weighting."""

import heapq
import logging
import math
from collections import defaultdict

logger = logging.getLogger(__name__)

# Snap road segment endpoints within this distance (metres) into a single node.
_SNAP_M = 100

# Bridge endpoints across different connected components within this distance.
_BRIDGE_M = 600

# Weight multiplier for penalised roads when searching for alternatives.
_PENALTY = 1_000.0

# Cost multipliers by OSM highway type.
# Lower = preferred.  Motorway/trunk are ~1×, local roads ~10×.
_HIGHWAY_COST = {
    "motorway":       1.0,
    "motorway_link":  1.2,
    "trunk":          1.2,
    "trunk_link":     1.4,
    "primary":        1.5,
    "primary_link":   1.8,
    "secondary":      3.0,
    "secondary_link": 3.5,
    "tertiary":       6.0,
    "tertiary_link":  7.0,
}
_DEFAULT_COST = 10.0   # unclassified / residential / service / etc.


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6_371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _build_graph(features, snap_m=_SNAP_M, bridge_m=_BRIDGE_M):
    """
    Build an undirected road graph from GeoJSON features.

    Edge weight = haversine_km × highway_cost_multiplier, so Dijkstra
    naturally prefers motorways/trunks over local roads.

    Returns:
        node_coords : list of (lon, lat)
        adj         : dict  node_id -> [(neighbor_id, weight, feat_idx)]
        feat_ref    : dict  feat_idx -> ref_str
    """
    snap_deg = snap_m / 111_000.0
    node_coords = []
    node_map = {}
    adj = defaultdict(list)
    feat_ref = {}

    def get_node(lon, lat):
        key = (round(lon / snap_deg), round(lat / snap_deg))
        if key not in node_map:
            node_map[key] = len(node_coords)
            node_coords.append((lon, lat))
        return node_map[key]

    for idx, feat in enumerate(features):
        props = feat.get("properties") or {}
        ref = (props.get("ref") or "").strip()
        if ref:
            feat_ref[idx] = ref

        highway = (props.get("highway") or "").strip()
        cost_mult = _HIGHWAY_COST.get(highway, _DEFAULT_COST)

        geom = feat.get("geometry") or {}
        gtype = geom.get("type", "")
        if gtype == "LineString":
            lines = [geom.get("coordinates", [])]
        elif gtype == "MultiLineString":
            lines = geom.get("coordinates", [])
        else:
            continue

        for coords in lines:
            if len(coords) < 2:
                continue
            snapped = [get_node(c[0], c[1]) for c in coords]
            for i in range(len(snapped) - 1):
                n1, n2 = snapped[i], snapped[i + 1]
                if n1 == n2:
                    continue
                lon1, lat1 = node_coords[n1]
                lon2, lat2 = node_coords[n2]
                w = _haversine_km(lat1, lon1, lat2, lon2) * cost_mult
                adj[n1].append((n2, w, idx))
                adj[n2].append((n1, w, idx))

    n_nodes = len(node_coords)
    logger.info(
        "_build_graph: %d nodes from %d features", n_nodes, len(features)
    )

    if bridge_m > snap_m and n_nodes > 0:
        _bridge_components(node_coords, adj, bridge_m)

    return node_coords, adj, feat_ref


def _find_components(node_coords, adj):
    n = len(node_coords)
    comp_id = [-1] * n
    c = 0
    for start in range(n):
        if comp_id[start] >= 0:
            continue
        stack = [start]
        while stack:
            node = stack.pop()
            if comp_id[node] >= 0:
                continue
            comp_id[node] = c
            stack.extend(nb for nb, _w, _f in adj[node] if comp_id[nb] < 0)
        c += 1
    return comp_id


def _bridge_components(node_coords, adj, bridge_m):
    """Connect nodes in different components within bridge_m metres."""
    bridge_deg = bridge_m / 111_000.0
    comp_id = _find_components(node_coords, adj)

    grid = defaultdict(list)
    for nid, (lon, lat) in enumerate(node_coords):
        grid[(int(lon / bridge_deg), int(lat / bridge_deg))].append(nid)

    added = 0
    for nid, (lon, lat) in enumerate(node_coords):
        gx = int(lon / bridge_deg)
        gy = int(lat / bridge_deg)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((gx + dx, gy + dy), []):
                    if other <= nid or comp_id[other] == comp_id[nid]:
                        continue
                    o_lon, o_lat = node_coords[other]
                    d_km = _haversine_km(lat, lon, o_lat, o_lon)
                    if d_km * 1_000 <= bridge_m:
                        adj[nid].append((other, d_km, -1))
                        adj[other].append((nid, d_km, -1))
                        added += 1

    if added:
        logger.info("_bridge_components: added %d bridge edges", added)


# ---------------------------------------------------------------------------
# Dijkstra
# ---------------------------------------------------------------------------

def _dijkstra(adj, starts, end_set, feat_ref=None, penalised=None):
    """
    Multi-source Dijkstra.  Edges whose road ref is in *penalised* get
    weight × _PENALTY to push alternatives onto different named roads.

    Returns (dist_map, found_end_node_or_None).
    dist_map: node_id -> (dist, prev_node_id, feat_idx)
    """
    dist_map = {}
    pq = [(d, nid, -1, -1) for d, nid in starts]
    heapq.heapify(pq)
    while pq:
        d, cur, prev, fidx = heapq.heappop(pq)
        if cur in dist_map:
            continue
        dist_map[cur] = (d, prev, fidx)
        if cur in end_set:
            return dist_map, cur
        for nb, w, fidx2 in adj[cur]:
            if nb not in dist_map:
                ref2 = feat_ref.get(fidx2, "")
                if penalised and feat_ref and ref2 in penalised:
                    w = w * _PENALTY
                heapq.heappush(pq, (d + w, nb, cur, fidx2))
    return dist_map, None


def _extract_path_refs(dist_map, end_node, feat_ref):
    """Walk predecessor chain; return set of ref tags on the path."""
    refs = set()
    cur = end_node
    while cur is not None and cur != -1:
        _, prev, fidx = dist_map[cur]
        if fidx >= 0:
            ref = feat_ref.get(fidx, "")
            if ref:
                refs.add(ref)
        cur = prev if prev != -1 else None
    return refs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_p2p_roads(
    roads_geojson, site_pairs, proximity_km=10.0, n_alternatives=2
):
    """
    For each (site1, site2) pair find named roads (OSM ``ref`` groups) that
    form connecting paths through the road network.

    Higher-quality roads (motorway, trunk, primary) are strongly preferred
    over local roads via edge cost multipliers.  After finding the cheapest
    path, those refs are penalised and the search repeats to discover
    genuinely different route options.

    Returns:
        routes       — list of dicts: route_id, ref, road_name, pair_idx,
                        site1, site2, feature_indices, way_ids
        used_indices — set of feature indices used by any route
    """
    features = roads_geojson.get("features", [])
    if not features:
        return [], set()

    node_coords, adj, feat_ref = _build_graph(features)

    ref_to_indices = defaultdict(list)
    for idx, feat in enumerate(features):
        ref = (feat.get("properties") or {}).get("ref", "").strip()
        if ref:
            ref_to_indices[ref].append(idx)

    routes = []
    used_indices = set()
    route_counter = 0

    for pair_idx, (s1, s2) in enumerate(site_pairs):
        starts, ends = [], []
        for nid, (lon, lat) in enumerate(node_coords):
            d1 = _haversine_km(lat, lon, s1["lat"], s1["lon"])
            d2 = _haversine_km(lat, lon, s2["lat"], s2["lon"])
            if d1 < proximity_km:
                starts.append((d1, nid))
            if d2 < proximity_km:
                ends.append((d2, nid))

        if not starts or not ends:
            logger.warning(
                "Pair %s↔%s: no road nodes within %.1f km "
                "of one or both sites",
                s1["name"], s2["name"], proximity_km,
            )
            continue

        start_node_set = {nid for _, nid in starts}
        ends = [(d, nid) for d, nid in ends if nid not in start_node_set]
        if not ends:
            logger.warning(
                "Pair %s↔%s: all end nodes overlap with start nodes",
                s1["name"], s2["name"],
            )
            continue
        end_set = {nid for _, nid in ends}

        all_found_refs = set()
        penalised_refs = set()

        for _attempt in range(1 + n_alternatives):
            dist_map, found_end = _dijkstra(
                adj, starts, end_set,
                feat_ref=feat_ref, penalised=penalised_refs,
            )
            if found_end is None:
                break
            path_refs = _extract_path_refs(dist_map, found_end, feat_ref)
            new_refs = path_refs - all_found_refs
            if not new_refs:
                break
            all_found_refs |= new_refs
            penalised_refs |= path_refs

        if not all_found_refs:
            logger.warning(
                "Pair %s↔%s: no connected path found",
                s1["name"], s2["name"],
            )
            continue

        for ref in sorted(all_found_refs):
            all_idx = ref_to_indices.get(ref, [])
            if not all_idx:
                continue

            # For each feature compute min distance to each site.
            # A feature is "near site1" if any vertex is within proximity_km.
            # Only keep features that are near at least one of the two sites —
            # this clips the highway to the section between the sites and
            # drops fragments that happen to lie along a detour far away.
            # The ref is only emitted if the clipped set covers BOTH sites.
            near1 = set()
            near2 = set()
            for i in all_idx:
                geom = features[i].get("geometry") or {}
                coords = []
                if geom.get("type") == "LineString":
                    coords = geom.get("coordinates", [])
                elif geom.get("type") == "MultiLineString":
                    for line in geom.get("coordinates", []):
                        coords.extend(line)
                for c in coords:
                    if _haversine_km(c[1], c[0],
                                     s1["lat"], s1["lon"]) < proximity_km:
                        near1.add(i)
                    if _haversine_km(c[1], c[0],
                                     s2["lat"], s2["lon"]) < proximity_km:
                        near2.add(i)

            # Route only valid if road reaches both sites
            if not near1 or not near2:
                logger.debug(
                    "Ref %s skipped: not near both sites "
                    "(near1=%d, near2=%d)",
                    ref, len(near1), len(near2),
                )
                continue

            # Clip to features near either site
            idx_list = sorted(
                i for i in all_idx if i in near1 or i in near2
            )

            props0 = (features[idx_list[0]].get("properties") or {})
            name = props0.get("name", "") or ""
            way_ids = [
                (features[i].get("properties") or {}).get("osm_way_id")
                for i in idx_list
                if (features[i].get("properties") or {}).get(
                    "osm_way_id") is not None
            ]
            routes.append({
                "route_id":        f"route_{route_counter}",
                "ref":             ref,
                "road_name":       name or ref,
                "pair_idx":        pair_idx,
                "site1":           {
                    "name": s1["name"],
                    "lat": s1["lat"],
                    "lon": s1["lon"],
                },
                "site2":           {
                    "name": s2["name"],
                    "lat": s2["lat"],
                    "lon": s2["lon"],
                },
                "feature_indices": idx_list,
                "way_ids":         way_ids,
            })
            used_indices.update(idx_list)
            route_counter += 1

        logger.info(
            "Pair %d (%s↔%s): found refs %s",
            pair_idx, s1["name"], s2["name"], sorted(all_found_refs),
        )

    return routes, used_indices
