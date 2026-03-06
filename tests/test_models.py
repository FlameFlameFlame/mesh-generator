import pytest

from generator.models import SiteModel, SiteStore


def test_site_model_creation():
    site = SiteModel(name="Yerevan", lat=40.18, lon=44.51, priority=1, site_height_m=6.5)
    assert site.name == "Yerevan"
    assert site.lat == 40.18
    assert site.lon == 44.51
    assert site.priority == 1
    assert site.site_height_m == 6.5


def test_site_model_default_priority():
    site = SiteModel(name="Test", lat=0.0, lon=0.0)
    assert site.priority == 1
    assert site.site_height_m == 0.0


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


def test_site_store_iter():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1))
    store.add(SiteModel("B", 3.0, 4.0, 2))
    names = [s.name for s in store]
    assert names == ["A", "B"]


def test_site_store_to_list_includes_site_height():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1, site_height_m=7.0))
    out = store.to_list()
    assert out[0]["site_height_m"] == 7.0


def test_validate_priorities_ok():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1))
    store.add(SiteModel("B", 3.0, 4.0, 2))
    store.add(SiteModel("C", 5.0, 6.0, 3))
    store.validate_priorities()  # should not raise


def test_validate_priorities_gap():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 1))
    store.add(SiteModel("B", 3.0, 4.0, 3))
    with pytest.raises(ValueError, match="Priority gap"):
        store.validate_priorities()


def test_validate_priorities_empty():
    store = SiteStore()
    store.validate_priorities()  # should not raise


def test_validate_priorities_single():
    store = SiteStore()
    store.add(SiteModel("A", 1.0, 2.0, 2))
    store.validate_priorities()  # single priority, no gap
