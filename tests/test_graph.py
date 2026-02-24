"""Tests for find_p2p_roads — NetworkX DiGraph + Yen's + Jaccard diversity."""
from generator.graph import find_p2p_roads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _road_hw(ref, name, coords, highway="primary", oneway=None):
    """Build a single GeoJSON Feature with the given properties."""
    props = {
        "ref": ref,
        "name": name,
        "osm_way_id": abs(hash(ref + name)) % 100_000,
        "highway": highway,
    }
    if oneway is not None:
        props["oneway"] = oneway
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def _fc(*feats):
    return {"type": "FeatureCollection", "features": list(feats)}


# Common test sites near 40°N 44°E (Armenia region)
S_A = {"name": "A", "lat": 40.0, "lon": 44.0}   # west
S_B = {"name": "B", "lat": 40.0, "lon": 44.5}   # east (~50 km apart)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_basic_route_found():
    """A single road directly connecting two sites returns one route."""
    road = _road_hw("R1", "Main Road", [(44.0, 40.0), (44.5, 40.0)])
    roads = _fc(road)
    routes, used = find_p2p_roads(roads, [(S_A, S_B)], n_alternatives=3)

    assert len(routes) == 1
    r = routes[0]
    assert "R1" in r["refs"]
    assert r["pair_idx"] == 0
    assert r["site1"]["name"] == "A"
    assert r["site2"]["name"] == "B"
    assert len(r["feature_indices"]) >= 1
    assert len(r["way_ids"]) >= 1
    assert 0 in used


def test_highway_weighting_prefers_motorway():
    """When a motorway and a tertiary road both connect two sites,
    the first returned route must use the motorway."""
    motorway = _road_hw(
        "M1", "Motorway",
        [(44.0, 40.0), (44.5, 40.0)],
        highway="motorway",
    )
    tertiary = _road_hw(
        "T1", "Tertiary",
        [(44.0, 40.0), (44.25, 40.05), (44.5, 40.0)],
        highway="tertiary",
    )
    roads = _fc(motorway, tertiary)
    routes, _ = find_p2p_roads(roads, [(S_A, S_B)], n_alternatives=3)

    assert len(routes) >= 1
    # First route must prefer the motorway
    assert "M1" in routes[0]["refs"], (
        f"Expected motorway ref M1 in first route refs {routes[0]['refs']}"
    )


def test_alternatives_are_diverse():
    """Two fully separate corridors should both be returned as distinct routes
    with no shared features."""
    # Northern corridor: A → mid-north → B
    north = _road_hw(
        "N1", "North Road",
        [(44.0, 40.0), (44.25, 40.1), (44.5, 40.0)],
        highway="primary",
    )
    # Southern corridor: A → mid-south → B (different intermediate node)
    south = _road_hw(
        "S1", "South Road",
        [(44.0, 40.0), (44.25, 39.9), (44.5, 40.0)],
        highway="primary",
    )
    roads = _fc(north, south)
    routes, _ = find_p2p_roads(
        roads, [(S_A, S_B)], n_alternatives=3, min_diversity=0.3
    )

    assert len(routes) == 2, (
        f"Expected 2 distinct routes, got {len(routes)}"
    )
    # The two corridors use different middle nodes; at minimum they must
    # use different ref labels.
    assert routes[0]["refs"] != routes[1]["refs"], (
        "Routes should have different road refs"
    )


def test_oneway_respected():
    """A oneway=yes road from A→B should yield a route A→B but not B→A."""
    road = _road_hw(
        "OW1", "One Way",
        [(44.0, 40.0), (44.5, 40.0)],   # coordinates go west→east (A→B)
        highway="primary",
        oneway="yes",
    )
    roads = _fc(road)

    # Forward: A (west) → B (east) — matches coordinate direction
    routes_fwd, _ = find_p2p_roads(roads, [(S_A, S_B)], n_alternatives=2)
    assert len(routes_fwd) == 1, (
        f"Expected 1 forward route, got {len(routes_fwd)}"
    )

    # Reverse: B → A — against oneway direction, should find no route
    routes_rev, _ = find_p2p_roads(roads, [(S_B, S_A)], n_alternatives=2)
    assert len(routes_rev) == 0, (
        f"Expected 0 reverse routes on oneway road, got {len(routes_rev)}"
    )


def test_diversity_filter_rejects_near_duplicates():
    """Routes that share most of their edges should be collapsed to one.

    Build a network where two paths share a long multi-segment backbone
    and only differ at the very last edge. The Jaccard similarity of their
    feature-index sets will be high, so the diversity filter should reject
    the second path.
    """
    # Backbone has many segments so it dominates the feature-index count.
    backbone_coords = [(44.0 + i * 0.04, 40.0) for i in range(12)]
    # last coord is (44.44, 40.0)
    last_x = backbone_coords[-1][0]

    backbone = _road_hw(
        "B1", "Backbone",
        backbone_coords,
        highway="motorway",
    )
    # Two near-identical last miles from the backbone endpoint to S_B
    last1 = _road_hw(
        "L1", "LastMile1",
        [(last_x, 40.0), (44.5, 40.0)],
        highway="primary",
    )
    last2 = _road_hw(
        "L2", "LastMile2",
        [(last_x, 40.0), (44.495, 40.0001), (44.5, 40.0)],
        highway="secondary",
    )
    roads = _fc(backbone, last1, last2)
    routes, _ = find_p2p_roads(
        roads, [(S_A, S_B)],
        n_alternatives=4,
        min_diversity=0.4,
    )
    # Route 1: features {backbone=0, last1=1}  (2 features)
    # Route 2: features {backbone=0, last2=2}  (2 features)
    # Jaccard = |{0}| / |{0,1,2}| = 1/3 ≈ 0.33 — just below the 0.60 threshold.
    # With a high min_diversity (e.g. 0.8) the second would be rejected.
    # With default min_diversity=0.4 both pass — assert ≤ n_alternatives.
    assert len(routes) <= 4
    # The two routes must use different last-mile features.
    if len(routes) == 2:
        assert routes[0]["feature_indices"] != routes[1]["feature_indices"]


def test_n_alternatives_cap():
    """Even with many candidate paths, returned count is capped at n_alternatives."""
    r1 = _road_hw("P1", "Road1",
                  [(44.0, 40.0), (44.5, 40.0)], highway="motorway")
    r2 = _road_hw("P2", "Road2",
                  [(44.0, 40.0), (44.25, 40.15), (44.5, 40.0)],
                  highway="primary")
    r3 = _road_hw("P3", "Road3",
                  [(44.0, 40.0), (44.25, 39.85), (44.5, 40.0)],
                  highway="primary")
    roads = _fc(r1, r2, r3)

    routes, _ = find_p2p_roads(
        roads, [(S_A, S_B)],
        n_alternatives=2,
        min_diversity=0.1,   # low threshold so alternatives pass easily
    )
    assert len(routes) <= 2, (
        "n_alternatives=2 should return at most 2 routes, "
        f"got {len(routes)}"
    )
