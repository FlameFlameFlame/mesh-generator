"""P2P road route finder.

Uses NetworkX DiGraph + Yen's k-shortest paths + Jaccard diversity filter.
"""

import itertools
import logging
import math
from collections import defaultdict

import networkx as nx

logger = logging.getLogger(__name__)

# Snap road segment endpoints within this distance (metres) into a single node.
_SNAP_M = 100

# Bridge endpoints across different connected components within this distance.
_BRIDGE_M = 600

# Bridge nodes that share the same road ref (e.g. M-1) within this distance.
# This reconnects fragmented OSM highway designations (e.g. the 3.67 km gap
# in Armenian M-1) without creating spurious cross-road bridges.
_REF_BRIDGE_M = 5_000

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
# makes routing strongly avoid routes that stitch together many
# short local connectors (which would be cheap on distance alone).
# Value is in the same units as haversine_km × cost_mult (weighted-km).
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
# Biases the snap toward major roads so routing starts on a trunk/motorway
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

def _build_digraph(features, snap_m=_SNAP_M, bridge_m=_BRIDGE_M):
    """
    Build a directed road graph (nx.DiGraph) from GeoJSON features.

    Edge weight = haversine_km × highway_cost_multiplier + overhead, so
    routing naturally prefers motorways/trunks over local roads.

    Oneway OSM tags are respected:
      oneway=yes/true/1  → forward direction only
      oneway=-1          → reverse direction only
      (default)          → bidirectional

    Returns:
        node_coords  : list of (lon, lat)
        G            : nx.DiGraph with weight and feat_idx edge attributes
        feat_ref     : dict  feat_idx -> ref_str
        node_highway : dict  node_id -> best highway type string
        edge_to_feat : dict  (u, v) -> feat_idx
    """
    snap_deg = snap_m / 111_000.0
    node_coords = []
    node_map = {}
    G = nx.DiGraph()
    edge_to_feat = {}
    feat_ref = {}
    node_highway = {}  # node_id -> best (lowest _HIGHWAY_COST) highway type
    node_ref = {}      # node_id -> set of road refs (for ref-based bridging)

    def get_node(lon, lat):
        key = (round(lon / snap_deg), round(lat / snap_deg))
        if key not in node_map:
            nid = len(node_coords)
            node_map[key] = nid
            node_coords.append((lon, lat))
            G.add_node(nid)
        return node_map[key]

    def _add_directed_edge(u, v, w, idx):
        """Add directed edge u→v; keep cheaper weight if already exists."""
        if G.has_edge(u, v):
            if G[u][v]["weight"] > w:
                G[u][v]["weight"] = w
                G[u][v]["feat_idx"] = idx
                edge_to_feat[(u, v)] = idx
        else:
            G.add_edge(u, v, weight=w, feat_idx=idx)
            edge_to_feat[(u, v)] = idx

    for idx, feat in enumerate(features):
        props = feat.get("properties") or {}
        ref = (props.get("ref") or "").strip()
        if ref:
            feat_ref[idx] = ref

        highway = (props.get("highway") or "").strip()
        cost_mult = _HIGHWAY_COST.get(highway, _DEFAULT_COST)
        overhead = _HIGHWAY_OVERHEAD.get(highway, _DEFAULT_OVERHEAD)

        oneway_val = (props.get("oneway") or "").strip()
        oneway_fwd = oneway_val in ("yes", "true", "1")
        oneway_rev = oneway_val == "-1"

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
                w = (
                    _haversine_km(lat1, lon1, lat2, lon2) * cost_mult
                    + overhead
                )

                if oneway_rev:
                    _add_directed_edge(n2, n1, w, idx)
                elif oneway_fwd:
                    _add_directed_edge(n1, n2, w, idx)
                else:
                    _add_directed_edge(n1, n2, w, idx)
                    _add_directed_edge(n2, n1, w, idx)

                # Track best highway type per node (lower cost_mult = better)
                for nid in (n1, n2):
                    existing = node_highway.get(nid)
                    if (existing is None
                            or cost_mult < _HIGHWAY_COST.get(
                                existing, _DEFAULT_COST)):
                        node_highway[nid] = highway
                    if ref:
                        node_ref.setdefault(nid, set()).add(ref)

    n_nodes = G.number_of_nodes()
    logger.info(
        "_build_digraph: %d nodes, %d edges from %d features",
        n_nodes, G.number_of_edges(), len(features),
    )

    if bridge_m > snap_m and n_nodes > 0:
        _bridge_components(node_coords, G, bridge_m)
        if _REF_BRIDGE_M > bridge_m and node_ref:
            _bridge_components(
                node_coords, G, _REF_BRIDGE_M, node_ref=node_ref
            )

    return node_coords, G, feat_ref, node_highway, edge_to_feat


def _bridge_components(node_coords, G, bridge_m, node_ref=None):
    """Connect nodes in different weakly-connected components within bridge_m.

    If node_ref is provided (dict node_id -> set of ref strings), only bridge
    pairs that share at least one ref.  This reconnects fragmented highway
    designations without creating spurious cross-road shortcuts.

    Bridge edges are added as bidirectional pairs with weight=haversine
    distance and feat_idx=-1 (synthetic, no real GeoJSON geometry).
    """
    bridge_deg = bridge_m / 111_000.0

    # Build component map: node_id -> component_id
    comp_map = {}
    for comp_id, comp_nodes in enumerate(
            nx.weakly_connected_components(G)):
        for nid in comp_nodes:
            comp_map[nid] = comp_id

    # Spatial grid for fast neighbour lookup
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
                    if other <= nid:
                        continue
                    if comp_map.get(other) == comp_map.get(nid):
                        continue
                    if node_ref is not None:
                        refs_a = node_ref.get(nid)
                        refs_b = node_ref.get(other)
                        if (not refs_a or not refs_b
                                or refs_a.isdisjoint(refs_b)):
                            continue
                    o_lon, o_lat = node_coords[other]
                    d_km = _haversine_km(lat, lon, o_lat, o_lon)
                    if d_km * 1_000 <= bridge_m:
                        G.add_edge(nid, other, weight=d_km, feat_idx=-1)
                        G.add_edge(other, nid, weight=d_km, feat_idx=-1)
                        # Update component map so later pairs see merged comp
                        merged = comp_map[nid]
                        old = comp_map[other]
                        for n, c in comp_map.items():
                            if c == old:
                                comp_map[n] = merged
                        added += 1

    if added:
        label = "ref-bridge" if node_ref is not None else "bridge"
        logger.info(
            "_bridge_components(%s): added %d edge pairs (max %.0f m)",
            label, added, bridge_m,
        )


# ---------------------------------------------------------------------------
# Route-finding helpers
# ---------------------------------------------------------------------------

def _path_to_edge_set(path, edge_to_feat):
    """Return frozenset of feat_idx for edges along path (skip bridge edges)."""
    result = set()
    for u, v in zip(path, path[1:]):
        fi = edge_to_feat.get((u, v), -1)
        if fi >= 0:
            result.add(fi)
    return frozenset(result)


def _path_km(path, node_coords):
    """Total haversine distance along the node-list path in km."""
    total = 0.0
    for u, v in zip(path, path[1:]):
        lon1, lat1 = node_coords[u]
        lon2, lat2 = node_coords[v]
        total += _haversine_km(lat1, lon1, lat2, lon2)
    return total


def _jaccard_similarity(a, b):
    """Jaccard similarity between two frozensets."""
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union > 0 else 0.0


def _find_routes_for_pair(
    G, node_coords, feat_ref, edge_to_feat, features,
    source, target, site_dist_km, s1, s2, pair_idx,
    route_counter_start, max_candidates=10, max_routes=4,
    min_diversity=0.4, max_detour_factor=3.0,
):
    """
    Find up to max_routes diverse routes between source and target nodes.

    Uses Yen's k-shortest paths (via nx.shortest_simple_paths) as candidates,
    then applies a Jaccard diversity filter to return routes that genuinely
    use different road corridors.

    Returns (routes_list, new_route_counter).
    """
    routes = []
    route_counter = route_counter_start

    try:
        path_gen = nx.shortest_simple_paths(
            G, source, target, weight="weight"
        )
        candidates = list(itertools.islice(path_gen, max_candidates))
    except nx.NetworkXNoPath:
        logger.warning(
            "Pair %s\u2194%s: no path found in graph",
            s1["name"], s2["name"],
        )
        return [], route_counter
    except nx.NodeNotFound as e:
        logger.warning(
            "Pair %s\u2194%s: node not found \u2014 %s",
            s1["name"], s2["name"], e,
        )
        return [], route_counter

    selected_edge_sets = []  # frozensets for already-selected routes

    for path in candidates:
        if len(routes) >= max_routes:
            break

        # Detour check
        route_km = _path_km(path, node_coords)
        if site_dist_km > 0 and route_km > site_dist_km * max_detour_factor:
            logger.debug(
                "Pair %s\u2194%s: candidate too long "
                "(%.1f km > %.1f km limit), skipping",
                s1["name"], s2["name"],
                route_km, site_dist_km * max_detour_factor,
            )
            continue

        edge_set = _path_to_edge_set(path, edge_to_feat)
        if not edge_set:
            logger.debug(
                "Pair %s\u2194%s: candidate has empty edge set, skipping",
                s1["name"], s2["name"],
            )
            continue

        # Jaccard diversity: reject if too similar to any already-selected
        too_similar = any(
            _jaccard_similarity(edge_set, sel) >= (1.0 - min_diversity)
            for sel in selected_edge_sets
        )
        if too_similar:
            logger.debug(
                "Pair %s\u2194%s: candidate rejected by diversity filter",
                s1["name"], s2["name"],
            )
            continue

        selected_edge_sets.append(edge_set)

        # Accumulate ref_km for labelling
        feat_indices = sorted(fi for fi in edge_set if fi >= 0)
        ref_km = {}
        for u, v in zip(path, path[1:]):
            fi = edge_to_feat.get((u, v), -1)
            if fi < 0:
                continue
            ref = feat_ref.get(fi, "")
            lon1, lat1 = node_coords[u]
            lon2, lat2 = node_coords[v]
            km = _haversine_km(lat1, lon1, lat2, lon2)
            if ref:
                ref_km[ref] = ref_km.get(ref, 0.0) + km

        path_refs = set(ref_km.keys())
        total_km = sum(ref_km.values())

        # Label: only refs covering ≥5% of named-road distance
        label_refs = sorted(
            r for r, km in ref_km.items()
            if total_km == 0 or km / total_km >= 0.05
        )
        refs_label = (
            ", ".join(label_refs) if label_refs
            else ", ".join(sorted(path_refs)) if path_refs
            else "unnamed"
        )

        # Road name: prefer first named feature on the path
        road_name = ""
        for fi in feat_indices:
            n = (features[fi].get("properties") or {}).get("name", "") or ""
            if n:
                road_name = n
                break
        if not road_name:
            road_name = refs_label

        way_ids = [
            (features[fi].get("properties") or {}).get("osm_way_id")
            for fi in feat_indices
            if (features[fi].get("properties") or {}).get(
                "osm_way_id") is not None
        ]

        routes.append({
            "route_id":        f"route_{route_counter}",
            "refs":            sorted(path_refs),
            "ref":             refs_label,
            "road_name":       road_name,
            "pair_idx":        pair_idx,
            "site1":           {
                "name": s1["name"],
                "lat":  s1["lat"],
                "lon":  s1["lon"],
            },
            "site2":           {
                "name": s2["name"],
                "lat":  s2["lat"],
                "lon":  s2["lon"],
            },
            "feature_indices": feat_indices,
            "way_ids":         way_ids,
        })
        route_counter += 1

        logger.info(
            "Pair %d (%s\u2194%s) route %d: refs=%s, features=%d, km=%.1f",
            pair_idx, s1["name"], s2["name"],
            route_counter - 1, refs_label, len(feat_indices), route_km,
        )

    return routes, route_counter


# ---------------------------------------------------------------------------
# Snapping helpers (unchanged from original)
# ---------------------------------------------------------------------------

def _nearest_node_outside_boundary(
        node_coords, boundary_geojson, site_lat, site_lon,
        node_highway=None):
    """
    Return (dist_km, nid) for the road node outside the boundary polygon
    that is the best routing start/end point for this site.

    Selection: minimise distance_from_site × road_type_penalty.
    Major roads (trunk/motorway) get penalty 1.0; minor roads up to 12×.
    This picks the nearest outside-boundary major-road exit, not the node
    closest to the other site (which would cause start==end when both sites
    snap to the same node).

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
        return _nearest_node(node_coords, site_lat, site_lon)
    nlon, nlat = node_coords[best_nid]
    hw = (node_highway or {}).get(best_nid, "unknown")
    logger.debug(
        "_nearest_node_outside_boundary: nid=%d hw=%s dist=%.2f km",
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_p2p_roads(
    roads_geojson,
    site_pairs,
    n_alternatives=3,
    max_candidates=10,
    min_diversity=0.4,
    max_detour_factor=3.0,
):
    """
    For each (site1, site2) pair find up to n_alternatives road routes.

    Routes are found using Yen's k-shortest paths algorithm (via
    nx.shortest_simple_paths) and filtered by Jaccard edge-set diversity so
    that returned routes represent genuinely different road corridors rather
    than near-identical variants.

    Parameters
    ----------
    roads_geojson   : GeoJSON FeatureCollection of road segments
    site_pairs      : list of (site1_dict, site2_dict) pairs
    n_alternatives  : max routes to return per pair (default 3)
    max_candidates  : how many Yen's paths to generate per pair before
                      diversity filtering (default 10)
    min_diversity   : minimum fraction of edges that must differ between
                      any two selected routes — routes with Jaccard >=
                      (1 - min_diversity) are rejected as too similar
                      (default 0.4)
    max_detour_factor : routes longer than site_dist × factor are discarded
                        (default 3.0)

    Endpoint selection
    ------------------
    Sites with boundary_geojson: nearest road node OUTSIDE that boundary,
    biased toward major roads (motorway/trunk).
    Sites without boundary: absolute nearest road node to the pin.

    Returns
    -------
    routes       : list of dicts with keys route_id, refs, ref, road_name,
                   pair_idx, site1, site2, feature_indices, way_ids
    used_indices : set of feature indices used by any route
    """
    features = roads_geojson.get("features", [])
    if not features:
        return [], set()

    node_coords, G, feat_ref, node_highway, edge_to_feat = _build_digraph(
        features
    )

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

        _, src = start
        _, tgt = end

        logger.info(
            "Site %s \u2192 node %d (%.2f km away)",
            s1["name"], src, start[0],
        )
        logger.info(
            "Site %s \u2192 node %d (%.2f km away)",
            s2["name"], tgt, end[0],
        )

        if src == tgt:
            logger.warning(
                "Pair %s\u2194%s: start and end snap to same node",
                s1["name"], s2["name"],
            )
            continue

        pair_routes, route_counter = _find_routes_for_pair(
            G, node_coords, feat_ref, edge_to_feat, features,
            source=src, target=tgt,
            site_dist_km=site_dist_km,
            s1=s1, s2=s2,
            pair_idx=pair_idx,
            route_counter_start=route_counter,
            max_candidates=max_candidates,
            max_routes=n_alternatives,
            min_diversity=min_diversity,
            max_detour_factor=max_detour_factor,
        )

        if not pair_routes:
            logger.warning(
                "Pair %s\u2194%s: no connected path found",
                s1["name"], s2["name"],
            )
        else:
            routes.extend(pair_routes)
            for r in pair_routes:
                used_indices.update(r["feature_indices"])

    return routes, used_indices
