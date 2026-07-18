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


# ── experience-level override (-x/--experience) ──

def test_experience_override_sets_levels():
    c = _cfg()
    _apply_search_overrides(c, None, None, ["associate", "mid_senior"])
    assert c.search["experience_levels"] == ["associate", "mid_senior"]
    assert c.search["keywords"] == ["Data Scientist"]  # untouched


def test_experience_override_normalizes_case():
    c = _cfg()
    _apply_search_overrides(c, None, None, ["Associate", " MID_SENIOR "])
    assert c.search["experience_levels"] == ["associate", "mid_senior"]


def test_experience_override_drops_invalid_keeps_valid():
    c = _cfg()
    _apply_search_overrides(c, None, None, ["associate", "bogus_level"])
    assert c.search["experience_levels"] == ["associate"]


def test_experience_all_invalid_leaves_config_alone():
    c = Config(settings={"search": {"experience_levels": ["director"]}})
    _apply_search_overrides(c, None, None, ["nope"])
    assert c.search["experience_levels"] == ["director"]  # unchanged


def test_experience_override_builds_correct_fE():
    from velvetoverride.linkedin.search import build_search_url
    c = _cfg()
    _apply_search_overrides(c, ["Software Engineer"], None, ["associate", "mid_senior"])
    url = build_search_url(c, keyword="Software Engineer")
    assert "f_E=3%2C4" in url or "f_E=3,4" in url


# ── date-posted override (-d/--posted), incl. flexible past_<N>_days ──

def test_date_posted_param_presets():
    from velvetoverride.linkedin.search import date_posted_param
    assert date_posted_param("past_24h") == "r86400"
    assert date_posted_param("past_3_days") == "r259200"   # 3 * 86400
    assert date_posted_param("past_week") == "r604800"
    assert date_posted_param("past_month") == "r2592000"
    assert date_posted_param("any") == ""
    assert date_posted_param("") == ""


def test_date_posted_param_flexible_days():
    from velvetoverride.linkedin.search import date_posted_param
    assert date_posted_param("past_5_days") == "r432000"
    assert date_posted_param("past_2d") == "r172800"
    assert date_posted_param("past_10_day") == "r864000"
    assert date_posted_param("past_0_days") == ""   # nonsensical -> unset
    assert date_posted_param("garbage") == ""


def test_date_posted_override_sets_config():
    c = _cfg()
    _apply_search_overrides(c, None, None, None, "past_3_days")
    assert c.search["date_posted"] == "past_3_days"


def test_invalid_date_posted_leaves_config_alone():
    c = Config(settings={"search": {"date_posted": "past_week"}})
    _apply_search_overrides(c, None, None, None, "nonsense")
    assert c.search["date_posted"] == "past_week"


def test_date_posted_override_builds_correct_fTPR():
    from velvetoverride.linkedin.search import build_search_url
    c = _cfg()
    _apply_search_overrides(c, ["Software Engineer"], None, None, "past_3_days")
    url = build_search_url(c, keyword="Software Engineer")
    assert "f_TPR=r259200" in url
