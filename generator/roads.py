"""Fetch road data from OpenStreetMap via Overpass API."""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HIGHWAY_TYPES = ["motorway", "trunk", "primary", "secondary", "tertiary"]


def build_overpass_query(south: float, west: float, north: float, east: float) -> str:
    bbox = f"{south},{west},{north},{east}"
    highway_filter = "|".join(HIGHWAY_TYPES)
    return (
        f'[out:json][timeout:60];'
        f'way["highway"~"^({highway_filter})$"]({bbox});'
        f'out body;>;out skel qt;'
    )


def parse_overpass_response(data: dict) -> dict:
    """Parse Overpass JSON into a GeoJSON FeatureCollection of LineStrings."""
    nodes = {}
    features = []

    for element in data.get("elements", []):
        if element["type"] == "node":
            nodes[element["id"]] = (element["lon"], element["lat"])

    for element in data.get("elements", []):
        if element["type"] != "way":
            continue
        node_ids = element.get("nodes", [])
        coords = []
        resolved_ids = []
        for node_id in node_ids:
            if node_id in nodes:
                coords.append(list(nodes[node_id]))
                resolved_ids.append(node_id)
        if len(coords) < 2:
            continue
        tags = element.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                **tags,
                "osm_way_id": element["id"],
                "osm_node_ids": resolved_ids,
            },
        })

    return {"type": "FeatureCollection", "features": features}


def fetch_roads(south: float, west: float, north: float, east: float) -> dict:
    """Fetch roads from Overpass API for the given bounding box. Returns GeoJSON."""
    query = build_overpass_query(south, west, north, east)
    logger.info("Fetching roads from Overpass API, bbox=[%.4f, %.4f, %.4f, %.4f]",
                south, west, north, east)

    response = requests.post(OVERPASS_URL, data={"data": query}, timeout=90)
    response.raise_for_status()
    data = response.json()

    way_count = sum(1 for e in data.get("elements", []) if e["type"] == "way")
    logger.info("Overpass returned %d ways", way_count)

    geojson = parse_overpass_response(data)
    logger.info("Parsed %d road features", len(geojson["features"]))
    return geojson


def fetch_roads_cached(
    south: float, west: float, north: float, east: float,
    cache_dir: str | None = None,
) -> dict:
    """fetch_roads with optional disk cache keyed by rounded bbox.

    Cache key uses 2 decimal places (~1 km precision) so minor bbox
    differences don't bust the cache unnecessarily.
    """
    cache_file = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        key = f"{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}"
        cache_file = os.path.join(cache_dir, f"roads_{key}.json")
        if os.path.isfile(cache_file):
            logger.info("Roads cache hit: %s", cache_file)
            with open(cache_file) as f:
                return json.load(f)

    geojson = fetch_roads(south, west, north, east)

    if cache_file:
        with open(cache_file, "w") as f:
            json.dump(geojson, f)
        logger.info("Roads cached to %s", cache_file)

    return geojson
