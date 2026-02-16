"""Tests for generator.coverage — H3 hexagon grid for roads."""

from generator.coverage import h3_cells_to_geojson, roads_to_h3_cells


def _make_roads(*segments):
    features = []
    for coords in segments:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {},
        })
    return {"type": "FeatureCollection", "features": features}


# Short road segment (~111 km along equator)
SHORT_ROAD = _make_roads([[44.0, 40.0], [44.5, 40.0]])

# Two separate roads
TWO_ROADS = _make_roads(
    [[44.0, 40.0], [44.2, 40.0]],
    [[45.0, 40.0], [45.2, 40.0]],
)


class TestRoadsToH3Cells:
    def test_returns_nonempty(self):
        cells = roads_to_h3_cells(SHORT_ROAD, resolution=8)
        assert len(cells) > 0

    def test_all_valid_h3(self):
        import h3
        cells = roads_to_h3_cells(SHORT_ROAD, resolution=8)
        for idx in cells:
            assert h3.is_valid_cell(idx)

    def test_more_cells_for_longer_road(self):
        short = roads_to_h3_cells(
            _make_roads([[44.0, 40.0], [44.1, 40.0]]), resolution=8)
        long = roads_to_h3_cells(
            _make_roads([[44.0, 40.0], [44.5, 40.0]]), resolution=8)
        assert len(long) > len(short)

    def test_two_roads_union(self):
        cells = roads_to_h3_cells(TWO_ROADS, resolution=8)
        # Should have cells from both road segments
        assert len(cells) > 10

    def test_empty_roads(self):
        cells = roads_to_h3_cells(
            {"type": "FeatureCollection", "features": []})
        assert len(cells) == 0


class TestH3CellsToGeojson:
    def test_feature_collection_structure(self):
        cells = roads_to_h3_cells(SHORT_ROAD, resolution=8)
        geojson = h3_cells_to_geojson(cells)
        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) == len(cells)

    def test_polygon_geometry(self):
        cells = roads_to_h3_cells(SHORT_ROAD, resolution=8)
        geojson = h3_cells_to_geojson(cells)
        feat = geojson["features"][0]
        assert feat["geometry"]["type"] == "Polygon"
        ring = feat["geometry"]["coordinates"][0]
        # H3 hexagons have 7 points (6 vertices + closing)
        assert len(ring) == 7

    def test_has_h3_index_property(self):
        cells = roads_to_h3_cells(SHORT_ROAD, resolution=8)
        geojson = h3_cells_to_geojson(cells)
        for feat in geojson["features"]:
            assert "h3_index" in feat["properties"]

    def test_empty_cells(self):
        geojson = h3_cells_to_geojson(set())
        assert len(geojson["features"]) == 0
