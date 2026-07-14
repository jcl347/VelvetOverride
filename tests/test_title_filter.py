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
