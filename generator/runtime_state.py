from dataclasses import dataclass, field

from generator.models import SiteStore


@dataclass
class AppState:
    """Mutable in-memory state for a single mesh-generator server session."""

    store: SiteStore = field(default_factory=SiteStore)
    counter: int = 0
    loaded_layers: dict = field(default_factory=dict)
    roads_geojson: dict | None = None
    full_roads_geojson: dict | None = None
    loaded_report: dict | None = None
    loaded_coverage: dict | None = None
    runtime_tower_coverage: dict | None = None
    elevation_path: str | None = None
    grid_bundle_path: str | None = None
    grid_provider = None
    grid_provider_summary: str = ""
    p2p_routes: list = field(default_factory=list)
    p2p_all_route_features: dict = field(default_factory=dict)
    p2p_display_features: dict = field(default_factory=dict)
    forced_waypoints: dict = field(default_factory=dict)
    active_mesh_parameters: dict = field(default_factory=dict)
    opt_result: dict = field(default_factory=dict)
