"""Tests for the ChatGPT experience-fit verdict and years-of-experience resolution."""

import pytest

from velvetoverride.agent.llm import LLMClient
from velvetoverride.linkedin.apply import ApplicationFlow


# ── Fit verdict parsing (defensive: model output is untrusted) ──

def test_parse_fit_clean_json():
    raw = ('{"fit_score": 82, "seniority": "match", "recommend": "apply", '
           '"gaps": ["AWS"], "reasoning": "Strong ML background."}')
    v = LLMClient._parse_fit(raw)
    assert v["fit_score"] == 82
    assert v["seniority"] == "match"
    assert v["recommend"] == "apply"
    assert v["gaps"] == ["AWS"]


def test_parse_fit_strips_markdown_fence():
    raw = '```json\n{"fit_score": 30, "seniority": "under_qualified", "recommend": "skip"}\n```'
    v = LLMClient._parse_fit(raw)
    assert v["fit_score"] == 30
    assert v["recommend"] == "skip"


def test_parse_fit_clamps_out_of_range_score():
    assert LLMClient._parse_fit('{"fit_score": 900}')["fit_score"] == 100
    assert LLMClient._parse_fit('{"fit_score": -20}')["fit_score"] == 0


def test_parse_fit_rejects_invalid_enums():
    v = LLMClient._parse_fit('{"seniority": "godlike", "recommend": "definitely"}')
    assert v["seniority"] == "match"       # falls back to default
    assert v["recommend"] == "stretch"


def test_parse_fit_survives_garbage():
    for raw in ("", "not json at all", "{broken", None):
        v = LLMClient._parse_fit(raw)
        assert v["fit_score"] == 50  # neutral default, never raises


def test_parse_fit_handles_prose_around_json():
    raw = 'Here is my assessment:\n{"fit_score": 65, "recommend": "stretch"}\nHope that helps.'
    v = LLMClient._parse_fit(raw)
    assert v["fit_score"] == 65


# ── Years-of-experience resolution ──

class _Cfg:
    def __init__(self, tech):
        self.technology_experience = tech
        self.profile = {}
        self.answers = {}
        self.personal = {}


def _solver(tech, llm=None):
    from velvetoverride.agent.field_solver import FieldSolver
    return FieldSolver(_Cfg(tech), llm)


TECH = {"python": 5, "ml": 5, "machine learning": 5, "ai": 4, "azure": 3, "default": 2}


def test_longest_key_wins():
    s = _solver(TECH)
    assert s._resolve_experience_years("years of experience with machine learning") == "5"


def test_ml_does_not_match_html():
    """The bug word-boundary matching prevents: 'ml' inside 'HTML'."""
    s = _solver(TECH)
    assert s._resolve_experience_years("How many years of experience with HTML?") is None


def test_ai_does_not_match_email():
    s = _solver(TECH)
    assert s._resolve_experience_years("years of experience with email marketing") is None


def test_standalone_ml_matches():
    s = _solver(TECH)
    assert s._resolve_experience_years("How many years of experience do you have with ML?") == "5"


def test_unknown_tech_returns_none_for_llm_fallback():
    s = _solver(TECH)
    assert s._resolve_experience_years("years of experience with Forward Deployment Engineering") is None


def test_known_tech_case_insensitive():
    s = _solver(TECH)
    assert s._resolve_experience_years("Years of experience with AZURE?") == "3"


# ── Validation-error numeric cleaning (the decimal inversion bug) ──

def test_decimal_error_must_not_round_to_whole():
    """'Enter a decimal number larger than 0.0' wants a DECIMAL, not an integer."""
    assert ApplicationFlow._clean_numeric("2.5", whole=False) == "2.5"


def test_whole_number_error_strips_decimal():
    assert ApplicationFlow._clean_numeric("2.5", whole=True) == "2"


def test_clean_numeric_strips_commas_and_text():
    assert ApplicationFlow._clean_numeric("about 1,500 hrs", whole=True) == "1500"


def test_clean_numeric_no_digits():
    assert ApplicationFlow._clean_numeric("N/A", whole=True) == ""
    assert ApplicationFlow._clean_numeric(None, whole=True) == ""


# ── Fit persistence + dashboard aggregates ──

@pytest.fixture
def db(tmp_path):
    from velvetoverride.tracking.database import TrackingDB
    d = TrackingDB(tmp_path / "fit.db")
    d.connect()
    yield d
    d.close()


def _app(url, score, sen, rec, gaps="", company="Acme"):
    from velvetoverride.tracking.models import ApplicationRecord
    return ApplicationRecord(
        job_url=url, job_title="MLE", company=company, status="applied",
        fit_score=score, fit_seniority=sen, fit_recommend=rec,
        fit_reasoning="because", fit_gaps=gaps,
    )


def test_fit_verdict_roundtrips(db):
    db.save_application(_app("u1", 82, "match", "apply", "AWS; Kubernetes"))
    a = db.get_applications()[0]
    assert a.fit_score == 82
    assert a.fit_seniority == "match"
    assert a.fit_recommend == "apply"
    assert a.fit_gaps == "AWS; Kubernetes"


def test_fit_summary_aggregates(db):
    db.save_application(_app("u1", 80, "match", "apply"))
    db.save_application(_app("u2", 40, "under_qualified", "skip"))
    db.save_application(_app("u3", 60, "match", "stretch"))
    s = db.get_fit_summary()
    assert s["evaluated"] == 3
    assert s["avg_score"] == 60
    assert s["by_recommend"] == {"apply": 1, "skip": 1, "stretch": 1}
    assert s["by_seniority"]["match"] == 2


def test_fit_summary_ignores_unevaluated(db):
    from velvetoverride.tracking.models import ApplicationRecord
    db.save_application(ApplicationRecord(job_url="u9", job_title="X", company="Y"))
    s = db.get_fit_summary()
    assert s["evaluated"] == 0
    assert s["avg_score"] is None


def test_fit_rows_sorted_best_first(db):
    db.save_application(_app("u1", 40, "under_qualified", "skip", company="Low"))
    db.save_application(_app("u2", 90, "match", "apply", company="High"))
    rows = db.get_fit_rows()
    assert [r["company"] for r in rows] == ["High", "Low"]


def test_fit_api_endpoints(tmp_path):
    """The website must expose fit data."""
    from velvetoverride.tracking.database import TrackingDB
    from velvetoverride.web.dashboard import create_app
    p = tmp_path / "d.db"
    d = TrackingDB(p); d.connect()
    d.save_application(_app("u1", 77, "match", "apply", "AWS"))
    d.close()

    c = create_app(str(p)).test_client()
    fit = c.get("/api/fit").get_json()
    assert fit[0]["fit_score"] == 77
    assert fit[0]["fit_gaps"] == "AWS"

    summary = c.get("/api/summary").get_json()
    assert summary["fit"]["evaluated"] == 1
    assert summary["fit"]["avg_score"] == 77
