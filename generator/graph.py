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

# Bridge nodes that share the same road ref (e.g. M-1) within this distance.
# This reconnects fragmented OSM highway designations (e.g. the 3.67 km gap
# in Armenian M-1) without creating spurious cross-road bridges.
_REF_BRIDGE_M = 5_000

# Weight multiplier for penalised roads when searching for alternatives.
_PENALTY = 1_000.0

# Cost multipliers by OSM highway type (applied to haversine distance).
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

# Fixed overhead added to every edge regardless of length.
# Represents the "cost" of using a road of that class at all —
# makes Dijkstra strongly avoid routes that stitch together many
# short local connectors (which would be cheap on distance alone).
# Value is in the same units as haversine_km × cost_mult (weighted-km).
# 0.5 weighted-km overhead ≈ the cost of 500 m on a motorway.
_HIGHWAY_OVERHEAD = {
    "motorway":       0.0,
    "motorway_link":  0.1,
    "trunk":          0.0,
    "trunk_link":     0.1,
    "primary":        0.1,
    "primary_link":   0.2,
    "secondary":      0.5,
    "secondary_link": 0.6,
    "tertiary":       1.0,
    "tertiary_link":  1.2,
}
_DEFAULT_OVERHEAD = 2.0  # residential / unclassified / service

# Penalty multipliers applied to distance when selecting boundary exit node.
# Biases the snap toward major roads so Dijkstra starts on a trunk/motorway
# rather than a nearby tertiary road that happens to be slightly closer.
_SNAP_HIGHWAY_PENALTY = {
    "motorway":       1.0,
    "motorway_link":  1.0,
    "trunk":          1.0,
    "trunk_link":     1.1,
    "primary":        1.5,
    "primary_link":   2.0,
    "secondary":      4.0,
    "secondary_link": 5.0,
    "tertiary":       8.0,
    "tertiary_link":  9.0,
}
_DEFAULT_SNAP_PENALTY = 12.0  # unclassified / residential / service


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
        node_coords  : list of (lon, lat)
        adj          : dict  node_id -> [(neighbor_id, weight, feat_idx)]
        feat_ref     : dict  feat_idx -> ref_str
        node_highway : dict  node_id -> best highway type string
    """
    snap_deg = snap_m / 111_000.0
    node_coords = []
    node_map = {}
    adj = defaultdict(list)
    feat_ref = {}
    node_highway = {}  # node_id -> best (lowest _HIGHWAY_COST) highway type
    node_ref = {}      # node_id -> set of road refs (for ref-based bridging)

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
                overhead = _HIGHWAY_OVERHEAD.get(highway, _DEFAULT_OVERHEAD)
                w = (_haversine_km(lat1, lon1, lat2, lon2) * cost_mult
                     + overhead)
                adj[n1].append((n2, w, idx))
                adj[n2].append((n1, w, idx))
                # Track best highway type per node (lower cost_mult = better)
                for nid in (n1, n2):
                    existing = node_highway.get(nid)
                    if existing is None or cost_mult < _HIGHWAY_COST.get(existing, _DEFAULT_COST):
                        node_highway[nid] = highway
                    if ref:
                        if nid not in node_ref:
                            node_ref[nid] = set()
                        node_ref[nid].add(ref)

    n_nodes = len(node_coords)
    logger.info(
        "_build_graph: %d nodes from %d features", n_nodes, len(features)
    )

    if bridge_m > snap_m and n_nodes > 0:
        _bridge_components(node_coords, adj, bridge_m)
        # Second pass: bridge nodes that share a road ref across larger gaps.
        # This reconnects fragmented highway designations (e.g. M-1 in OSM
        # has a 3.67 km gap) without creating spurious cross-road shortcuts.
        if _REF_BRIDGE_M > bridge_m and node_ref:
            _bridge_components(node_coords, adj, _REF_BRIDGE_M,
                               node_ref=node_ref)

    return node_coords, adj, feat_ref, node_highway


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


def _bridge_components(node_coords, adj, bridge_m, node_ref=None):
    """Connect nodes in different components within bridge_m metres.

    If node_ref is provided (dict node_id -> set of ref strings), only bridge
    pairs that share at least one ref.  This reconnects fragmented highway
    designations without creating spurious cross-road shortcuts.
    """
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
                    if node_ref is not None:
                        refs_a = node_ref.get(nid)
                        refs_b = node_ref.get(other)
                        if not refs_a or not refs_b or refs_a.isdisjoint(refs_b):
                            continue
                    o_lon, o_lat = node_coords[other]
                    d_km = _haversine_km(lat, lon, o_lat, o_lon)
                    if d_km * 1_000 <= bridge_m:
                        adj[nid].append((other, d_km, -1))
                        adj[other].append((nid, d_km, -1))
                        added += 1

    if added:
        label = "ref-bridge" if node_ref is not None else "bridge"
        logger.info("_bridge_components(%s): added %d edges (max %.0f m)",
                    label, added, bridge_m)


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


def _extract_path(dist_map, end_node, feat_ref, node_coords):
    """
    Walk predecessor chain; return:
      refs         — set of ref tags on the path
      feat_indices — set of feature indices on the path
      ref_km       — dict ref -> total haversine km on path for that ref
    """
    refs = set()
    feat_indices = set()
    ref_km: dict = {}
    cur = end_node
    while cur is not None and cur != -1:
        _, prev, fidx = dist_map[cur]
        if fidx >= 0 and prev != -1:
            feat_indices.add(fidx)
            ref = feat_ref.get(fidx, "")
            if ref:
                refs.add(ref)
                # Accumulate raw km for this edge
                lon1, lat1 = node_coords[cur]
                lon2, lat2 = node_coords[prev]
                km = _haversine_km(lat1, lon1, lat2, lon2)
                ref_km[ref] = ref_km.get(ref, 0.0) + km
        cur = prev if prev != -1 else None
    return refs, feat_indices, ref_km


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _nearest_node_outside_boundary(
        node_coords, boundary_geojson, site_lat, site_lon,
        node_highway=None):
    """
    Return (dist_km, nid) for the road node that lies outside the boundary
    polygon and is the best Dijkstra start/end point for this site.

    Selection: minimise distance_from_site × road_type_penalty.
    Major roads (trunk/motorway) get penalty 1.0; minor roads get up to 12×.
    This picks the nearest outside-boundary major-road exit, not the node
    closest to the other site (which would select nodes near the destination
    and cause start==end when both sites snap to the same node).

    Falls back to absolute nearest node if shapely is unavailable or all
    nodes are inside the boundary.
    """
    try:
        from shapely.geometry import Point, shape
        poly = shape(boundary_geojson)
    except Exception:
        return _nearest_node(node_coords, site_lat, site_lon)

    best_score = float("inf")
    best_nid = 0
    found_any = False
    for nid, (lon, lat) in enumerate(node_coords):
        if poly.contains(Point(lon, lat)):
            continue
        found_any = True
        d = _haversine_km(site_lat, site_lon, lat, lon)
        hw = (node_highway or {}).get(nid, "")
        penalty = _SNAP_HIGHWAY_PENALTY.get(hw, _DEFAULT_SNAP_PENALTY)
        score = d * penalty
        if score < best_score:
            best_score = score
            best_nid = nid

    if not found_any:
        # All nodes inside boundary — fall back to absolute nearest
        return _nearest_node(node_coords, site_lat, site_lon)
    nlon, nlat = node_coords[best_nid]
    hw = (node_highway or {}).get(best_nid, "unknown")
    logger.debug(
        "_nearest_node_outside_boundary: nid=%d hw=%s dist_from_site=%.2f km",
        best_nid, hw,
        _haversine_km(site_lat, site_lon, nlat, nlon),
    )
    return _haversine_km(site_lat, site_lon, nlat, nlon), best_nid


def _nearest_node(node_coords, lat, lon):
    """Return (dist_km, nid) for the single closest road node to (lat, lon)."""
    best_d = float("inf")
    best_nid = 0
    for nid, (nlon, nlat) in enumerate(node_coords):
        d = _haversine_km(lat, lon, nlat, nlon)
        if d < best_d:
            best_d = d
            best_nid = nid
    return best_d, best_nid


def find_p2p_roads(
    roads_geojson, site_pairs, n_alternatives=1
):
    """
    For each (site1, site2) pair find road routes connecting the sites.

    Each route is a complete Dijkstra path from site1 to site2.  The first
    run finds the best (cheapest) path; subsequent runs penalise all refs
    from prior paths so Dijkstra is forced onto genuinely different roads.

    Endpoint selection:
      - Site with ``boundary_geojson``: nearest road node outside that
        site's own boundary polygon (so Dijkstra starts outside the city).
      - Site without boundary: absolute nearest road node to the pin.
      Other cities along the route are irrelevant and not filtered.

    Returns:
        routes       — list of dicts: route_id, refs, road_name, pair_idx,
                        site1, site2, feature_indices, way_ids
        used_indices — set of feature indices used by any route
    """
    features = roads_geojson.get("features", [])
    if not features:
        return [], set()

    node_coords, adj, feat_ref, node_highway = _build_graph(features)

    routes = []
    used_indices = set()
    route_counter = 0

    for pair_idx, (s1, s2) in enumerate(site_pairs):
        site_dist_km = _haversine_km(
            s1["lat"], s1["lon"], s2["lat"], s2["lon"]
        )
        logger.info(
            "Pair %s\u2194%s: dist=%.1f km",
            s1["name"], s2["name"], site_dist_km,
        )

        # Dijkstra source/target nodes.
        # Sites with a boundary: snap to nearest node OUTSIDE their own
        # boundary so Dijkstra doesn't start inside the city.
        # Sites without a boundary: snap to absolute nearest node.
        # Intermediate/other cities are completely ignored.
        if s1.get("boundary_geojson"):
            start = _nearest_node_outside_boundary(
                node_coords, s1["boundary_geojson"],
                s1["lat"], s1["lon"],
                node_highway=node_highway)
        else:
            start = _nearest_node(node_coords, s1["lat"], s1["lon"])

        if s2.get("boundary_geojson"):
            end = _nearest_node_outside_boundary(
                node_coords, s2["boundary_geojson"],
                s2["lat"], s2["lon"],
                node_highway=node_highway)
        else:
            end = _nearest_node(node_coords, s2["lat"], s2["lon"])

        logger.info(
            "Site %s → node %d (%.2f km away)",
            s1["name"], start[1], start[0],
        )
        logger.info(
            "Site %s → node %d (%.2f km away)",
            s2["name"], end[1], end[0],
        )

        starts = [start]
        ends = [end]

        if start[1] == end[1]:
            logger.warning(
                "Pair %s↔%s: start and end snap to same node",
                s1["name"], s2["name"],
            )
            continue
        end_set = {nid for _, nid in ends}

        all_found_ref_sets = []   # list of frozenset, one per route found
        penalised_refs = set()

        for _attempt in range(1 + n_alternatives):
            dist_map, found_end = _dijkstra(
                adj, starts, end_set,
                feat_ref=feat_ref, penalised=penalised_refs,
            )
            if found_end is None:
                break

            path_refs, path_feat_indices, ref_km = _extract_path(
                dist_map, found_end, feat_ref, node_coords
            )

            # Skip if this ref-set is identical to an already-found route
            ref_key = frozenset(path_refs)
            if ref_key in all_found_ref_sets:
                break
            all_found_ref_sets.append(ref_key)

            total_km = sum(ref_km.values())

            # Penalise only refs that account for ≥10% of the named-road
            # distance on this path.  These are the "backbone" refs.
            # Refs with small km share are city-entry connectors that the
            # next route will also need — penalising them would block M-1
            # just because it shares a short junction segment with M-3.
            dominant_refs = {
                r for r, km in ref_km.items()
                if total_km == 0 or km / total_km >= 0.25
            }
            penalised_refs |= dominant_refs

            # path_feat_indices are the actual Dijkstra-traversed features.
            # Drop only bridge edges (feat_idx -1 = synthetic gap-closing
            # edges with no real GeoJSON geometry).
            # Features in the middle of a long highway (far from both
            # endpoints) are legitimately on the route, so we keep them all.
            if not path_feat_indices:
                logger.debug(
                    "Route %d: empty path features", route_counter,
                )
                continue

            idx_list = sorted(path_feat_indices)

            # Sanity check: discard routes whose actual traversed distance is
            # more than 3× the straight-line site-to-site distance.
            # Measure by walking the predecessor chain (actual Dijkstra path),
            # NOT by summing up feature geometries (which can be much longer
            # since a highway feature spans many kilometres).
            route_km = 0.0
            cur = found_end
            while cur is not None and cur != -1:
                dist_entry = dist_map[cur]
                prev = dist_entry[1]
                if prev != -1:
                    c_lon, c_lat = node_coords[cur]
                    p_lon, p_lat = node_coords[prev]
                    route_km += _haversine_km(c_lat, c_lon, p_lat, p_lon)
                cur = prev if prev != -1 else None
            max_detour_km = site_dist_km * 3.0
            if site_dist_km > 0 and route_km > max_detour_km:
                logger.info(
                    "Pair %d (%s↔%s) attempt %d: route too long "
                    "(%.1f km > %.1f km limit), discarding",
                    pair_idx, s1["name"], s2["name"],
                    _attempt, route_km, max_detour_km,
                )
                continue

            # Label: only refs covering ≥5% of named-road distance.
            label_refs = sorted(
                r for r, km in ref_km.items()
                if total_km == 0 or km / total_km >= 0.05
            )
            refs_label = (
                ", ".join(label_refs) if label_refs
                else ", ".join(sorted(path_refs)) if path_refs
                else "unnamed"
            )

            # Road name: prefer the name of the first named feature on the path
            road_name = ""
            for i in idx_list:
                n = (features[i].get("properties") or {}).get("name", "") or ""
                if n:
                    road_name = n
                    break
            if not road_name:
                road_name = refs_label

            way_ids = [
                (features[i].get("properties") or {}).get("osm_way_id")
                for i in idx_list
                if (features[i].get("properties") or {}).get(
                    "osm_way_id") is not None
            ]

            routes.append({
                "route_id":        f"route_{route_counter}",
                "refs":            sorted(path_refs),
                "ref":             refs_label,   # kept for UI compatibility
                "road_name":       road_name,
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
                "Pair %d (%s↔%s) route %d: refs=%s, features=%d",
                pair_idx, s1["name"], s2["name"],
                route_counter - 1, refs_label, len(idx_list),
            )

        if not routes or all(r["pair_idx"] != pair_idx for r in routes):
            logger.warning(
                "Pair %s↔%s: no connected path found",
                s1["name"], s2["name"],
            )

    return routes, used_indices
