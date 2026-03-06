from dataclasses import dataclass, field


@dataclass
class SiteModel:
    name: str
    lat: float
    lon: float
    priority: int = 1
    site_height_m: float = 0.0
    boundary_geojson: dict | None = field(default=None, repr=False)
    boundary_name: str = ""
    fetch_city: bool = True


class SiteStore:
    def __init__(self):
        self._sites: list[SiteModel] = []

    def add(self, site: SiteModel) -> None:
        self._sites.append(site)

    def remove(self, index: int) -> None:
        del self._sites[index]

    def update_priority(self, index: int, priority: int) -> None:
        self._sites[index].priority = priority

    def get(self, index: int) -> SiteModel:
        return self._sites[index]

    def to_list(self) -> list[dict]:
        result = []
        for s in self._sites:
            d = {"name": s.name, "lat": s.lat, "lon": s.lon,
                 "priority": s.priority, "fetch_city": s.fetch_city,
                 "site_height_m": s.site_height_m}
            if s.boundary_name:
                d["boundary_name"] = s.boundary_name
            result.append(d)
        return result

    def validate_priorities(self) -> None:
        """Raise ValueError if priorities have gaps (e.g. 1 and 3 but no 2)."""
        if not self._sites:
            return
        used = sorted({s.priority for s in self._sites})
        expected = list(range(used[0], used[-1] + 1))
        missing = set(expected) - set(used)
        if missing:
            raise ValueError(
                f"Priority gap: levels {sorted(missing)} have no sites. "
                f"Used priorities: {used}"
            )

    def __len__(self) -> int:
        return len(self._sites)

    def __iter__(self):
        return iter(self._sites)
