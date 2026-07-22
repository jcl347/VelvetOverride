"""Title-level seniority exclusion: skip e.g. 'Principal' roles while still
searching all experience levels."""

from velvetoverride.main import _title_excluded


def test_principal_excluded():
    for t in ("Principal Engineer", "Principal Data Scientist",
              "Staff/Principal ML Engineer", "principal software developer"):
        assert _title_excluded(t, ["principal"]) == "principal", t


def test_non_principal_not_excluded():
    for t in ("Senior Software Engineer", "Software Engineer",
              "Data Scientist", "Machine Learning Engineer", "Lead Engineer"):
        assert _title_excluded(t, ["principal"]) is None, t


def test_word_boundary_prevents_partial_match():
    # 'lead' must not match 'leadership'; must match standalone 'Lead'
    assert _title_excluded("Leadership Program Analyst", ["lead"]) is None
    assert _title_excluded("Lead Data Engineer", ["lead"]) == "lead"


def test_empty_blacklist_excludes_nothing():
    assert _title_excluded("Principal Engineer", []) is None
    assert _title_excluded("Principal Engineer", None) is None


def test_multiple_keywords():
    assert _title_excluded("Staff Engineer", ["principal", "staff"]) == "staff"
    assert _title_excluded("VP of Engineering", ["principal", "staff"]) is None


# ── location focus: Seattle metro OR remote ──

def _listing(loc):
    class L:
        location = loc
    L.location = loc
    return L()


def test_location_focus_keeps_seattle_and_remote():
    from velvetoverride.main import _location_in_focus
    from velvetoverride.utils.config import Config
    cfg = Config(settings={"search": {"location_focus": ["seattle", "bellevue", "redmond"]}})
    assert _location_in_focus(_listing("Seattle, WA (Hybrid)"), cfg)
    assert _location_in_focus(_listing("United States (Remote)"), cfg)
    assert _location_in_focus(_listing("Bellevue, WA (On-site)"), cfg)
    assert _location_in_focus(_listing("Remote"), cfg)


def test_location_focus_drops_out_of_area():
    from velvetoverride.main import _location_in_focus
    from velvetoverride.utils.config import Config
    cfg = Config(settings={"search": {"location_focus": ["seattle", "bellevue"]}})
    assert not _location_in_focus(_listing("Austin, TX (On-site)"), cfg)
    assert not _location_in_focus(_listing("New York, NY (On-site)"), cfg)


def test_no_focus_list_applies_everywhere():
    from velvetoverride.main import _location_in_focus
    from velvetoverride.utils.config import Config
    cfg = Config(settings={"search": {}})
    assert _location_in_focus(_listing("Austin, TX (On-site)"), cfg)


def test_unknown_location_not_over_filtered():
    from velvetoverride.main import _location_in_focus
    from velvetoverride.utils.config import Config
    cfg = Config(settings={"search": {"location_focus": ["seattle"]}})
    assert _location_in_focus(_listing(""), cfg)
