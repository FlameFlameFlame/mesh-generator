import json
import logging
import os

import yaml
from shapely.geometry import MultiPoint, mapping, shape
from shapely.ops import unary_union

from generator.models import SiteModel

logger = logging.getLogger(__name__)
_EXPORT_EXCLUDED_PARAMETER_KEYS = {"h3_resolution", "max_coverage_radius_m"}


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
                "fetch_city": site.fetch_city,
                "site_height_m": site.site_height_m,
                **({"boundary_name": site.boundary_name}
                   if site.boundary_name else {}),
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


def export_city_boundaries_geojson(
    sites: list[SiteModel], path: str,
) -> None:
    """Write city boundaries as a GeoJSON FeatureCollection."""
    features = []
    for site in sites:
        if site.boundary_geojson:
            features.append({
                "type": "Feature",
                "geometry": site.boundary_geojson,
                "properties": {
                    "name": site.name,
                    "boundary_name": site.boundary_name,
                },
            })

    collection = {
        "type": "FeatureCollection",
        "features": features,
    }
    with open(path, "w") as f:
        json.dump(collection, f, indent=2)
    logger.info(
        "Exported %d city boundaries to %s", len(features), path)


def export_config_yaml(
    output_dir: str,
    sites_path: str,
    boundary_path: str,
    roads_path: str = "",
    elevation_path: str = "",
    grid_bundle_path: str = "",
    city_boundaries_path: str = "",
    parameters: dict | None = None,
) -> None:
    """Write config.yaml for mesh-engine."""
    defaults = {
        "frequency_hz": 868000000.0,
        "mast_height_m": 5.0,
        "tx_power_mw": 500.0,
        "antenna_gain_dbi": 2.0,
        "receiver_sensitivity_dbm": -137.0,
        "max_towers_per_route": 10,
        "road_buffer_m": 100.0,
    }
    if parameters:
        defaults.update(parameters)
    for key in _EXPORT_EXCLUDED_PARAMETER_KEYS:
        defaults.pop(key, None)
    def _rel(p):
        """Make path relative to output_dir if possible, else keep as-is."""
        if not p:
            return p
        try:
            return os.path.relpath(p, output_dir)
        except ValueError:
            return p  # different drive on Windows

    config = {
        "parameters": defaults,
        "inputs": {
            "boundary": _rel(boundary_path),
            "elevation": _rel(elevation_path),
            "grid_bundle": _rel(grid_bundle_path),
            "roads": _rel(roads_path),
            "target_sites": _rel(sites_path),
            "city_boundaries": _rel(city_boundaries_path),
        },
        "outputs": {
            "towers": "towers.geojson",
            "coverage": "coverage.geojson",
            "report": "report.json",
            "visibility_edges": "visibility_edges.geojson",
        },
    }

    config_path = os.path.join(output_dir, "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(
            config, f,
            default_flow_style=False, sort_keys=False)
    logger.info("Exported config to %s", config_path)
