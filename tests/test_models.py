from generator.models import SiteModel, SiteStore


def test_site_model_creation():
    site = SiteModel(name="Yerevan", lat=40.18, lon=44.51, priority=1)
    assert site.name == "Yerevan"
    assert site.lat == 40.18
    assert site.lon == 44.51
    assert site.priority == 1


def test_site_model_default_priority():
    site = SiteModel(name="Test", lat=0.0, lon=0.0)
    assert site.priority == 1


def test_site_store_add_remove():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1))
    store.add(SiteModel("B", 3.0, 4.0, 2))
    assert len(store) == 2

    store.remove(0)
    assert len(store) == 1
    assert store.get(0).name == "B"


def test_site_store_update_priority():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1))
    store.update_priority(0, 3)
    assert store.get(0).priority == 3


def test_site_store_to_list():
    store = SiteStore()
    store.add(SiteModel("A", 10.0, 20.0, 1))
    store.add(SiteModel("B", 30.0, 40.0, 2))
    result = store.to_list()
    assert result == [
        {"name": "A", "lat": 10.0, "lon": 20.0, "priority": 1},
        {"name": "B", "lat": 30.0, "lon": 40.0, "priority": 2},
    ]


def test_site_store_iter():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1))
    store.add(SiteModel("B", 3.0, 4.0, 2))
    names = [s.name for s in store]
    assert names == ["A", "B"]
