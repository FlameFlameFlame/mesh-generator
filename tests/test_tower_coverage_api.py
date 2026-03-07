from flask import jsonify

from generator import app as app_mod
from generator.app import app


def test_get_tower_coverage_returns_runtime_cache(monkeypatch):
    monkeypatch.setattr(app_mod, "_runtime_tower_coverage", {
        "type": "FeatureCollection",
        "features": [],
    })
    with app.test_client() as client:
        resp = client.get("/api/tower-coverage")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["type"] == "FeatureCollection"


def test_calculate_requires_source_list():
    with app.test_client() as client:
        resp = client.post("/api/tower-coverage/calculate", json={})
    assert resp.status_code == 400
    assert "source" in resp.get_json()["error"].lower()


def test_calculate_returns_400_without_elevation(monkeypatch):
    monkeypatch.setattr(app_mod, "_elevation_path", "")
    with app.test_client() as client:
        resp = client.post("/api/tower-coverage/calculate", json={
            "source": {"source_id": 1, "lat": 40.2, "lon": 44.5},
        })
    assert resp.status_code == 400
    assert "elevation" in resp.get_json()["error"].lower()


def test_calculate_single_calls_runtime_helper(monkeypatch):
    captured = {}

    def _fake_run(sources, body):
        captured["sources"] = sources
        captured["body"] = body
        return jsonify({
            "coverage": {"type": "FeatureCollection", "features": []},
            "source_count": len(sources),
            "feature_count": 0,
        })

    monkeypatch.setattr(app_mod, "_run_runtime_tower_coverage", _fake_run)
    with app.test_client() as client:
        resp = client.post("/api/tower-coverage/calculate", json={
            "source": {"source_id": "point", "lat": 40.2, "lon": 44.5},
            "parameters": {"h3_resolution": 8},
        })
    assert resp.status_code == 200
    assert len(captured["sources"]) == 1
    assert captured["sources"][0]["source_id"] == "point"


def test_calculate_batch_calls_runtime_helper(monkeypatch):
    captured = {}

    def _fake_run(sources, body):
        captured["sources"] = sources
        return jsonify({
            "coverage": {"type": "FeatureCollection", "features": []},
            "source_count": len(sources),
            "feature_count": 0,
        })

    monkeypatch.setattr(app_mod, "_run_runtime_tower_coverage", _fake_run)
    with app.test_client() as client:
        resp = client.post("/api/tower-coverage/calculate-batch", json={
            "sources": [
                {"source_id": 1, "h3_index": "8828341aedfffff", "lat": 40.2, "lon": 44.5},
                {"source_id": 2, "h3_index": "8828341aebfffff", "lat": 40.21, "lon": 44.51},
            ],
        })
    assert resp.status_code == 200
    assert len(captured["sources"]) == 2


def test_calculate_single_forwards_coverage_resolution(monkeypatch):
    captured = {}

    def _fake_run(sources, body):
        captured["body"] = body
        return jsonify({
            "coverage": {"type": "FeatureCollection", "features": []},
            "source_count": len(sources),
            "feature_count": 0,
            "coverage_h3_resolution": body.get("coverage_h3_resolution"),
        })

    monkeypatch.setattr(app_mod, "_run_runtime_tower_coverage", _fake_run)
    with app.test_client() as client:
        resp = client.post("/api/tower-coverage/calculate", json={
            "source": {"source_id": "point", "lat": 40.2, "lon": 44.5},
            "parameters": {"h3_resolution": 8},
            "coverage_h3_resolution": 9,
        })
    assert resp.status_code == 200
    assert captured["body"]["coverage_h3_resolution"] == 9


def test_calculate_rejects_out_of_range_coverage_resolution(monkeypatch):
    monkeypatch.setattr(app_mod, "_elevation_path", __file__)
    monkeypatch.setattr(app_mod, "_grid_provider", object())
    with app.test_client() as client:
        resp = client.post("/api/tower-coverage/calculate", json={
            "source": {"source_id": "point", "lat": 40.2, "lon": 44.5},
            "parameters": {"h3_resolution": 8},
            "coverage_h3_resolution": 12,
        })
    assert resp.status_code == 400
    assert "between 6 and 9" in resp.get_json()["error"]
