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
            "properties": {"ref": ref, "name": name, "osm_way_id": len(feats) + 1},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    return {"type": "FeatureCollection", "features": feats}


# Sites near 40°N 44°E
S_A = {"name": "A", "lat": 40.0, "lon": 44.0}
S_B = {"name": "B", "lat": 40.5, "lon": 44.5}
S_C = {"name": "C", "lat": 41.0, "lon": 44.0}


def test_single_road_connects_pair():
    # Road runs close to both A and B
    roads = _roads(("A-1", "Highway A1", [(44.0, 40.0), (44.5, 40.5)]))
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert len(routes) == 1
    assert routes[0]["ref"] == "A-1"
    assert routes[0]["pair_idx"] == 0
    assert 0 in used


def test_road_not_close_enough_excluded():
    # Road far from both sites (10°+ away)
    roads = _roads(("A-1", "", [(55.0, 50.0), (56.0, 51.0)]))
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert routes == []
    assert used == set()


def test_road_close_to_only_one_site_excluded():
    # Road near A but far from B
    roads = _roads(("A-1", "", [(44.0, 40.0), (44.01, 40.01)]))
    routes, used = find_p2p_roads(roads, [(S_A, S_B)], proximity_km=1.0)
    assert routes == []


def test_two_roads_both_connect_pair():
    roads = _roads(
        ("A-1",   "Road1", [(44.0, 40.0), (44.5, 40.5)]),
        ("AH-81", "Road2", [(44.0, 40.0), (44.5, 40.5)]),
    )
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert len(routes) == 2
    refs = {r["ref"] for r in routes}
    assert refs == {"A-1", "AH-81"}
    assert used == {0, 1}


def test_two_pairs_separate_routes():
    roads = _roads(
        ("A-1", "", [(44.0, 40.0), (44.5, 40.5)]),   # A↔B
        ("M3",  "", [(44.0, 40.0), (44.0, 41.0)]),   # A↔C
    )
    routes, _ = find_p2p_roads(roads, [(S_A, S_B), (S_A, S_C)])
    pair_route_map = {r["pair_idx"]: r["ref"] for r in routes}
    assert 0 in pair_route_map
    assert 1 in pair_route_map


def test_empty_roads_returns_no_routes():
    roads = {"type": "FeatureCollection", "features": []}
    routes, used = find_p2p_roads(roads, [(S_A, S_B)])
    assert routes == []
    assert used == set()


def test_route_contains_way_ids():
    roads = _roads(("A-1", "", [(44.0, 40.0), (44.5, 40.5)]))
    routes, _ = find_p2p_roads(roads, [(S_A, S_B)])
    assert routes[0]["way_ids"] == [1]


def test_proximity_km_respected():
    # Road is offset ~7 km from each site (> 1 km threshold, < 100 km threshold)
    roads = _roads(("A-1", "", [(44.05, 40.05), (44.45, 40.45)]))
    # tight 1 km threshold → should miss
    routes_miss, _ = find_p2p_roads(roads, [(S_A, S_B)], proximity_km=1.0)
    assert routes_miss == []
    # generous 100 km → should hit
    routes_hit, _ = find_p2p_roads(roads, [(S_A, S_B)], proximity_km=100.0)
    assert len(routes_hit) == 1
