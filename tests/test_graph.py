"""Tests for generator.graph — road graph construction and routing."""

from generator.graph import (
    build_road_graph,
    collect_path_edges,
    filter_roads_to_edges,
    find_nearest_node,
    shortest_path,
)


def _make_roads(*segments):
    """Helper: build a GeoJSON FeatureCollection from coordinate lists."""
    features = []
    for coords in segments:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {},
        })
    return {"type": "FeatureCollection", "features": features}


# --- Two-point road:  A ------ B
SIMPLE = _make_roads([[0.0, 0.0], [1.0, 0.0]])

# --- Triangle:  A--B--C--A  (three separate roads)
TRIANGLE = _make_roads(
    [[0.0, 0.0], [1.0, 0.0]],          # A-B
    [[1.0, 0.0], [0.5, 0.866]],        # B-C
    [[0.5, 0.866], [0.0, 0.0]],        # C-A
)

# --- Fork:  A--B--C  and  B--D  (B is junction)
FORK = _make_roads(
    [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]],  # A-B-C
    [[1.0, 0.0], [1.0, 1.0]],                # B-D
)


class TestBuildRoadGraph:
    def test_simple_nodes_and_edges(self):
        G = build_road_graph(SIMPLE)
        assert G.number_of_nodes() == 2
        assert G.number_of_edges() == 1

    def test_triangle_nodes_and_edges(self):
        G = build_road_graph(TRIANGLE)
        assert G.number_of_nodes() == 3
        assert G.number_of_edges() == 3

    def test_fork_junction(self):
        G = build_road_graph(FORK)
        # A(0,0) B(1,0) C(2,0) D(1,1) — 4 nodes, 3 edges
        assert G.number_of_nodes() == 4
        assert G.number_of_edges() == 3

    def test_edge_has_distance(self):
        G = build_road_graph(SIMPLE)
        a, b = (0.0, 0.0), (1.0, 0.0)
        assert G[a][b]["distance"] > 0

    def test_empty_roads(self):
        G = build_road_graph({"type": "FeatureCollection", "features": []})
        assert G.number_of_nodes() == 0


class TestFindNearestNode:
    def test_exact_match(self):
        G = build_road_graph(FORK)
        node = find_nearest_node(G, lat=0.0, lon=0.0)
        assert node == (0.0, 0.0)

    def test_close_point(self):
        G = build_road_graph(FORK)
        node = find_nearest_node(G, lat=0.01, lon=0.99)
        assert node == (1.0, 0.0)


class TestShortestPath:
    def test_direct_path(self):
        G = build_road_graph(SIMPLE)
        path = shortest_path(G, (0.0, 0.0), (1.0, 0.0))
        assert path == [(0.0, 0.0), (1.0, 0.0)]

    def test_multi_hop_path(self):
        G = build_road_graph(FORK)
        path = shortest_path(G, (0.0, 0.0), (1.0, 1.0))
        assert path is not None
        assert path[0] == (0.0, 0.0)
        assert path[-1] == (1.0, 1.0)
        assert (1.0, 0.0) in path  # must go through junction

    def test_no_path(self):
        # Two disconnected roads
        roads = _make_roads(
            [[0.0, 0.0], [1.0, 0.0]],
            [[10.0, 10.0], [11.0, 10.0]],
        )
        G = build_road_graph(roads)
        path = shortest_path(G, (0.0, 0.0), (10.0, 10.0))
        assert path is None


class TestCollectPathEdges:
    def test_single_edge(self):
        edges = collect_path_edges([(0.0, 0.0), (1.0, 0.0)])
        assert len(edges) == 1

    def test_multi_edge(self):
        edges = collect_path_edges([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
        assert len(edges) == 2

    def test_normalized_order(self):
        # Same path in both directions should yield same edge set
        fwd = collect_path_edges([(0.0, 0.0), (1.0, 0.0)])
        rev = collect_path_edges([(1.0, 0.0), (0.0, 0.0)])
        assert fwd == rev


class TestFilterRoadsToEdges:
    def test_keep_matching_feature(self):
        edges = {((0.0, 0.0), (1.0, 0.0))}
        result = filter_roads_to_edges(TRIANGLE, edges)
        assert len(result["features"]) == 1

    def test_keep_none_if_no_match(self):
        edges = {((99.0, 99.0), (100.0, 100.0))}
        result = filter_roads_to_edges(TRIANGLE, edges)
        assert len(result["features"]) == 0

    def test_fork_filter_to_branch(self):
        # Only keep edges on the B-D branch
        edges = {((1.0, 0.0), (1.0, 1.0))}
        result = filter_roads_to_edges(FORK, edges)
        # The B-D road is the second feature
        assert len(result["features"]) == 1
