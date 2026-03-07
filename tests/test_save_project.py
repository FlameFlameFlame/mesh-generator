import json
import os

from generator import app as app_mod
from generator.app import app
from generator.models import SiteModel, SiteStore


def test_save_project_routes_export_uses_runtime_parameters(tmp_path, monkeypatch):
    store = SiteStore()
    store.add(SiteModel(name="A", lat=40.0, lon=44.0, priority=1))
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "_roads_geojson", {"type": "FeatureCollection", "features": []})
    monkeypatch.setattr(app_mod, "_elevation_path", "")
    monkeypatch.setattr(app_mod, "_loaded_layers", {})

    route_feature = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[44.0, 40.0], [44.1, 40.1]]},
        "properties": {"osm_way_id": 123},
    }
    route = {
        "route_id": "route_0",
        "site1": {"name": "A", "lat": 40.0, "lon": 44.0},
        "site2": {"name": "B", "lat": 40.1, "lon": 44.1},
        "pair_idx": 0,
        "feature_indices": [0],
        "way_ids": [123],
        "features": [route_feature],
    }
    monkeypatch.setattr(app_mod, "_p2p_routes", [route])
    monkeypatch.setattr(app_mod, "_p2p_all_route_features", {"route_0": [route_feature]})

    params = {
        "h3_resolution": 9,
        "frequency_hz": 915000000,
        "mast_height_m": 2,
        "max_towers_per_route": 5,
        "road_buffer_m": 100,
    }
    app_mod._save_project_to_dir(str(tmp_path), parameters=params)

    with open(tmp_path / "routes.json") as f:
        routes_data = json.load(f)
    with open(tmp_path / "status.json") as f:
        status_data = json.load(f)

    assert routes_data["parameters"]["h3_resolution"] == 9
    assert routes_data["parameters"]["frequency_hz"] == 915000000
    assert routes_data["parameters"]["mast_height_m"] == 2
    assert routes_data["routes"][0]["max_towers_per_route"] == 5
    assert status_data["parameters"]["mast_height_m"] == 2


def test_export_persists_loaded_calculation_outputs_and_run_history(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_mod, "DEFAULT_OUTPUT_DIR", str(projects_root))
    store = SiteStore()
    store.add(SiteModel(name="A", lat=40.0, lon=44.0, priority=1))
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "_roads_geojson", {"type": "FeatureCollection", "features": []})
    monkeypatch.setattr(app_mod, "_elevation_path", "")
    monkeypatch.setattr(app_mod, "_grid_bundle_path", "")
    monkeypatch.setattr(app_mod, "_grid_provider", None)
    monkeypatch.setattr(app_mod, "_grid_provider_summary", "")
    monkeypatch.setattr(app_mod, "_p2p_routes", [])
    monkeypatch.setattr(app_mod, "_p2p_all_route_features", {})
    monkeypatch.setattr(app_mod, "_loaded_layers", {
        "towers": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [44.0, 40.0]}, "properties": {"tower_id": 1}}
        ]},
        "edges": {"type": "FeatureCollection", "features": []},
        "grid_cells": {"type": "FeatureCollection", "features": []},
    })
    monkeypatch.setattr(app_mod, "_loaded_report", {"total_towers": 1, "visibility_edges": 0})
    monkeypatch.setattr(app_mod, "_opt_result", {"summary": {"total_towers": 1, "visibility_edges": 0}})

    project_name = "New project"
    project_dir = projects_root / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    with app.test_client() as client:
        resp = client.post("/api/export", json={
            "project_name": project_name,
            "parameters": {"h3_resolution": 8, "mast_height_m": 5, "max_towers_per_route": 5},
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)

    assert os.path.isfile(project_dir / "towers.geojson")
    assert os.path.isfile(project_dir / "visibility_edges.geojson")
    assert os.path.isfile(project_dir / "grid_cells.geojson")
    assert os.path.isfile(project_dir / "report.json")

    runs_dir = project_dir / "runs"
    assert runs_dir.is_dir()
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert os.path.isfile(run_dir / "run_settings.json")
    with open(run_dir / "run_settings.json") as f:
        run_settings = json.load(f)
    assert run_settings["parameters"]["mast_height_m"] == 5
    assert "towers.geojson" in run_settings["files"]

    with open(project_dir / "status.json") as f:
        status = json.load(f)
    assert len(status.get("optimization_runs", [])) == 1
    assert status.get("last_optimization_run", {}).get("run_id")
