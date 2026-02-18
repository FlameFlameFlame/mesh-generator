"""Road graph construction and shortest-path routing from OSM GeoJSON."""

import logging
from math import atan2, cos, radians, sin, sqrt

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

logger = logging.getLogger(__name__)


def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Haversine distance in meters between two (lon, lat) points."""
    R = 6_371_000
    la1, la2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(la1) * cos(la2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# Cost multipliers by road class — lower = preferred for routing.
# Dijkstra will strongly prefer major roads, using minor ones only
# when no major-road alternative exists.
HIGHWAY_COST_MULTIPLIER = {
    "motorway": 1.0,
    "trunk": 1.0,
    "primary": 1.2,
    "secondary": 3.0,
    "tertiary": 5.0,
}
_DEFAULT_COST_MULTIPLIER = 8.0


def build_road_graph(
    roads_geojson: dict,
    prefer_major: bool = False,
) -> nx.Graph:
    """Build a NetworkX graph from GeoJSON road features.

    Nodes are (lon, lat) tuples.  Edges connect consecutive coordinates
    within each road, weighted by haversine distance.  Each edge stores
    ``feature_idx`` — the index of the originating road feature.

    If *prefer_major* is True, edge weights are scaled by a road-class
    multiplier so that Dijkstra strongly favours highways/trunks over
    secondary/tertiary roads.
    """
    G = nx.Graph()
    features = roads_geojson.get("features", [])

    for idx, feat in enumerate(features):
        hw = feat.get("properties", {}).get("highway", "")
        multiplier = (HIGHWAY_COST_MULTIPLIER.get(hw, _DEFAULT_COST_MULTIPLIER)
                      if prefer_major else 1.0)
        coords = feat["geometry"]["coordinates"]
        for i in range(len(coords) - 1):
            n1 = tuple(coords[i])
            n2 = tuple(coords[i + 1])
            dist = _haversine(n1[0], n1[1], n2[0], n2[1])
            cost = dist * multiplier
            # Keep shorter edge if duplicate (two roads sharing a segment)
            if G.has_edge(n1, n2):
                if cost < G[n1][n2]["distance"]:
                    G[n1][n2]["distance"] = cost
                    G[n1][n2]["feature_idx"] = idx
            else:
                G.add_edge(n1, n2, distance=cost, feature_idx=idx)

    logger.info("Road graph: %d nodes, %d edges (prefer_major=%s)",
                G.number_of_nodes(), G.number_of_edges(), prefer_major)
    return G


def build_node_index(graph: nx.Graph):
    """Build a KDTree spatial index for fast nearest-node lookups.

    Returns ``(tree, node_list)`` where *tree* is a ``cKDTree`` of
    approximate Cartesian coordinates and *node_list* maps tree indices
    back to ``(lon, lat)`` graph nodes.
    """
    nodes = list(graph.nodes)
    if not nodes:
        return None, []
    # Convert (lon, lat) to approximate Cartesian for KDTree
    coords = np.array(nodes, dtype=np.float64)
    lons_rad = np.radians(coords[:, 0])
    lats_rad = np.radians(coords[:, 1])
    cos_lat = np.cos(lats_rad)
    # Equirectangular projection (good enough for nearest-neighbor)
    xy = np.column_stack([lons_rad * cos_lat, lats_rad])
    tree = cKDTree(xy)
    return tree, nodes


def find_nearest_node(graph: nx.Graph, lat: float, lon: float,
                      _index=None):
    """Find the graph node closest to (lat, lon).  Returns (lon, lat) tuple.

    Pass ``_index=(tree, node_list)`` from :func:`build_node_index` to
    avoid O(n) brute-force search.
    """
    if _index is not None:
        tree, node_list = _index
        if tree is None:
            return None
        lon_rad = radians(lon)
        lat_rad = radians(lat)
        cos_lat = cos(lat_rad)
        _, idx = tree.query([lon_rad * cos_lat, lat_rad])
        return node_list[idx]
    # Fallback brute-force for backward compat
    best = None
    best_dist = float("inf")
    for node in graph.nodes:
        d = _haversine(lon, lat, node[0], node[1])
        if d < best_dist:
            best_dist = d
            best = node
    return best


def shortest_path(graph: nx.Graph, node1, node2) -> list | None:
    """Dijkstra shortest path.  Returns list of nodes or None."""
    try:
        return nx.dijkstra_path(graph, node1, node2, weight="distance")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def k_shortest_paths(
    graph: nx.Graph, node1, node2, k: int = 3,
) -> list[list]:
    """Return up to *k* shortest simple paths between two nodes.

    Uses NetworkX's Yen's algorithm implementation.  Returns a list of
    node-lists (may be shorter than *k* if fewer paths exist).
    """
    try:
        paths = []
        for path in nx.shortest_simple_paths(graph, node1, node2,
                                             weight="distance"):
            paths.append(path)
            if len(paths) >= k:
                break
        return paths
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def k_penalty_paths(
    graph: nx.Graph, node1, node2, k: int = 3,
    penalty: float = 100.0,
) -> list[list]:
    """Return up to *k* diverse paths using feature-level penalty.

    After finding each shortest path, multiplies the weight of ALL edges
    belonging to every OSM feature (road way) used by that path by
    *penalty*.  This forces subsequent Dijkstra calls onto different
    roads where alternatives exist, while still allowing reuse of shared
    segments when no alternative corridor is available.

    Paths whose feature sets overlap more than 80% with an already-found
    path are skipped (deduplication).

    Restores original weights before returning.
    """
    paths = []
    path_features: list[set[int]] = []
    saved = {}  # (u, v) -> original weight

    # Build feature_idx -> list of edges for fast lookup
    feat_edges: dict[int, list[tuple]] = {}
    for u, v, data in graph.edges(data=True):
        fidx = data.get("feature_idx")
        if fidx is not None:
            feat_edges.setdefault(fidx, []).append((u, v))

    max_attempts = k * 3  # try more times to find diverse paths
    attempts = 0
    try:
        for _ in range(max_attempts):
            attempts = _ + 1
            if len(paths) >= k:
                break
            try:
                p = nx.dijkstra_path(graph, node1, node2,
                                     weight="distance")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                break
            # Collect features used by this path
            used_features = set()
            for i in range(len(p) - 1):
                fidx = graph[p[i]][p[i + 1]].get("feature_idx")
                if fidx is not None:
                    used_features.add(fidx)
            # Skip if >80% overlap with any existing path
            is_dup = False
            for prev_feats in path_features:
                overlap = len(used_features & prev_feats)
                bigger = max(len(used_features), len(prev_feats))
                if bigger > 0 and overlap / bigger > 0.8:
                    is_dup = True
                    break
            if not is_dup:
                paths.append(p)
                path_features.append(used_features)
            # Penalise ALL edges of each used feature regardless
            for fidx in used_features:
                for u, v in feat_edges.get(fidx, []):
                    if (u, v) not in saved:
                        saved[(u, v)] = graph[u][v]["distance"]
                    graph[u][v]["distance"] *= penalty
    finally:
        for (u, v), w in saved.items():
            graph[u][v]["distance"] = w

    logger.info("k_penalty_paths: %d diverse paths found "
                "(from %d attempts)", len(paths), attempts)
    return paths


def collect_path_edges(path: list) -> set[tuple]:
    """Extract set of undirected edges from a node path."""
    edges = set()
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        edges.add((a, b) if a <= b else (b, a))
    return edges


def collect_path_feature_indices(
    graph: nx.Graph, path: list,
) -> set[int]:
    """Collect the set of feature indices used by a path through the graph."""
    indices = set()
    for i in range(len(path) - 1):
        edge_data = graph.get_edge_data(path[i], path[i + 1])
        if edge_data and "feature_idx" in edge_data:
            indices.add(edge_data["feature_idx"])
    return indices


def filter_roads_by_feature_indices(
    roads_geojson: dict,
    used_indices: set[int],
) -> dict:
    """Keep only road features whose index is in *used_indices*.

    Returns a new GeoJSON FeatureCollection.
    """
    features = roads_geojson.get("features", [])
    kept = [f for i, f in enumerate(features) if i in used_indices]

    logger.info("Filtered roads: %d / %d features kept",
                len(kept), len(features))
    return {"type": "FeatureCollection", "features": kept}


def filter_roads_to_edges(
    roads_geojson: dict,
    used_edges: set[tuple],
) -> dict:
    """Keep only road features that have at least one segment in *used_edges*.

    Returns a new GeoJSON FeatureCollection.
    """
    kept = []
    for feat in roads_geojson.get("features", []):
        coords = feat["geometry"]["coordinates"]
        for i in range(len(coords) - 1):
            a = tuple(coords[i])
            b = tuple(coords[i + 1])
            edge = (a, b) if a <= b else (b, a)
            if edge in used_edges:
                kept.append(feat)
                break

    logger.info("Filtered roads: %d / %d features kept",
                len(kept), len(roads_geojson.get("features", [])))
    return {"type": "FeatureCollection", "features": kept}
