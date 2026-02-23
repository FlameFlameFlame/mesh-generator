"""Tests for find_p2p_roads proximity-based route finder."""
import pytest
from generator.graph import find_p2p_roads


# Helpers to build minimal GeoJSON FeatureCollections
def _roads(*lines):
    """lines: list of (ref, name, [(lon,lat)...]) tuples"""
    feats = []
    for ref, name, coords in lines:
        feats.append({
            "type": "Feature",
            "properties": {
                "ref": ref, "name": name, "osm_way_id": len(feats) + 1,
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    return {"type": "FeatureCollection", "features": feats}


# Sites near 40°N 44°E (~63 km apart)
S_A = {"name": "A", "lat": 40.0, "lon": 44.0}
S_B = {"name": "B", "lat": 40.5, "lon": 44.5}
S_C = {"name": "C", "lat": 41.0, "lon": 44.0}

# Sites very close together (~2.7 km apart).
# Auto-scaling: eff_proximity = max(prox, 2.7/3) ≈ max(prox, 0.9).
# With proximity_km=1.0 the effective threshold stays at 1.0 km.
S_NEAR1 = {"name": "N1", "lat": 40.00, "lon": 44.00}
S_NEAR2 = {"name": "N2", "lat": 40.02, "lon": 44.02}


def test_single_road_connects_pair():
    # Road runs close to both A and B
    roads = _roads(("A-1", "Highway A1", [(44.0, 40.0), (44.5, 40.5)]))
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert len(routes) == 1
    assert "A-1" in routes[0]["refs"]
    assert routes[0]["pair_idx"] == 0
    assert 0 in used


def test_road_not_close_enough_excluded():
    # Road far from both sites (10°+ away)
    roads = _roads(("A-1", "", [(55.0, 50.0), (56.0, 51.0)]))
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert routes == []
    assert used == set()


def test_road_close_to_only_one_site_excluded():
    # With nearest-node routing, both sites snap to the closest node on the
    # available road.  If both snap to the *same* node Dijkstra has no target
    # to reach and returns no routes.
    # Use a road far from both sites so both snap to the same endpoint.
    roads = _roads(("A-1", "", [(55.0, 50.0), (55.001, 50.001)]))
    routes, _ = find_p2p_roads(roads, [(S_A, S_B)])
    # start node == end node → no route
    assert routes == []


def test_two_roads_both_connect_pair():
    # Two parallel roads covering the same pair — each should appear in at
    # least one route (routes are path-based; both refs may share a path or
    # appear in separate alternative paths).
    roads = _roads(
        ("A-1",   "Road1", [(44.0, 40.0), (44.5, 40.5)]),
        ("AH-81", "Road2", [(44.0, 40.0), (44.5, 40.5)]),
    )
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert len(routes) >= 1
    all_refs = {ref for r in routes for ref in r["refs"]}
    assert "A-1" in all_refs or "AH-81" in all_refs
    assert used


def test_two_pairs_separate_routes():
    roads = _roads(
        ("A-1", "", [(44.0, 40.0), (44.5, 40.5)]),   # A↔B
        ("M3",  "", [(44.0, 40.0), (44.0, 41.0)]),   # A↔C
    )
    routes, _ = find_p2p_roads(roads, [(S_A, S_B), (S_A, S_C)])
    pair_indices = {r["pair_idx"] for r in routes}
    assert 0 in pair_indices
    assert 1 in pair_indices


def test_empty_roads_returns_no_routes():
    roads = {"type": "FeatureCollection", "features": []}
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert routes == []
    assert used == set()


def test_route_contains_way_ids():
    roads = _roads(("A-1", "", [(44.0, 40.0), (44.5, 40.5)]))
    routes, _ = find_p2p_roads(roads, [(S_A, S_B)])
    assert routes[0]["way_ids"] == [1]


def test_nearest_node_routing():
    # Routing always snaps to the nearest road node regardless of distance.
    # A road between S_A and S_B is found even when offset slightly.
    roads = _roads(("A-1", "", [(44.05, 40.05), (44.45, 40.45)]))
    routes, _ = find_p2p_roads(roads, [(S_A, S_B)])
    assert len(routes) == 1


def test_multi_highway_route():
    """Two refs needed to connect the pair — both appear on the single route."""
    roads = _roads(
        ("R1", "Road1", [(44.0, 40.0), (44.25, 40.25)]),
        ("R2", "Road2", [(44.25, 40.25), (44.5, 40.5)]),
    )
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert len(routes) == 1
    assert set(routes[0]["refs"]) == {"R1", "R2"}
    assert used == {0, 1}
