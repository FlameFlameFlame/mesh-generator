import json
import logging
import os

import yaml
from shapely.geometry import MultiPoint, mapping, shape
from shapely.ops import unary_union

from generator.models import SiteModel

logger = logging.getLogger(__name__)


def export_sites_geojson(sites: list[SiteModel], path: str) -> None:
    """Write sites as a GeoJSON FeatureCollection of Points."""
    features = []
    for site in sites:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [site.lon, site.lat],
            },
            "properties": {
                "name": site.name,
                "priority": site.priority,
            },
        })

    collection = {
        "type": "FeatureCollection",
        "features": features,
    }

    with open(path, "w") as f:
        json.dump(collection, f, indent=2)
    logger.info("Exported %d sites to %s", len(sites), path)


def export_roads_geojson(roads_geojson: dict, path: str) -> None:
    """Write roads GeoJSON FeatureCollection to file."""
    with open(path, "w") as f:
        json.dump(roads_geojson, f, indent=2)
    logger.info("Exported %d roads to %s", len(roads_geojson.get("features", [])), path)


def export_boundary_geojson(
    sites: list[SiteModel], path: str, buffer_deg: float = 0.15,
    roads_geojson: dict | None = None,
) -> None:
    """Write convex hull of sites+roads (buffered) as a GeoJSON FeatureCollection with a single Polygon."""
    geoms = [MultiPoint([(s.lon, s.lat) for s in sites])]
    if roads_geojson:
        for feat in roads_geojson.get("features", []):
            try:
                geoms.append(shape(feat["geometry"]))
            except Exception:
                pass
    combined = unary_union(geoms)
    hull = combined.convex_hull.buffer(buffer_deg)

    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(hull),
                "properties": {},
            }
        ],
    }

    with open(path, "w") as f:
        json.dump(collection, f, indent=2)
    logger.info("Exported boundary to %s", path)


def export_config_yaml(
    output_dir: str,
    sites_path: str,
    boundary_path: str,
    roads_path: str = "",
    elevation_path: str = "",
) -> None:
    """Write a config.yaml matching mesh-engine's MeshCalculatorConfig.from_dict format."""
    config = {
        "parameters": {
            "h3_resolution": 8,
            "frequency_hz": 868000000.0,
            "mast_height_m": 28.0,
            "max_visibility_m": 70000.0,
            "tower_separation_m": 5000.0,
            "hop_limit": 7,
            "max_nodes_per_road": 10,
        },
        "inputs": {
            "boundary": boundary_path,
            "elevation": elevation_path,
            "roads": roads_path,
            "target_sites": sites_path,
        },
        "outputs": {
            "towers": os.path.join(output_dir, "towers.geojson"),
            "coverage": os.path.join(output_dir, "coverage.geojson"),
            "report": os.path.join(output_dir, "report.json"),
        },
    }

    config_path = os.path.join(output_dir, "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    logger.info("Exported config to %s", config_path)
