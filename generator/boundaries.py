"""City/town boundary detection via Overpass API."""

import logging

import requests
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def detect_city(lat: float, lon: float) -> dict | None:
    """Query Overpass for the administrative boundary containing (lat, lon).

    Returns ``{"name": str, "admin_level": int, "geometry": <GeoJSON>}``
    for the smallest matching area, or ``None`` if the point is not
    inside any admin boundary at levels 4-8.
    """
    query = (
        f'[out:json][timeout:60];'
        f'is_in({lat},{lon})->.a;'
        f'area.a["boundary"="administrative"]'
        f'["admin_level"~"^(4|5|6|7|8)$"];'
        f'rel(pivot);out geom;'
    )

    logger.info("Detecting city at (%.4f, %.4f)", lat, lon)

    try:
        resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=65)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Overpass city query failed: %s", e)
        return None

    data = resp.json()
    elements = [e for e in data.get("elements", []) if e.get("type") == "relation"]

    if not elements:
        logger.info("No admin boundary found at (%.4f, %.4f)", lat, lon)
        return None

    # Prefer city/town boundaries, then highest admin_level
    city_places = {"city", "town"}
    cities = [
        e for e in elements
        if e.get("tags", {}).get("place") in city_places
    ]
    if cities:
        best = min(
            cities,
            key=lambda e: int(
                e.get("tags", {}).get("admin_level", "99")
            ),
        )
    else:
        best = max(
            elements,
            key=lambda e: int(
                e.get("tags", {}).get("admin_level", "0")
            ),
        )

    geojson = _relation_to_geojson(best)
    if geojson is None:
        return None

    name = best.get("tags", {}).get("name", "Unknown")
    level = int(best.get("tags", {}).get("admin_level", 0))

    logger.info("Detected: %s (admin_level=%d)", name, level)
    return {"name": name, "admin_level": level, "geometry": geojson}


def _relation_to_geojson(relation: dict) -> dict | None:
    """Convert an Overpass relation with geometry to a GeoJSON Polygon/MultiPolygon."""
    members = relation.get("members", [])
    outers = []
    inners = []

    for member in members:
        if member.get("type") != "way":
            continue
        geom = member.get("geometry")
        if not geom:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        if len(coords) < 2:
            continue
        role = member.get("role", "outer")
        if role == "inner":
            inners.append(coords)
        else:
            outers.append(coords)

    if not outers:
        return None

    # Merge outer way segments into closed rings
    rings = _merge_ways(outers)
    if not rings:
        return None

    # Build Shapely polygon(s) and convert to GeoJSON
    from shapely.geometry import Polygon, MultiPolygon
    polys = []
    inner_rings = _merge_ways(inners) if inners else []

    for ring in rings:
        try:
            p = Polygon(ring, inner_rings)
            if p.is_valid and not p.is_empty:
                polys.append(p)
        except Exception:
            continue

    if not polys:
        return None

    if len(polys) == 1:
        return mapping(polys[0])
    return mapping(unary_union(polys))


def _merge_ways(way_coords: list[list[tuple]]) -> list[list[tuple]]:
    """Merge way segments into closed rings by connecting endpoints."""
    if not way_coords:
        return []

    # Try to join ways end-to-end
    remaining = list(way_coords)
    rings = []

    while remaining:
        current = list(remaining.pop(0))
        changed = True
        while changed:
            changed = False
            for i, segment in enumerate(remaining):
                if current[-1] == segment[0]:
                    current.extend(segment[1:])
                    remaining.pop(i)
                    changed = True
                    break
                elif current[-1] == segment[-1]:
                    current.extend(reversed(segment[:-1]))
                    remaining.pop(i)
                    changed = True
                    break
                elif current[0] == segment[-1]:
                    current = list(segment[:-1]) + current
                    remaining.pop(i)
                    changed = True
                    break
                elif current[0] == segment[0]:
                    current = list(reversed(segment[1:])) + current
                    remaining.pop(i)
                    changed = True
                    break

        # Close the ring if needed
        if current[0] != current[-1]:
            current.append(current[0])
        if len(current) >= 4:
            rings.append(current)

    return rings


def sample_border_points(
    boundary_geojson: dict, n: int = 8,
) -> list[tuple[float, float]]:
    """Sample *n* evenly-spaced points along a boundary perimeter.

    Returns list of ``(lat, lon)`` tuples.
    """
    geom = shape(boundary_geojson)
    if geom.is_empty:
        return []

    boundary_line = geom.boundary
    if boundary_line.is_empty:
        return []

    # For MultiPolygon, use the exterior of the largest polygon
    if hasattr(boundary_line, 'geoms'):
        boundary_line = max(boundary_line.geoms, key=lambda g: g.length)

    length = boundary_line.length
    if length == 0:
        return []

    points = []
    for i in range(n):
        frac = i / n
        pt = boundary_line.interpolate(frac, normalized=True)
        points.append((pt.y, pt.x))  # (lat, lon)

    return points
