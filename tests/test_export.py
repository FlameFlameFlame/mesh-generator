import json

import yaml

from generator.models import SiteModel
from generator.export import (
    export_sites_geojson,
    export_boundary_geojson,
    export_config_yaml,
)


def _make_sites():
    return [
        SiteModel("Yerevan", 40.18, 44.51, 1),
        SiteModel("Gyumri", 40.79, 43.84, 2),
        SiteModel("Vanadzor", 40.81, 44.49, 1),
    ]


class TestExportSitesGeojson:
    def test_format(self, tmp_path):
        sites = _make_sites()
        path = str(tmp_path / "sites.geojson")
        export_sites_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 3
        for feat in data["features"]:
            assert feat["type"] == "Feature"
            assert feat["geometry"]["type"] == "Point"
            assert "name" in feat["properties"]
            assert "priority" in feat["properties"]

    def test_coordinates_lon_lat_order(self, tmp_path):
        sites = [SiteModel("Test", 40.0, 44.0, 1)]
        path = str(tmp_path / "sites.geojson")
        export_sites_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        coords = data["features"][0]["geometry"]["coordinates"]
        # GeoJSON is [lon, lat]
        assert coords == [44.0, 40.0]

    def test_properties_match(self, tmp_path):
        sites = _make_sites()
        path = str(tmp_path / "sites.geojson")
        export_sites_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        props = data["features"][1]["properties"]
        assert props["name"] == "Gyumri"
        assert props["priority"] == 2


class TestExportBoundaryGeojson:
    def test_polygon_geometry(self, tmp_path):
        sites = _make_sites()
        path = str(tmp_path / "boundary.geojson")
        export_boundary_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1
        assert data["features"][0]["geometry"]["type"] == "Polygon"

    def test_encloses_all_sites(self, tmp_path):
        from shapely.geometry import shape, Point

        sites = _make_sites()
        path = str(tmp_path / "boundary.geojson")
        export_boundary_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        boundary = shape(data["features"][0]["geometry"])
        for site in sites:
            assert boundary.contains(Point(site.lon, site.lat))

    def test_two_sites_no_crash(self, tmp_path):
        """Two sites form a line — should still produce a valid polygon via buffer."""
        sites = [SiteModel("A", 40.0, 44.0, 1), SiteModel("B", 41.0, 45.0, 1)]
        path = str(tmp_path / "boundary.geojson")
        export_boundary_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        assert data["features"][0]["geometry"]["type"] == "Polygon"

    def test_one_site_no_crash(self, tmp_path):
        """Single site is a point — should produce a buffered circle polygon."""
        sites = [SiteModel("A", 40.0, 44.0, 1)]
        path = str(tmp_path / "boundary.geojson")
        export_boundary_geojson(sites, path)

        with open(path) as f:
            data = json.load(f)

        assert data["features"][0]["geometry"]["type"] == "Polygon"


class TestExportConfigYaml:
    def test_parameters_override(self, tmp_path):
        """Custom parameters dict should override defaults in config.yaml."""
        export_config_yaml(
            str(tmp_path),
            str(tmp_path / "sites.geojson"),
            str(tmp_path / "boundary.geojson"),
            parameters={
                "frequency_hz": 433000000.0,
                "mast_height_m": 10.0,
                "h3_resolution": 9,
            },
        )
        config_path = str(tmp_path / "config.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config["parameters"]["frequency_hz"] == 433000000.0
        assert config["parameters"]["mast_height_m"] == 10.0
        assert config["parameters"]["h3_resolution"] == 9
        # Non-overridden defaults preserved
        assert config["parameters"]["max_towers_per_route"] == 10
