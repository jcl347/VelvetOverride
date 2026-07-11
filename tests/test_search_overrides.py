"""CLI overrides for target roles and locations must win over settings.yaml."""

from velvetoverride.main import _apply_search_overrides
from velvetoverride.utils.config import Config


def _cfg():
    return Config(settings={"search": {"keywords": ["Data Scientist"], "locations": ["Seattle"]}})


def test_keyword_override_replaces_config():
    c = _cfg()
    _apply_search_overrides(c, ["AI Engineer", "ML Engineer"], None)
    assert c.search["keywords"] == ["AI Engineer", "ML Engineer"]
    assert c.search["locations"] == ["Seattle"]  # untouched


def test_location_override_replaces_config():
    c = _cfg()
    _apply_search_overrides(c, None, ["Remote", "New York"])
    assert c.search["locations"] == ["Remote", "New York"]
    assert c.search["keywords"] == ["Data Scientist"]  # untouched


def test_both_overrides():
    c = _cfg()
    _apply_search_overrides(c, ["Backend Engineer"], ["Austin, TX"])
    assert c.search["keywords"] == ["Backend Engineer"]
    assert c.search["locations"] == ["Austin, TX"]


def test_no_override_leaves_config_alone():
    c = _cfg()
    _apply_search_overrides(c, None, None)
    assert c.search["keywords"] == ["Data Scientist"]
    assert c.search["locations"] == ["Seattle"]


def test_empty_lists_are_noops():
    c = _cfg()
    _apply_search_overrides(c, [], [])
    assert c.search["keywords"] == ["Data Scientist"]


def test_override_builds_correct_search_url():
    from velvetoverride.linkedin.search import build_search_url
    c = _cfg()
    _apply_search_overrides(c, ["AI Engineer"], ["Remote"])
    url = build_search_url(c, keyword="AI Engineer")
    assert "keywords=AI+Engineer" in url
    assert "location=Remote" in url


def test_override_on_config_without_search_section():
    c = Config(settings={})
    _apply_search_overrides(c, ["AI Engineer"], ["Remote"])
    assert c.search["keywords"] == ["AI Engineer"]
    assert c.search["locations"] == ["Remote"]
