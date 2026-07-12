"""Regression tests for fixes from the full-repo review."""

import sqlite3

import pytest

from velvetoverride.tracking.database import TrackingDB
from velvetoverride.tracking.models import ApplicationRecord, ApplicationStatus, QuestionRecord
from velvetoverride.utils.config import Config


# ── #4: migration-ordering — connecting to an upgraded DB must not crash ──

def test_connect_on_legacy_db_without_migrated_columns(tmp_path):
    """Simulate an old DB (applications without job_id/run_id) then connect()."""
    p = tmp_path / "legacy.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE applications (id INTEGER PRIMARY KEY, job_url TEXT UNIQUE, "
        "job_title TEXT, company TEXT, status TEXT, applied_at TEXT)"
    )
    con.commit()
    con.close()
    db = TrackingDB(p)
    db.connect()  # must not raise "no such column: job_id"
    # And the migrated index/columns now work
    assert not db.is_already_applied("http://x", "999")
    db.close()


# ── static resume selection (#own-resume feature) ──

def test_static_resume_prefers_configured_pdf(tmp_path):
    from velvetoverride.main import _static_resume
    pdf = tmp_path / "mine.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    c = Config(settings={"resume": {"static_resume_path": str(pdf)}})
    assert _static_resume(c) == str(pdf)


def test_static_resume_rejects_non_resume_ext(tmp_path):
    from velvetoverride.main import _static_resume
    html = tmp_path / "mine.html"
    html.write_text("<html></html>")
    c = Config(settings={"resume": {"static_resume_path": str(html)}})
    assert _static_resume(c) is None  # .html is not uploadable


def test_static_resume_none_when_unset():
    from velvetoverride.main import _static_resume
    assert _static_resume(Config(settings={})) is None


# ── #40: a zero-question retry must not wipe the earlier attempt's Q&A ──

@pytest.fixture
def db(tmp_path):
    d = TrackingDB(tmp_path / "t.db")
    d.connect()
    yield d
    d.close()


def test_retry_without_questions_preserves_prior_answers(db):
    url = "https://www.linkedin.com/jobs/view/500"
    first = ApplicationRecord(
        job_url=url, job_id="500", job_title="MLE", company="Acme",
        status=ApplicationStatus.FAILED.value,
        questions=[QuestionRecord("Q1?", "text", "a1", "llm", True)],
    )
    db.save_application(first)
    # Retry that recorded NO questions (e.g. crashed before filling)
    retry = ApplicationRecord(job_url=url, job_id="500", job_title="MLE",
                              company="Acme", status=ApplicationStatus.FAILED.value)
    db.save_application(retry)
    apps = db.get_applications()
    assert len(apps) == 1
    assert [q.question_text for q in apps[0].questions] == ["Q1?"]  # preserved


def test_retry_with_questions_replaces_prior(db):
    url = "https://www.linkedin.com/jobs/view/501"
    db.save_application(ApplicationRecord(
        job_url=url, job_id="501", job_title="X", company="Y",
        status=ApplicationStatus.FAILED.value,
        questions=[QuestionRecord("Old?", "text", "o", "llm", True)]))
    db.save_application(ApplicationRecord(
        job_url=url, job_id="501", job_title="X", company="Y",
        status=ApplicationStatus.APPLIED.value,
        questions=[QuestionRecord("New?", "text", "n", "config", False)]))
    apps = db.get_applications()
    assert [q.question_text for q in apps[0].questions] == ["New?"]


# ── #17/#20: consent checkboxes ticked; adverse yes/no NOT auto-Yes ──

def _solver():
    from velvetoverride.agent.field_solver import FieldSolver
    return FieldSolver(Config(profile={"personal": {}}, answers={}))


def test_consent_checkbox_detected():
    fs = _solver()
    assert fs._is_consent_checkbox("I agree to the terms and privacy policy")
    assert fs._is_consent_checkbox("I acknowledge and consent")
    assert not fs._is_consent_checkbox("Sign me up for marketing emails")


def test_adverse_yes_no_not_auto_yes():
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    fs = _solver()
    f = FormField(label="Have you ever been convicted of a felony?",
                  field_type=FieldType.RADIO, locator=None, options=["Yes", "No"])
    assert fs._check_yes_no_default(f) is None  # must NOT auto-answer "Yes"


def test_benign_yes_no_defaults_yes():
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    fs = _solver()
    f = FormField(label="Are you comfortable working in a team?",
                  field_type=FieldType.RADIO, locator=None, options=["Yes", "No"])
    assert fs._check_yes_no_default(f) == "Yes"


# ── #15: phone country code beats generic "country" mapping ──

def test_phone_country_code_not_shadowed_by_country():
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    from velvetoverride.agent.field_solver import FieldSolver
    c = Config(profile={"personal": {"country": "United States",
                                      "phone_country_code": "United States (+1)"}})
    fs = FieldSolver(c)
    f = FormField(label="Phone country code", field_type=FieldType.DROPDOWN,
                  locator=None, options=[])
    assert fs._check_profile(f) == "United States (+1)"


# ── #21/#30: location preference derives from the user's own config ──

def test_location_preference_uses_user_city():
    from velvetoverride.agent.field_solver import FieldSolver
    c = Config(profile={"personal": {"city": "Austin", "state": "Texas"}},
               settings={"search": {"locations": ["Austin, TX"], "remote": ["remote"]}})
    fs = FieldSolver(c)
    prefs = fs._location_preferences()
    assert "austin" in prefs
    assert "seattle" not in prefs  # no longer hardcoded


# ── #29: years-of-experience matches symbol-bearing tech names ──

def test_years_matches_cpp_and_csharp():
    from velvetoverride.agent.field_solver import FieldSolver
    c = Config(profile={"technology_experience": {"c++": 4, "c#": 3, "default": 1}})
    fs = FieldSolver(c)
    assert fs._resolve_experience_years("years of experience with C++") == "4"
    assert fs._resolve_experience_years("How many years with C#?") == "3"


# ── cover letter / summary routing ──

class _CoverLLM:
    def generate_cover_letter_snippet(self, title, company, jd, profile):
        return f"Compelling case for {title} at {company}."


def test_cover_letter_field_routed_to_llm():
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    from velvetoverride.agent.field_solver import FieldSolver
    fs = FieldSolver(Config(profile={"personal": {"first_name": "J"}}), _CoverLLM())
    f = FormField(label="Cover letter", field_type=FieldType.TEXTAREA, locator=None)
    out = fs._check_cover_letter(f, "JD text", "MLE", "Acme")
    assert out == "Compelling case for MLE at Acme."


def test_why_interested_routed_to_cover_letter():
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    from velvetoverride.agent.field_solver import FieldSolver
    fs = FieldSolver(Config(profile={"personal": {}}), _CoverLLM())
    f = FormField(label="Why are you interested in this role?",
                  field_type=FieldType.TEXTAREA, locator=None)
    assert fs._check_cover_letter(f, "JD", "MLE", "Acme") is not None


def test_non_cover_field_not_routed():
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    from velvetoverride.agent.field_solver import FieldSolver
    fs = FieldSolver(Config(profile={"personal": {}}), _CoverLLM())
    f = FormField(label="Phone number", field_type=FieldType.TEXT, locator=None)
    assert fs._check_cover_letter(f, "JD", "MLE", "Acme") is None
