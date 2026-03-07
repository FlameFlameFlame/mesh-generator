"""Tests for expanded load, coverage endpoint, and clear state."""
import json
import os

import h3
import yaml

from generator import app as app_mod
from generator.app import DEFAULT_OUTPUT_DIR, app
from generator.models import SiteModel, SiteStore


def _write_fixture(tmp_path):
    """Create a minimal project fixture with all output files."""
    # Sites
    sites = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.5, 40.2]},
            "properties": {"name": "Yerevan", "priority": 1},
        }],
    }
    sites_path = str(tmp_path / "sites.geojson")
    with open(sites_path, "w") as f:
        json.dump(sites, f)

    # Boundary
    boundary = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [
                [[44.0, 40.0], [45.0, 40.0], [45.0, 41.0], [44.0, 41.0], [44.0, 40.0]]
            ]},
            "properties": {},
        }],
    }
    boundary_path = str(tmp_path / "boundary.geojson")
    with open(boundary_path, "w") as f:
        json.dump(boundary, f)

    # Towers
    towers = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.5, 40.2]},
            "properties": {"tower_id": 1, "source": "seed", "h3_index": "8828c0001"},
        }],
    }
    towers_path = str(tmp_path / "towers.geojson")
    with open(towers_path, "w") as f:
        json.dump(towers, f)

    # Visibility edges
    edges = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[44.5, 40.2], [44.6, 40.3]]},
            "properties": {"source_id": 1, "target_id": 2, "distance_m": 12000},
        }],
    }
    edges_path = str(tmp_path / "visibility_edges.geojson")
    with open(edges_path, "w") as f:
        json.dump(edges, f)

    # Coverage
    coverage = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [
                [[44.5, 40.2], [44.51, 40.2], [44.51, 40.21], [44.5, 40.21], [44.5, 40.2]]
            ]},
            "properties": {"h3_index": "8828c0001", "elevation": 1200, "visible_tower_count": 2},
        }],
    }
    coverage_path = str(tmp_path / "coverage.geojson")
    with open(coverage_path, "w") as f:
        json.dump(coverage, f)

    # Report
    report = {
        "total_cells": 100,
        "cells_with_towers": 5,
        "total_towers": 5,
        "num_clusters": 1,
        "towers_by_source": {"seed": 2, "route": 3},
    }
    report_path = str(tmp_path / "report.json")
    with open(report_path, "w") as f:
        json.dump(report, f)

    # Config
    config = {
        "parameters": {"h3_resolution": 8},
        "inputs": {
            "boundary": boundary_path,
            "roads": "",
            "target_sites": sites_path,
            "elevation": "",
        },
        "outputs": {
            "towers": towers_path,
            "coverage": coverage_path,
            "report": report_path,
            "visibility_edges": edges_path,
        },
    }
    config_path = str(tmp_path / "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    return config_path


class TestLoadProject:
    def test_index_sets_projects_default_output_dir(self):
        with app.test_client() as client:
            resp = client.get("/")

        assert resp.status_code == 200
        assert f'value="{DEFAULT_OUTPUT_DIR}"'.encode() in resp.data

    def test_load_returns_edges_layer(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert "edges" in data["layers"]
        assert data["layers"]["edges"]["type"] == "FeatureCollection"

    def test_load_returns_report(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert data["report"] is not None
        assert data["report"]["total_towers"] == 5

    def test_load_returns_has_coverage(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert data["has_coverage"] is True

    def test_load_without_outputs(self, tmp_path):
        """Loading a config with no output files still works."""
        sites = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [44.5, 40.2]},
                "properties": {"name": "A", "priority": 1},
            }],
        }
        sites_path = str(tmp_path / "sites.geojson")
        with open(sites_path, "w") as f:
            json.dump(sites, f)

        config = {
            "parameters": {},
            "inputs": {"target_sites": sites_path, "boundary": "", "roads": "", "elevation": ""},
            "outputs": {"towers": "nonexistent.geojson", "report": "nonexistent.json"},
        }
        config_path = str(tmp_path / "config.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert "error" not in data
        assert data["report"] is None
        assert data["has_coverage"] is False

    def test_load_prefers_config_parameters_over_stale_status(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        status_path = tmp_path / "status.json"
        with open(status_path, "w") as f:
            json.dump({
                "has_roads": True,
                "parameters": {
                    "mast_height_m": 2,
                    "h3_resolution": 8,
                },
            }, f)

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        cfg["parameters"]["mast_height_m"] = 28
        with open(config_path, "w") as f:
            yaml.safe_dump(cfg, f)

        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert data["project_status"]["parameters"]["mast_height_m"] == 28

    def test_load_uses_fallback_elevation_for_grid_bundle_hydration(self, tmp_path, monkeypatch):
        config_path = _write_fixture(tmp_path)
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        cfg["inputs"]["elevation"] = "missing_elevation.tif"
        cfg["inputs"]["grid_bundle"] = "grid_bundle.json"
        with open(config_path, "w") as f:
            yaml.safe_dump(cfg, f)

        (tmp_path / "grid_bundle.json").write_text("{}", encoding="utf-8")
        fallback_elevation = tmp_path / "elevation.tif"
        fallback_elevation.write_bytes(b"not-a-real-geotiff")

        captured = {}

        def _fake_hydrate(bundle_path, elevation_path=None):
            captured["bundle_path"] = bundle_path
            captured["elevation_path"] = elevation_path

        monkeypatch.setattr(app_mod, "_hydrate_grid_provider", _fake_hydrate)

        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert "error" not in data
        assert captured["bundle_path"] == str(tmp_path / "grid_bundle.json")
        assert captured["elevation_path"] == str(fallback_elevation)

    def test_load_overrides_stale_status_provider_flags(self, tmp_path, monkeypatch):
        config_path = _write_fixture(tmp_path)
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        cfg["inputs"]["elevation"] = "elevation.tif"
        cfg["inputs"]["grid_bundle"] = "grid_bundle.json"
        with open(config_path, "w") as f:
            yaml.safe_dump(cfg, f)

        elevation_path = tmp_path / "elevation.tif"
        elevation_path.write_bytes(b"fake-geotiff")
        (tmp_path / "grid_bundle.json").write_text("{}", encoding="utf-8")
        with open(tmp_path / "status.json", "w") as f:
            json.dump({
                "has_elevation": False,
                "has_grid_provider": False,
                "grid_provider_summary": "",
                "has_routes": False,
            }, f)

        def _fake_hydrate(bundle_path, elevation_path=None):
            app_mod._grid_provider = object()
            app_mod._grid_bundle_path = bundle_path
            app_mod._grid_provider_summary = "res=8,9"

        monkeypatch.setattr(app_mod, "_hydrate_grid_provider", _fake_hydrate)
        monkeypatch.setattr(app_mod, "_close_grid_provider", lambda: None)

        with app.test_client() as client:
            resp = client.post("/api/load", json={"path": config_path})
            data = resp.get_json()

        assert resp.status_code == 200
        assert data["has_elevation"] is True
        assert data["has_grid_provider"] is True
        assert data["project_status"]["has_elevation"] is True
        assert data["project_status"]["has_grid_provider"] is True
        assert data["project_status"]["grid_provider_summary"] == "res=8,9"


class TestElevationDownload:
    def test_elevation_build_uses_fresh_downloaded_path(self, tmp_path, monkeypatch):
        store = SiteStore()
        store.add(SiteModel(name="A", lat=40.70, lon=43.80, priority=1))
        store.add(SiteModel(name="B", lat=40.50, lon=43.90, priority=1))
        monkeypatch.setattr(app_mod, "store", store)
        monkeypatch.setattr(app_mod, "_loaded_layers", {
            "boundary": {"type": "FeatureCollection", "features": []},
        })
        monkeypatch.setattr(app_mod, "_roads_geojson", None)
        monkeypatch.setattr(app_mod, "_full_roads_geojson", None)
        monkeypatch.setattr(app_mod, "_elevation_path", None)

        monkeypatch.setattr(app_mod, "_resolve_project_output_dir", lambda payload: str(tmp_path))
        monkeypatch.setattr(app_mod, "_get_cache_dir", lambda output_dir: str(tmp_path / "cache"))

        def _fake_fetch(_south, _west, _north, _east, output_path, cache_dir=None):
            with open(output_path, "wb") as f:
                f.write(b"fake-geotiff")
            return output_path

        captured = {}

        def _fake_build(output_dir=None, elevation_path=None, boundary_geojson=None, roads_geojson=None):
            assert elevation_path is not None and os.path.isfile(elevation_path)
            captured["elevation_path"] = elevation_path
            return {
                "bundle_path": str(tmp_path / "grid_bundle.json"),
                "resolutions": [8, 9],
                "summary": "res=8,9",
            }

        monkeypatch.setattr(app_mod.pipeline_site_handlers, "fetch_and_write_elevation_cached", _fake_fetch)
        monkeypatch.setattr(app_mod, "_build_grid_bundle_for_current_state", _fake_build)
        monkeypatch.setattr(app_mod, "_hydrate_grid_provider", lambda bundle_path, elevation_path=None: None)
        monkeypatch.setattr(app_mod, "_write_status_json", lambda *args, **kwargs: None)

        with app.test_client() as client:
            resp = client.post("/api/elevation", json={"project_name": "demo"})
            data = resp.get_json()

        assert resp.status_code == 200
        assert "error" not in data
        assert data["grid_provider_ready"] is True
        assert captured["elevation_path"] == app_mod._elevation_path


class TestCoverageEndpoint:
    def test_coverage_after_load(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        with app.test_client() as client:
            client.post("/api/load", json={"path": config_path})
            resp = client.get("/api/coverage")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1

    def test_coverage_404_without_load(self, tmp_path):
        with app.test_client() as client:
            # Clear first to ensure no stale state
            client.post("/api/clear")
            resp = client.get("/api/coverage")

        assert resp.status_code == 404

    def test_coverage_runtime_fallback_from_grid_and_towers(self, tmp_path):
        h3_idx = h3.latlng_to_cell(40.2, 44.5, 8)
        lat, lon = h3.cell_to_latlng(h3_idx)
        boundary = h3.cell_to_boundary(h3_idx)
        poly_coords = [[lng, lt] for lt, lng in boundary] + [[boundary[0][1], boundary[0][0]]]

        sites = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"name": "A", "priority": 1},
            }],
        }
        sites_path = str(tmp_path / "sites.geojson")
        with open(sites_path, "w") as f:
            json.dump(sites, f)

        towers = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"tower_id": 1, "source": "site", "h3_index": h3_idx},
            }],
        }
        with open(tmp_path / "towers.geojson", "w") as f:
            json.dump(towers, f)

        grid_cells = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [poly_coords]},
                "properties": {"h3_index": h3_idx, "elevation": 1200.0, "has_road": True},
            }],
        }
        with open(tmp_path / "grid_cells.geojson", "w") as f:
            json.dump(grid_cells, f)

        config = {
            "parameters": {"h3_resolution": 8},
            "inputs": {
                "target_sites": sites_path,
                "boundary": "",
                "roads": "",
                "elevation": "",
            },
            "outputs": {
                "towers": str(tmp_path / "towers.geojson"),
                "coverage": str(tmp_path / "missing_coverage.geojson"),
                "report": str(tmp_path / "missing_report.json"),
                "visibility_edges": str(tmp_path / "missing_edges.geojson"),
            },
        }
        config_path = str(tmp_path / "config.yaml")
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f)

        with app.test_client() as client:
            load_resp = client.post("/api/load", json={"path": config_path})
            assert load_resp.status_code == 200
            resp = client.get("/api/coverage")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1


class TestGridLayersEndpoint:
    def test_grid_layers_uses_helper_injected_from_app_module(self, monkeypatch):
        h3_idx = h3.latlng_to_cell(40.2, 44.5, 8)

        class _FakeGridProvider:
            def adaptive_resolution_summary(self, base_res, cfg):
                return {
                    "effective_h3_resolution_min": 8,
                    "effective_h3_resolution_max": 8,
                }

            def get_adaptive_road_cells(self, base_res, cfg):
                return {h3_idx}

            def get_adaptive_full_cells(self, base_res, cfg):
                return {h3_idx}

            def get_adaptive_cell_metadata(self, h3_index, base_res, cfg):
                return {"h3_resolution": 8, "effective_h3_resolution": 8}

            def get_h3_cell_max_elevation(self, h3_index):
                return 100.0

        monkeypatch.setattr(app_mod, "_grid_provider", _FakeGridProvider())

        with app.test_client() as client:
            resp = client.post("/api/grid-layers", json={
                "parameters": {"h3_resolution": 8},
                "include_full": True,
                "max_cells": 5000,
            })
            data = resp.get_json()

        assert resp.status_code == 200
        assert "error" not in data
        assert data["grid_cells_count"] >= 1
        assert data["grid_cells_full_count"] >= 1


class TestPickFile:
    def test_pick_file_returns_error_for_non_cancel_macos_failure(self, monkeypatch):
        import subprocess

        class _Res:
            returncode = 1
            stdout = ""
            stderr = "execution error: Not authorized to send Apple events."

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _Res())

        with app.test_client() as client:
            resp = client.post("/api/pick-file")
            data = resp.get_json()

        assert resp.status_code == 200
        assert data.get("path") == ""
        assert "Native picker failed:" in data.get("error", "")

    def test_pick_file_cancel_on_macos_returns_empty_path(self, monkeypatch):
        import subprocess

        class _Res:
            returncode = 1
            stdout = ""
            stderr = "User canceled."

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _Res())

        with app.test_client() as client:
            resp = client.post("/api/pick-file")
            data = resp.get_json()

        assert resp.status_code == 200
        assert data.get("path") == ""
        assert "error" not in data

    def test_pick_file_uses_jxa_fallback_on_macos(self, monkeypatch):
        import subprocess

        class _Res:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        calls = {"n": 0}

        def _fake_run(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] in (1, 2):
                return _Res(1, "", "Not authorized to send Apple events.")
            return _Res(0, "/tmp/my_project\n", "")

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(subprocess, "run", _fake_run)

        with app.test_client() as client:
            resp = client.post("/api/pick-file")
            data = resp.get_json()

        assert resp.status_code == 200
        assert data.get("path") == "/tmp/my_project"


class TestClearResetsState:
    def test_clear_resets_coverage(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        with app.test_client() as client:
            client.post("/api/load", json={"path": config_path})
            client.post("/api/clear")
            resp = client.get("/api/coverage")

        assert resp.status_code == 404

    def test_clear_resets_report(self, tmp_path):
        config_path = _write_fixture(tmp_path)
        with app.test_client() as client:
            client.post("/api/load", json={"path": config_path})
            client.post("/api/clear")
            resp = client.post("/api/load", json={"path": config_path})
            # Re-load should work fine after clear
            data = resp.get_json()

        assert data["report"]["total_towers"] == 5


class TestProjectApis:
    def test_projects_create_list_rename(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(app_mod, "DEFAULT_OUTPUT_DIR", str(projects_root))

        with app.test_client() as client:
            resp = client.get("/api/projects")
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["projects"] == []

            create = client.post("/api/projects/create", json={})
            c = create.get_json()
            assert create.status_code == 200
            assert c["name"] == "New project"

            listed = client.get("/api/projects").get_json()["projects"]
            assert any(p["name"] == "New project" for p in listed)

            rename = client.post("/api/projects/rename", json={
                "old_name": "New project",
                "new_name": "Renamed project",
            })
            assert rename.status_code == 200
            listed2 = client.get("/api/projects").get_json()["projects"]
            assert any(p["name"] == "Renamed project" for p in listed2)

    def test_projects_open_and_load_run(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(app_mod, "DEFAULT_OUTPUT_DIR", str(projects_root))
        project_dir = projects_root / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)

        config_path = _write_fixture(project_dir)
        run_dir = project_dir / "runs" / "20260307T000000.000000Z"
        run_dir.mkdir(parents=True, exist_ok=True)
        towers = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [44.5, 40.2]},
                "properties": {"tower_id": 7, "source": "route", "h3_index": "8828c0001"},
            }],
        }
        with open(run_dir / "towers.geojson", "w") as f:
            json.dump(towers, f)
        with open(run_dir / "report.json", "w") as f:
            json.dump({"total_towers": 7, "visibility_edges": 2}, f)
        with open(run_dir / "run_settings.json", "w") as f:
            json.dump({
                "run_id": "20260307T000000.000000Z",
                "saved_at_utc": "2026-03-07T00:00:00Z",
                "parameters": {"mast_height_m": 5},
                "summary": {"total_towers": 7, "visibility_edges": 2},
                "files": ["towers.geojson", "report.json"],
            }, f)
        with open(project_dir / "status.json", "w") as f:
            json.dump({"optimization_runs": [{"run_id": "20260307T000000.000000Z"}]}, f)

        with app.test_client() as client:
            open_resp = client.post("/api/projects/open", json={"project_name": "demo"})
            open_data = open_resp.get_json()
            assert open_resp.status_code == 200
            assert open_data["latest_run_id"] == "20260307T000000.000000Z"
            assert open_data["config_path"] == config_path

            client.post("/api/load", json={"path": config_path})
            run_resp = client.post("/api/projects/load-run", json={
                "project_name": "demo",
                "run_id": "20260307T000000.000000Z",
            })
            run_data = run_resp.get_json()
            assert run_resp.status_code == 200
            assert run_data["report"]["total_towers"] == 7
            assert run_data["layers"]["towers"]["features"][0]["properties"]["tower_id"] == 7

    def test_projects_list_handles_null_status_json(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(app_mod, "DEFAULT_OUTPUT_DIR", str(projects_root))
        project_dir = projects_root / "null-status"
        project_dir.mkdir(parents=True, exist_ok=True)
        with open(project_dir / "status.json", "w", encoding="utf-8") as f:
            f.write("null")

        with app.test_client() as client:
            resp = client.get("/api/projects")
            data = resp.get_json()

        assert resp.status_code == 200
        assert any(p["name"] == "null-status" for p in data["projects"])


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
