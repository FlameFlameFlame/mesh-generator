"""Tests for generator.boundaries — city boundary detection and helpers."""

from unittest.mock import patch, MagicMock

from generator.boundaries import (
    _merge_ways,
    _relation_to_geojson,
    sample_border_points,
)


class TestMergeWays:
    def test_single_closed_ring(self):
        ring = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
        result = _merge_ways([ring])
        assert len(result) == 1
        assert result[0][0] == result[0][-1]

    def test_two_segments_join(self):
        seg1 = [(0, 0), (1, 0)]
        seg2 = [(1, 0), (1, 1), (0, 1), (0, 0)]
        result = _merge_ways([seg1, seg2])
        assert len(result) == 1
        assert result[0][0] == result[0][-1]

    def test_reversed_segment(self):
        seg1 = [(0, 0), (1, 0)]
        seg2 = [(0, 0), (0, 1), (1, 1), (1, 0)]  # reversed start
        result = _merge_ways([seg1, seg2])
        assert len(result) == 1

    def test_empty(self):
        assert _merge_ways([]) == []


class TestRelationToGeojson:
    def test_simple_polygon(self):
        relation = {
            "members": [
                {
                    "type": "way",
                    "role": "outer",
                    "geometry": [
                        {"lon": 0, "lat": 0},
                        {"lon": 1, "lat": 0},
                        {"lon": 1, "lat": 1},
                        {"lon": 0, "lat": 1},
                        {"lon": 0, "lat": 0},
                    ],
                }
            ]
        }
        geojson = _relation_to_geojson(relation)
        assert geojson is not None
        assert geojson["type"] == "Polygon"

    def test_no_members(self):
        assert _relation_to_geojson({"members": []}) is None

    def test_no_ways(self):
        relation = {"members": [{"type": "node", "geometry": []}]}
        assert _relation_to_geojson(relation) is None


class TestSampleBorderPoints:
    def test_returns_correct_count(self):
        geojson = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        }
        pts = sample_border_points(geojson, n=4)
        assert len(pts) == 4

    def test_returns_lat_lon_tuples(self):
        geojson = {
            "type": "Polygon",
            "coordinates": [[[10, 20], [11, 20], [11, 21], [10, 21], [10, 20]]],
        }
        pts = sample_border_points(geojson, n=2)
        for lat, lon in pts:
            assert 19 <= lat <= 22
            assert 9 <= lon <= 12

    def test_empty_polygon(self):
        geojson = {"type": "Polygon", "coordinates": []}
        pts = sample_border_points(geojson)
        assert pts == []


class TestDetectCity:
    @patch("generator.boundaries.requests.post")
    def test_no_boundary_returns_none(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"elements": []}
        mock_post.return_value = mock_resp

        from generator.boundaries import detect_city
        result = detect_city(40.0, 44.0)
        assert result is None

    @patch("generator.boundaries.requests.post")
    def test_found_boundary(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "elements": [
                {
                    "type": "relation",
                    "id": 123,
                    "tags": {"name": "TestCity", "admin_level": "4",
                             "boundary": "administrative",
                             "place": "city"},
                    "members": [
                        {
                            "type": "way",
                            "role": "outer",
                            "geometry": [
                                {"lon": 0, "lat": 0},
                                {"lon": 1, "lat": 0},
                                {"lon": 1, "lat": 1},
                                {"lon": 0, "lat": 1},
                                {"lon": 0, "lat": 0},
                            ],
                        }
                    ],
                }
            ]
        }
        mock_post.return_value = mock_resp

        from generator.boundaries import detect_city
        result = detect_city(0.5, 0.5)
        assert result is not None
        assert result["name"] == "TestCity"
        assert result["admin_level"] == 4
        assert "type" in result["geometry"]

    @patch("generator.boundaries.requests.post")
    def test_prefers_city_over_suburb(self, mock_post):
        """When both city and suburb match, prefer city."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        geom_members = [
            {
                "type": "way",
                "role": "outer",
                "geometry": [
                    {"lon": 0, "lat": 0},
                    {"lon": 1, "lat": 0},
                    {"lon": 1, "lat": 1},
                    {"lon": 0, "lat": 1},
                    {"lon": 0, "lat": 0},
                ],
            }
        ]
        mock_resp.json.return_value = {
            "elements": [
                {
                    "type": "relation",
                    "id": 100,
                    "tags": {
                        "name": "BigCity",
                        "admin_level": "4",
                        "boundary": "administrative",
                        "place": "city",
                    },
                    "members": geom_members,
                },
                {
                    "type": "relation",
                    "id": 200,
                    "tags": {
                        "name": "District",
                        "admin_level": "5",
                        "boundary": "administrative",
                        "place": "suburb",
                    },
                    "members": geom_members,
                },
            ]
        }
        mock_post.return_value = mock_resp

        from generator.boundaries import detect_city
        result = detect_city(0.5, 0.5)
        assert result is not None
        assert result["name"] == "BigCity"
        assert result["admin_level"] == 4

    @patch("generator.boundaries.requests.post")
    def test_network_error_returns_none(self, mock_post):
        mock_post.side_effect = Exception("timeout")

        from generator.boundaries import detect_city
        result = detect_city(40.0, 44.0)
        assert result is None
