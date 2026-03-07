import json
from pathlib import Path

import mesh_calculator.optimization.route_pipeline as route_pipeline_mod

from generator import app as app_mod
from generator.app import app


def _seed_minimum_optimization_state(tmp_path: Path) -> None:
    elev = tmp_path / "elevation.tif"
    elev.write_bytes(b"fake")
    app_mod._elevation_path = str(elev)
    app_mod._grid_provider = object()
    app_mod._opt_running = False
    app_mod._opt_result = {}
    app_mod._p2p_routes = [{
        "route_id": "route_0",
        "site1": {"name": "Yerevan", "lat": 40.2, "lon": 44.5},
        "site2": {"name": "Gyumri", "lat": 40.8, "lon": 43.8},
        "pair_idx": 0,
        "feature_indices": [0],
        "way_ids": [123],
    }]
    app_mod._p2p_all_route_features = {
        "route_0": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[44.5, 40.2], [43.8, 40.8]]},
            "properties": {"osm_way_id": 123},
        }]
    }


def _read_sse_events(client):
    resp = client.get("/api/optimization-stream")
    assert resp.status_code == 200
    events = []
    for line in resp.data.decode("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload:
            continue
        events.append(json.loads(payload))
    return events


def test_optimization_stream_emits_progress_and_done(monkeypatch, tmp_path):
    _seed_minimum_optimization_state(tmp_path)

    def _fake_run_route_pipeline(
        routes, mesh_config, grid_provider, city_boundaries_geojson=None,
        boundary_geojson=None, output_dir="output", progress_callback=None
    ):
        route = routes[0]
        if progress_callback:
            progress_callback({
                "stage": "route",
                "step": "Preparing corridor",
                "percent": 0.0,
                "route_index": 1,
                "route_total": len(routes),
                "route_id": route.route_id,
                "route_label": f"{route.site1.get('name')} ↔ {route.site2.get('name')} ({route.route_id})",
            })
            progress_callback({
                "stage": "route",
                "step": "Finalizing route links",
                "percent": 80.0,
                "route_index": 1,
                "route_total": len(routes),
                "route_id": route.route_id,
                "route_label": f"{route.site1.get('name')} ↔ {route.site2.get('name')} ({route.route_id})",
            })
            progress_callback({
                "stage": "visibility",
                "step": "Computing visibility edges",
                "percent": 80.0,
                "route_index": 0,
                "route_total": len(routes),
                "route_id": None,
                "route_label": None,
            })
            progress_callback({
                "stage": "done",
                "step": "Pipeline complete",
                "percent": 100.0,
                "route_index": 1,
                "route_total": len(routes),
                "route_id": None,
                "route_label": None,
            })
        return {
            "routes_processed": len(routes),
            "total_towers": 7,
            "total_cells": 100,
            "visibility_edges": 11,
            "num_clusters": 1,
            "route_summaries": [],
            "los_cache": {},
            "elevation_cache": {},
        }

    monkeypatch.setattr(route_pipeline_mod, "run_route_pipeline", _fake_run_route_pipeline)

    with app.test_client() as client:
        start = client.post("/api/run-optimization", json={
            "max_towers_per_route": 5,
            "parameters": {},
            "output_dir": "",
        })
        assert start.status_code == 200
        assert start.get_json()["started"] is True

        events = _read_sse_events(client)

    assert any(e.get("done") for e in events)
    progress_events = [e["progress"] for e in events if "progress" in e]
    assert progress_events
    vals = [
        float(p.get("percent", 0.0))
        for p in progress_events
        if p.get("stage") != "error"
    ]
    assert vals == sorted(vals)
    assert all("stage" in p and "step" in p for p in progress_events)


def test_optimization_progress_handles_pipeline_failure(monkeypatch, tmp_path):
    _seed_minimum_optimization_state(tmp_path)

    def _fake_run_route_pipeline(
        routes, mesh_config, grid_provider, city_boundaries_geojson=None,
        boundary_geojson=None, output_dir="output", progress_callback=None
    ):
        raise RuntimeError("pipeline failed")

    monkeypatch.setattr(route_pipeline_mod, "run_route_pipeline", _fake_run_route_pipeline)

    with app.test_client() as client:
        start = client.post("/api/run-optimization", json={
            "max_towers_per_route": 5,
            "parameters": {},
            "output_dir": "",
        })
        assert start.status_code == 200

        events = _read_sse_events(client)

    assert not any(e.get("done") for e in events)
    assert any("error" in e for e in events)
    err = next(e["error"] for e in events if "error" in e)
    assert "pipeline failed" in err
