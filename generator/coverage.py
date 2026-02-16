"""Generate H3 hexagonal grid cells covering road geometries."""

import logging

import h3
from shapely.geometry import LineString, shape

logger = logging.getLogger(__name__)


def roads_to_h3_cells(
    roads_geojson: dict, resolution: int = 8,
) -> set[str]:
    """Find all H3 cells that intersect roads in a GeoJSON FeatureCollection.

    Samples points along each road LineString at intervals smaller than the
    H3 cell edge length, converts each sample to an H3 index, and returns
    the unique set.
    """
    # Sample spacing ~110 m at equator — sufficient for res 8 (~460 m edge)
    sample_spacing = 0.001  # degrees

    cells = set()
    for feat in roads_geojson.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            line = shape(geom)
        except Exception:
            continue

        if line.geom_type == "LineString":
            _sample_line(line, resolution, sample_spacing, cells)
        elif line.geom_type == "MultiLineString":
            for part in line.geoms:
                _sample_line(part, resolution, sample_spacing, cells)

    logger.info("H3 road cells: %d (resolution %d)", len(cells), resolution)
    return cells


def _sample_line(
    line: LineString, resolution: int, spacing: float, out: set,
) -> None:
    """Sample points along a LineString and add their H3 indices to *out*."""
    length = line.length
    if length == 0:
        return
    n = max(int(length / spacing) + 1, 2)
    for i in range(n):
        pt = line.interpolate(i / (n - 1), normalized=True)
        out.add(h3.latlng_to_cell(pt.y, pt.x, resolution))


def h3_cells_to_geojson(cells: set[str]) -> dict:
    """Convert a set of H3 cell indices to a GeoJSON FeatureCollection of Polygons."""
    features = []
    for idx in cells:
        boundary = h3.cell_to_boundary(idx)
        # boundary is list of (lat, lon) — convert to GeoJSON [lon, lat]
        coords = [[lon, lat] for lat, lon in boundary]
        coords.append(coords[0])  # close ring
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords],
            },
            "properties": {"h3_index": idx},
        })
    return {"type": "FeatureCollection", "features": features}
