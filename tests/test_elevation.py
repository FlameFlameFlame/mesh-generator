import gzip
import math
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio

from generator.elevation import (
    _tile_name,
    _tiles_for_bbox,
    _download_tile,
    _mosaic_tiles,
    _write_geotiff,
    fetch_and_write_elevation,
    SRTM1_SIZE,
)


class TestTileName:
    def test_positive_lat_lon(self):
        assert _tile_name(40, 44) == "N40E044"

    def test_negative_lat_lon(self):
        assert _tile_name(-3, -70) == "S03W070"

    def test_zero(self):
        assert _tile_name(0, 0) == "N00E000"

    def test_large_lon(self):
        assert _tile_name(10, 179) == "N10E179"


class TestTilesForBbox:
    def test_single_tile(self):
        tiles = _tiles_for_bbox(40.2, 44.3, 40.8, 44.7)
        assert tiles == [(40, 44)]

    def test_two_tiles_longitude(self):
        tiles = _tiles_for_bbox(40.2, 44.3, 40.8, 45.7)
        assert set(tiles) == {(40, 44), (40, 45)}

    def test_four_tiles(self):
        tiles = _tiles_for_bbox(39.8, 43.8, 40.5, 44.5)
        assert set(tiles) == {(39, 43), (39, 44), (40, 43), (40, 44)}

    def test_exact_boundary_no_extra(self):
        # north=41.0 exactly should NOT require tile at lat=41
        tiles = _tiles_for_bbox(40.0, 44.0, 41.0, 45.0)
        assert set(tiles) == {(40, 44)}


class TestDownloadTile:
    def _make_hgt_gz(self, value: int = 500) -> bytes:
        """Create a gzipped HGT file with constant elevation."""
        data = struct.pack(f">{SRTM1_SIZE * SRTM1_SIZE}h",
                           *([value] * (SRTM1_SIZE * SRTM1_SIZE)))
        return gzip.compress(data)

    def test_download_and_parse(self):
        mock_resp = MagicMock()
        mock_resp.content = self._make_hgt_gz(1234)
        mock_resp.raise_for_status = MagicMock()

        with patch("generator.elevation.requests.get", return_value=mock_resp):
            result = _download_tile(40, 44)

        assert result.shape == (SRTM1_SIZE, SRTM1_SIZE)
        assert result.dtype == np.float32
        assert result[0, 0] == 1234.0

    def test_http_error_propagates(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")

        with patch("generator.elevation.requests.get", return_value=mock_resp):
            with pytest.raises(Exception, match="404"):
                _download_tile(99, 99)

    def test_bad_size_raises(self):
        mock_resp = MagicMock()
        mock_resp.content = gzip.compress(b"\x00" * 100)
        mock_resp.raise_for_status = MagicMock()

        with patch("generator.elevation.requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="expected"):
                _download_tile(40, 44)


class TestMosaicTiles:
    def test_single_tile_clip(self):
        tile = np.arange(SRTM1_SIZE * SRTM1_SIZE, dtype=np.float32).reshape(
            SRTM1_SIZE, SRTM1_SIZE
        )
        tiles = {(40, 44): tile}
        data, nrows, ncols = _mosaic_tiles(tiles, 40.5, 44.5, 40.6, 44.6)

        # Should be clipped, much smaller than full tile
        assert nrows < SRTM1_SIZE
        assert ncols < SRTM1_SIZE
        assert nrows > 0
        assert ncols > 0


class TestWriteGeotiff:
    def test_roundtrip(self):
        data = np.array([[100.0, 200.0], [300.0, 400.0]], dtype=np.float32)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "test.tif")
            _write_geotiff(data, 40.0, 44.0, 40.1, 44.1, 2, 2, path)

            with rasterio.open(path) as src:
                assert src.crs.to_string() == "EPSG:4326"
                assert src.height == 2
                assert src.width == 2
                read_data = src.read(1)
                np.testing.assert_array_almost_equal(read_data, data)

    def test_compression(self):
        data = np.ones((10, 10), dtype=np.float32)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "compressed.tif")
            _write_geotiff(data, 0.0, 0.0, 1.0, 1.0, 10, 10, path)

            with rasterio.open(path) as src:
                assert src.compression == rasterio.enums.Compression.deflate


class TestFetchAndWriteElevation:
    def test_full_pipeline(self):
        """Integration test with mocked HTTP."""
        tile_data = np.full((SRTM1_SIZE, SRTM1_SIZE), 800.0, dtype=np.float32)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "elevation.tif")

            with patch("generator.elevation._download_tile", return_value=tile_data):
                result = fetch_and_write_elevation(40.2, 44.3, 40.8, 44.7, path)

            assert result == path
            assert Path(path).exists()

            with rasterio.open(path) as src:
                assert src.crs.to_string() == "EPSG:4326"
                data = src.read(1)
                # All values should be 800 (no NODATA)
                assert np.all(data == 800.0)
