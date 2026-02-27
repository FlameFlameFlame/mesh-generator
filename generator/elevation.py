"""Download SRTM elevation tiles and produce a GeoTIFF for a bounding box."""

import gzip
import logging
import math
import os
import struct
import zlib

import numpy as np
import rasterio
import requests
from rasterio.transform import from_bounds

logger = logging.getLogger(__name__)

# AWS/Mapzen public SRTM 1-arc-second tiles (no auth required)
SKADI_URL = "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{folder}/{filename}.hgt.gz"
SRTM1_SIZE = 3601  # 1 arc-second: 3601 x 3601 samples per 1°x1° tile
NODATA = -32768


def _tile_name(lat: int, lon: int) -> str:
    """Build SRTM tile filename from integer SW corner coordinates.

    >>> _tile_name(40, 44)
    'N40E044'
    >>> _tile_name(-3, -70)
    'S03W070'
    """
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}"


def _tiles_for_bbox(south: float, west: float, north: float, east: float) -> list[tuple[int, int]]:
    """Return list of (lat, lon) integer SW corners covering the bbox."""
    lat_min = math.floor(south)
    lat_max = math.floor(north - 1e-9)  # avoid grabbing extra tile at exact boundary
    lon_min = math.floor(west)
    lon_max = math.floor(east - 1e-9)
    tiles = []
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            tiles.append((lat, lon))
    return tiles


def _download_tile(lat: int, lon: int, timeout: int = 60) -> np.ndarray:
    """Download and parse one SRTM HGT tile. Returns (3601, 3601) int16 array."""
    name = _tile_name(lat, lon)
    folder = name[:3]  # e.g. "N40"
    url = SKADI_URL.format(folder=folder, filename=name)
    logger.info("Downloading SRTM tile %s", url)

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    raw = gzip.decompress(resp.content)
    expected = SRTM1_SIZE * SRTM1_SIZE * 2  # int16 = 2 bytes
    if len(raw) != expected:
        raise ValueError(f"Tile {name}: expected {expected} bytes, got {len(raw)}")

    data = np.frombuffer(raw, dtype=">i2").reshape(SRTM1_SIZE, SRTM1_SIZE)
    return data.astype(np.float32)


def _download_tile_cached(lat: int, lon: int, cache_dir: str | None = None) -> np.ndarray:
    """Download tile, with optional per-tile disk cache (numpy .npy format).

    Cache location: cache_dir/srtm/<tilename>.npy
    """
    cache_file = None
    if cache_dir:
        srtm_dir = os.path.join(cache_dir, "srtm")
        os.makedirs(srtm_dir, exist_ok=True)
        cache_file = os.path.join(srtm_dir, f"{_tile_name(lat, lon)}.npy")
        if os.path.isfile(cache_file):
            logger.info("Elevation tile cache hit: %s", cache_file)
            return np.load(cache_file)

    data = _download_tile(lat, lon)

    if cache_file:
        np.save(cache_file, data)
        logger.info("Elevation tile cached to %s", cache_file)

    return data


def _mosaic_tiles(
    tiles: dict[tuple[int, int], np.ndarray],
    south: float, west: float, north: float, east: float,
) -> tuple[np.ndarray, int, int]:
    """Mosaic downloaded tiles and clip to the bounding box.

    Returns (data, nrows, ncols) clipped to bbox.
    """
    lat_min = math.floor(south)
    lon_min = math.floor(west)
    lat_max = max(lat for lat, _ in tiles)
    lon_max = max(lon for _, lon in tiles)

    # Full mosaic dimensions
    n_tiles_lat = lat_max - lat_min + 1
    n_tiles_lon = lon_max - lon_min + 1
    total_rows = n_tiles_lat * SRTM1_SIZE - (n_tiles_lat - 1)  # overlapping edges
    total_cols = n_tiles_lon * SRTM1_SIZE - (n_tiles_lon - 1)

    mosaic = np.full((total_rows, total_cols), NODATA, dtype=np.float32)

    for (lat, lon), data in tiles.items():
        # Tile row/col offset in the mosaic (north to south)
        row_offset = (lat_max + 1 - (lat + 1)) * (SRTM1_SIZE - 1)
        col_offset = (lon - lon_min) * (SRTM1_SIZE - 1)
        mosaic[row_offset:row_offset + SRTM1_SIZE,
               col_offset:col_offset + SRTM1_SIZE] = data

    # Mosaic covers lat_min..(lat_max+1), lon_min..(lon_max+1)
    mosaic_north = lat_max + 1
    mosaic_west = lon_min
    px_per_deg = SRTM1_SIZE - 1  # 3600 pixels per degree

    # Clip to bbox (pixel indices)
    row_start = max(0, int(round((mosaic_north - north) * px_per_deg)))
    row_end = min(total_rows, int(round((mosaic_north - south) * px_per_deg)) + 1)
    col_start = max(0, int(round((west - mosaic_west) * px_per_deg)))
    col_end = min(total_cols, int(round((east - mosaic_west) * px_per_deg)) + 1)

    clipped = mosaic[row_start:row_end, col_start:col_end]
    return clipped, clipped.shape[0], clipped.shape[1]


def fetch_and_write_elevation(
    south: float,
    west: float,
    north: float,
    east: float,
    output_path: str,
) -> str:
    """Download SRTM tiles for a bounding box and write a GeoTIFF.

    Args:
        south, west, north, east: Bounding box in degrees.
        output_path: Destination .tif path.

    Returns:
        The output_path written.
    """
    tile_coords = _tiles_for_bbox(south, west, north, east)
    logger.info("Need %d SRTM tile(s) for bbox [%.3f, %.3f, %.3f, %.3f]",
                len(tile_coords), south, west, north, east)

    tiles = {}
    for lat, lon in tile_coords:
        tiles[(lat, lon)] = _download_tile(lat, lon)

    data, nrows, ncols = _mosaic_tiles(tiles, south, west, north, east)

    # Replace NODATA with 0
    data[data == NODATA] = 0.0

    _write_geotiff(data, south, west, north, east, nrows, ncols, output_path)
    return output_path


def fetch_and_write_elevation_cached(
    south: float,
    west: float,
    north: float,
    east: float,
    output_path: str,
    cache_dir: str | None = None,
) -> str:
    """Like fetch_and_write_elevation but uses per-tile disk cache."""
    tile_coords = _tiles_for_bbox(south, west, north, east)
    logger.info(
        "Need %d SRTM tile(s) for bbox [%.3f, %.3f, %.3f, %.3f]",
        len(tile_coords), south, west, north, east,
    )
    tiles = {}
    for lat, lon in tile_coords:
        tiles[(lat, lon)] = _download_tile_cached(lat, lon, cache_dir)
    data, nrows, ncols = _mosaic_tiles(tiles, south, west, north, east)
    data[data == NODATA] = 0.0
    _write_geotiff(data, south, west, north, east, nrows, ncols, output_path)
    return output_path


def _write_geotiff(
    data: np.ndarray,
    south: float, west: float, north: float, east: float,
    nrows: int, ncols: int,
    output_path: str,
) -> None:
    """Write a 2-D float32 array as a single-band GeoTIFF in EPSG:4326."""
    transform = from_bounds(west, south, east, north, ncols, nrows)
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=nrows,
        width=ncols,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        compress="deflate",
    ) as dst:
        dst.write(data, 1)
    logger.info("Wrote elevation GeoTIFF %s (%d x %d)", output_path, nrows, ncols)


# ---------------------------------------------------------------------------
# Elevation image rendering (GeoTIFF → PNG for Leaflet overlay)
# ---------------------------------------------------------------------------

# Terrain colormap: (elevation_fraction, R, G, B)
# 64 stops for smooth rendering: deep green → yellow-green → gold → brown → gray → white
_TERRAIN_STOPS = [
    (0.000,   0,  80,   0),
    (0.016,   5,  90,   5),
    (0.032,  12, 100,  12),
    (0.048,  20, 110,  20),
    (0.063,  28, 120,  28),
    (0.079,  34, 130,  34),
    (0.095,  40, 139,  34),
    (0.111,  50, 148,  38),
    (0.127,  60, 155,  42),
    (0.143,  72, 162,  46),
    (0.159,  85, 168,  50),
    (0.175, 100, 175,  55),
    (0.190, 112, 180,  58),
    (0.206, 124, 185,  60),
    (0.222, 134, 188,  62),
    (0.238, 144, 190,  65),
    (0.254, 155, 192,  66),
    (0.270, 165, 194,  68),
    (0.286, 175, 196,  70),
    (0.302, 185, 197,  72),
    (0.317, 192, 198,  74),
    (0.333, 200, 198,  76),
    (0.349, 206, 197,  78),
    (0.365, 212, 196,  79),
    (0.381, 218, 195,  80),
    (0.397, 216, 190,  76),
    (0.413, 213, 184,  72),
    (0.429, 210, 178,  68),
    (0.444, 206, 172,  64),
    (0.460, 202, 165,  60),
    (0.476, 198, 158,  57),
    (0.492, 194, 150,  55),
    (0.508, 190, 143,  53),
    (0.524, 185, 136,  52),
    (0.540, 180, 130,  50),
    (0.556, 175, 124,  50),
    (0.571, 170, 118,  52),
    (0.587, 165, 113,  55),
    (0.603, 162, 108,  57),
    (0.619, 158, 104,  60),
    (0.635, 155, 100,  63),
    (0.651, 152,  98,  66),
    (0.667, 150, 100,  70),
    (0.683, 150, 105,  78),
    (0.698, 152, 112,  88),
    (0.714, 155, 120, 100),
    (0.730, 158, 128, 110),
    (0.746, 162, 136, 118),
    (0.762, 166, 142, 126),
    (0.778, 170, 150, 130),
    (0.794, 175, 155, 138),
    (0.810, 180, 162, 148),
    (0.825, 185, 168, 155),
    (0.841, 190, 175, 160),
    (0.857, 196, 182, 168),
    (0.873, 202, 190, 178),
    (0.889, 208, 198, 188),
    (0.905, 214, 205, 198),
    (0.921, 220, 212, 206),
    (0.937, 228, 220, 215),
    (0.952, 235, 228, 224),
    (0.968, 242, 236, 233),
    (0.984, 248, 245, 243),
    (1.000, 255, 255, 255),
]


def _terrain_color(t: float) -> tuple[int, int, int]:
    """Interpolate the terrain colormap at fraction *t* (0..1)."""
    t = max(0.0, min(1.0, t))
    for i in range(len(_TERRAIN_STOPS) - 1):
        t0, r0, g0, b0 = _TERRAIN_STOPS[i]
        t1, r1, g1, b1 = _TERRAIN_STOPS[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
            return (
                int(r0 + f * (r1 - r0)),
                int(g0 + f * (g1 - g0)),
                int(b0 + f * (b1 - b0)),
            )
    return _TERRAIN_STOPS[-1][1:]


def _encode_png(rgba: np.ndarray) -> bytes:
    """Encode an (H, W, 4) uint8 RGBA array as PNG using stdlib only."""
    h, w = rgba.shape[:2]

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    # IHDR
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA

    # IDAT: filter type 0 (None) for each row, then deflate
    raw_rows = bytearray()
    for y in range(h):
        raw_rows.append(0)  # filter byte
        raw_rows.extend(rgba[y].tobytes())
    compressed = zlib.compress(bytes(raw_rows), 6)

    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", ihdr)
    out += _chunk(b"IDAT", compressed)
    out += _chunk(b"IEND", b"")
    return out


def render_elevation_image(
    geotiff_path: str,
    max_size: int = 1024,
) -> tuple[bytes, dict]:
    """Read a GeoTIFF and return a colorized PNG plus metadata.

    Args:
        geotiff_path: Path to a single-band elevation GeoTIFF.
        max_size: Maximum pixel dimension (longest side); image is downsampled
                  if needed for fast rendering.

    Returns:
        (png_bytes, metadata) where metadata has keys:
            bounds: {south, west, north, east}
            min_elevation: float
            max_elevation: float
            width: int
            height: int
    """
    with rasterio.open(geotiff_path) as src:
        bounds = src.bounds  # BoundingBox(left, bottom, right, top)
        full_h, full_w = src.height, src.width

        # Compute downsample factor
        longest = max(full_h, full_w)
        if longest > max_size:
            factor = longest / max_size
            out_h = max(1, int(full_h / factor))
            out_w = max(1, int(full_w / factor))
        else:
            out_h, out_w = full_h, full_w

        # Read with rasterio resampling (nearest for speed)
        from rasterio.enums import Resampling
        elev = src.read(
            1,
            out_shape=(out_h, out_w),
            resampling=Resampling.average,
        )

    # Mask NODATA / invalid
    valid = (elev > NODATA) & np.isfinite(elev)
    if valid.any():
        vmin = float(elev[valid].min())
        vmax = float(elev[valid].max())
    else:
        vmin, vmax = 0.0, 1.0

    erange = vmax - vmin if vmax > vmin else 1.0

    # Build RGBA image (vectorized)
    t = np.clip((elev - vmin) / erange, 0.0, 1.0)

    # Piecewise-linear interpolation over terrain colormap stops
    rgba = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    stops_t = np.array([s[0] for s in _TERRAIN_STOPS], dtype=np.float32)
    stops_rgb = np.array([s[1:] for s in _TERRAIN_STOPS], dtype=np.float32)

    # np.searchsorted gives the index of the upper stop for each pixel
    idx = np.searchsorted(stops_t, t, side="right").clip(1, len(stops_t) - 1)
    t0 = stops_t[idx - 1]
    t1 = stops_t[idx]
    f = np.where(t1 != t0, (t - t0) / (t1 - t0), 0.0).astype(np.float32)

    for ch in range(3):
        c0 = stops_rgb[idx - 1, ch]
        c1 = stops_rgb[idx, ch]
        rgba[:, :, ch] = np.clip(c0 + f * (c1 - c0), 0, 255).astype(np.uint8)

    rgba[:, :, 3] = np.where(valid, 200, 0)  # semi-transparent where valid

    png_bytes = _encode_png(rgba)

    metadata = {
        "bounds": {
            "south": bounds.bottom,
            "west": bounds.left,
            "north": bounds.top,
            "east": bounds.right,
        },
        "min_elevation": round(vmin, 1),
        "max_elevation": round(vmax, 1),
        "width": out_w,
        "height": out_h,
    }
    logger.info(
        "Rendered elevation image %dx%d, elev %.0f..%.0f m",
        out_w, out_h, vmin, vmax,
    )
    return png_bytes, metadata
