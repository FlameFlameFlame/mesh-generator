"""Fetch road data from OpenStreetMap via Overpass API."""

import logging

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
        coords = []
        for node_id in element.get("nodes", []):
            if node_id in nodes:
                coords.append(list(nodes[node_id]))
        if len(coords) < 2:
            continue
        tags = element.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": tags,
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
