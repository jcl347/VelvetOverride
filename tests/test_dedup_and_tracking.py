"""Tests for job-ID dedup, error logging, and token-usage tracking."""

import pytest

from velvetoverride.agent.llm import Usage
from velvetoverride.tracking.database import TrackingDB
from velvetoverride.tracking.models import (
    ApplicationRecord,
    ApplicationStatus,
    QuestionRecord,
)


@pytest.fixture
def db(tmp_path):
    tracking_db = TrackingDB(tmp_path / "test.db")
    tracking_db.connect()
    yield tracking_db
    tracking_db.close()


def _rec(url, job_id, company="Acme", title="Data Scientist"):
    return ApplicationRecord(
        job_url=url, job_id=job_id, job_title=title, company=company,
        status=ApplicationStatus.DRY_RUN.value,
    )


# ── Dedup ──

def test_dedup_by_job_id(db):
    db.save_application(_rec("https://www.linkedin.com/jobs/view/555", "555"))
    # Different URL, same job id → already applied
    assert db.is_already_applied("https://www.linkedin.com/jobs/search/?x=1", "555")


def test_dedup_by_url(db):
    db.save_application(_rec("https://www.linkedin.com/jobs/view/555", "555"))
    assert db.is_already_applied("https://www.linkedin.com/jobs/view/555")


def test_not_dedup_new_job(db):
    db.save_application(_rec("https://www.linkedin.com/jobs/view/555", "555"))
    assert not db.is_already_applied("https://www.linkedin.com/jobs/view/777", "777")


def test_empty_job_id_does_not_false_match(db):
    # Two different jobs both with empty job_id must not collide
    db.save_application(_rec("https://x/a", ""))
    assert not db.is_already_applied("https://x/b", "")


def test_job_id_persisted(db):
    db.save_application(_rec("https://www.linkedin.com/jobs/view/555", "555"))
    apps = db.get_applications()
    assert apps[0].job_id == "555"


def test_failed_job_can_be_reapplied(db):
    """A FAILED application must not block a retry."""
    rec = _rec("https://www.linkedin.com/jobs/view/900", "900")
    rec.status = ApplicationStatus.FAILED.value
    db.save_application(rec)
    assert not db.is_already_applied("https://www.linkedin.com/jobs/view/900", "900")


def test_skipped_job_can_be_reapplied(db):
    rec = _rec("https://www.linkedin.com/jobs/view/901", "901")
    rec.status = ApplicationStatus.SKIPPED.value
    db.save_application(rec)
    assert not db.is_already_applied("https://www.linkedin.com/jobs/view/901", "901")


def test_applied_job_blocks_reapply(db):
    rec = _rec("https://www.linkedin.com/jobs/view/902", "902")
    rec.status = ApplicationStatus.APPLIED.value
    db.save_application(rec)
    assert db.is_already_applied("https://www.linkedin.com/jobs/view/902", "902")


def test_needs_review_job_blocks_reapply(db):
    """needs_review means it WAS submitted — don't double-apply."""
    rec = _rec("https://www.linkedin.com/jobs/view/903", "903")
    rec.status = ApplicationStatus.NEEDS_REVIEW.value
    db.save_application(rec)
    assert db.is_already_applied("https://www.linkedin.com/jobs/view/903", "903")


def test_retry_failed_job_upserts_not_duplicates(db):
    """Retrying a failed job must UPDATE the row, not violate UNIQUE(job_url)."""
    url = "https://www.linkedin.com/jobs/view/904"
    first = _rec(url, "904")
    first.status = ApplicationStatus.FAILED.value
    first.notes = "Max form steps exceeded"
    app_id = db.save_application(first)

    # Retry: same job, now succeeds. Must not raise, must reuse the same row.
    retry = _rec(url, "904")
    retry.status = ApplicationStatus.APPLIED.value
    retry.notes = ""
    retry_id = db.save_application(retry)

    assert retry_id == app_id
    apps = db.get_applications()
    assert len(apps) == 1
    assert apps[0].status == ApplicationStatus.APPLIED.value
    # And now it blocks further re-application
    assert db.is_already_applied(url, "904")


def test_retry_replaces_prior_answers(db):
    """A retry's questions replace the failed attempt's questions."""
    url = "https://www.linkedin.com/jobs/view/905"
    first = _rec(url, "905")
    first.status = ApplicationStatus.FAILED.value
    first.questions = [QuestionRecord("Old q?", "text", "old", "llm", True)]
    db.save_application(first)

    retry = _rec(url, "905")
    retry.status = ApplicationStatus.APPLIED.value
    retry.questions = [QuestionRecord("New q?", "text", "new", "config", False)]
    db.save_application(retry)

    apps = db.get_applications()
    assert len(apps) == 1
    assert [q.question_text for q in apps[0].questions] == ["New q?"]


# ── Error logging ──

def test_log_and_get_errors(db):
    rid = db.start_run(dry_run=True, max_apps=5)
    db.log_error(stage="apply", message="boom", run_id=rid,
                 error_type="ValueError", company="Acme", job_title="DS")
    assert db.error_count() == 1
    errs = db.get_errors()
    assert errs[0]["stage"] == "apply"
    assert errs[0]["error_type"] == "ValueError"
    assert errs[0]["run_id"] == rid


def test_get_errors_by_run(db):
    r1 = db.start_run(dry_run=True, max_apps=5)
    r2 = db.start_run(dry_run=True, max_apps=5)
    db.log_error(stage="apply", message="a", run_id=r1)
    db.log_error(stage="apply", message="b", run_id=r2)
    assert len(db.get_errors(run_id=r1)) == 1


# ── Token usage tracking ──

def test_record_run_usage(db):
    rid = db.start_run(dry_run=True, max_apps=5)
    db.record_run_usage(rid, prompt_tokens=100, completion_tokens=50,
                        total_tokens=150, llm_calls=3, est_cost_usd=0.01)
    run = db.get_runs(1)[0]
    assert run["total_tokens"] == 150
    assert run["llm_calls"] == 3
    assert run["est_cost_usd"] == 0.01


# ── Usage accounting (no API) ──

def test_usage_accumulates():
    u = Usage()
    u.add("gpt-4.1-mini", 1000, 200)
    u.add("gpt-4.1-mini", 500, 100)
    assert u.prompt_tokens == 1500
    assert u.completion_tokens == 300
    assert u.total_tokens == 1800
    assert u.calls == 2
    assert u.by_model["gpt-4.1-mini"]["calls"] == 2


def test_usage_cost_estimation():
    u = Usage()
    u.add("gpt-4.1-mini", 1_000_000, 1_000_000)  # 1M in, 1M out
    # gpt-4.1-mini pricing: $0.40 in + $1.60 out per 1M
    assert abs(u.est_cost_usd - 2.00) < 0.001


def test_usage_unknown_model_zero_cost():
    u = Usage()
    u.add("mystery-model", 1000, 1000)
    assert u.est_cost_usd == 0.0
    assert u.total_tokens == 2000
