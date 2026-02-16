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


def build_road_graph(roads_geojson: dict) -> nx.Graph:
    """Build a NetworkX graph from GeoJSON road features.

    Nodes are (lon, lat) tuples.  Edges connect consecutive coordinates
    within each road, weighted by haversine distance.  Each edge stores
    ``feature_idx`` — the index of the originating road feature.
    """
    G = nx.Graph()
    features = roads_geojson.get("features", [])

    for idx, feat in enumerate(features):
        coords = feat["geometry"]["coordinates"]
        for i in range(len(coords) - 1):
            n1 = tuple(coords[i])
            n2 = tuple(coords[i + 1])
            dist = _haversine(n1[0], n1[1], n2[0], n2[1])
            # Keep shorter edge if duplicate (two roads sharing a segment)
            if G.has_edge(n1, n2):
                if dist < G[n1][n2]["distance"]:
                    G[n1][n2]["distance"] = dist
                    G[n1][n2]["feature_idx"] = idx
            else:
                G.add_edge(n1, n2, distance=dist, feature_idx=idx)

    logger.info("Road graph: %d nodes, %d edges", G.number_of_nodes(),
                G.number_of_edges())
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


def collect_path_edges(path: list) -> set[tuple]:
    """Extract set of undirected edges from a node path."""
    edges = set()
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        edges.add((a, b) if a <= b else (b, a))
    return edges


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
