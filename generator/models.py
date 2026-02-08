from dataclasses import dataclass


@dataclass
class SiteModel:
    name: str
    lat: float
    lon: float
    priority: int = 1


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
        return [
            {"name": s.name, "lat": s.lat, "lon": s.lon, "priority": s.priority}
            for s in self._sites
        ]

    def __len__(self) -> int:
        return len(self._sites)

    def __iter__(self):
        return iter(self._sites)
