import json

import yaml

from generator import graph as graph_mod
from generator import app as app_mod
from generator.app import app
from generator.models import SiteModel, SiteStore


def test_site_height_crud_roundtrip(monkeypatch):
    store = SiteStore()
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "_counter", 0)

    with app.test_client() as client:
        resp = client.post("/api/sites", json={
            "name": "A",
            "lat": 40.0,
            "lon": 44.0,
            "priority": 1,
            "site_height_m": 3.5,
        })
        data = resp.get_json()
        assert data[0]["site_height_m"] == 3.5

        resp2 = client.put("/api/sites/0", json={"site_height_m": 9.0})
        data2 = resp2.get_json()
        assert data2[0]["site_height_m"] == 9.0


def test_filter_p2p_propagates_site_height(monkeypatch):
    store = SiteStore()
    store.add(SiteModel(name="A", lat=40.0, lon=44.0, priority=1, site_height_m=4.0))
    store.add(SiteModel(name="B", lat=40.1, lon=44.1, priority=1, site_height_m=7.0))
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "_roads_geojson", {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[44.0, 40.0], [44.1, 40.1]]},
            "properties": {"osm_way_id": 123},
        }],
    })
    monkeypatch.setattr(app_mod, "_loaded_layers", {})

    captured = {}

    def _fake_find_p2p_roads(_roads_geojson, site_pairs, n_alternatives=3):
        captured["site_pairs"] = site_pairs
        s1, s2 = site_pairs[0]
        return ([{
            "route_id": "route_0",
            "refs": ["R1"],
            "ref": "R1",
            "road_name": "R1",
            "pair_idx": 0,
            "site1": s1,
            "site2": s2,
            "feature_indices": [0],
            "way_ids": [123],
        }], {0})

    monkeypatch.setattr(graph_mod, "find_p2p_roads", _fake_find_p2p_roads)

    with app.test_client() as client:
        resp = client.post("/api/roads/filter-p2p", json={})
        data = resp.get_json()

    assert data["routes"][0]["site1"]["site_height_m"] == 4.0
    assert data["routes"][0]["site2"]["site_height_m"] == 7.0
    assert captured["site_pairs"][0][0]["site_height_m"] == 4.0
    assert captured["site_pairs"][0][1]["site_height_m"] == 7.0


def test_load_project_reads_site_height_and_defaults_legacy(tmp_path, monkeypatch):
    sites = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [44.5, 40.2]},
                "properties": {"name": "A", "priority": 1, "site_height_m": 8.0},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [44.6, 40.3]},
                "properties": {"name": "B", "priority": 1},
            },
        ],
    }
    sites_path = tmp_path / "sites.geojson"
    with open(sites_path, "w") as f:
        json.dump(sites, f)

    config = {
        "parameters": {"h3_resolution": 8},
        "inputs": {
            "target_sites": str(sites_path),
            "boundary": "",
            "roads": "",
            "elevation": "",
        },
        "outputs": {},
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)

    monkeypatch.setattr(app_mod, "store", SiteStore())

    with app.test_client() as client:
        resp = client.post("/api/load", json={"path": str(config_path)})
        data = resp.get_json()

    by_name = {s["name"]: s for s in data["sites"]}
    assert by_name["A"]["site_height_m"] == 8.0
    assert by_name["B"]["site_height_m"] == 0.0


def test_link_analysis_accepts_endpoint_heights(monkeypatch, tmp_path):
    elev_path = tmp_path / "elevation.tif"
    elev_path.write_bytes(b"fake")
    monkeypatch.setattr(app_mod, "_elevation_path", str(elev_path))

    class _FakeElevationProvider:
        def __init__(self, _path):
            pass

        def get_elevation(self, _lat, _lon):
            return 100.0

    import mesh_calculator.core.elevation as elev_mod
    monkeypatch.setattr(elev_mod, "ElevationProvider", _FakeElevationProvider)

    with app.test_client() as client:
        resp = client.post("/api/link-analysis", json={
            "source_lat": 40.0,
            "source_lon": 44.0,
            "target_lat": 40.1,
            "target_lon": 44.1,
            "source_height_m": 12.0,
            "target_height_m": 6.0,
        })
        data = resp.get_json()

    assert data["source_height_m"] == 12.0
    assert data["target_height_m"] == 6.0
