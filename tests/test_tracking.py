"""Tests for the SQLite tracking database."""

import tempfile
from pathlib import Path

import pytest

from velvetoverride.tracking.database import TrackingDB
from velvetoverride.tracking.models import ApplicationRecord, ApplicationStatus, QuestionRecord


@pytest.fixture
def db(tmp_path):
    """Create a temporary tracking database."""
    db_path = tmp_path / "test_applications.db"
    tracking_db = TrackingDB(db_path)
    tracking_db.connect()
    yield tracking_db
    tracking_db.close()


def test_schema_creation(db):
    """Database schema should be created on connect."""
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {t["name"] for t in tables}
    assert "applications" in table_names
    assert "questions" in table_names
    assert "runs" in table_names


def test_save_and_retrieve(db):
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/123",
        job_title="Software Engineer",
        company="TestCorp",
        location="Remote",
        job_description="Build cool stuff",
        status=ApplicationStatus.APPLIED.value,
        match_score=85.5,
        questions=[
            QuestionRecord(
                question_text="Are you authorized?",
                field_type="radio",
                answer_given="Yes",
                answer_source="config",
            ),
        ],
    )

    app_id = db.save_application(record)
    assert app_id is not None
    assert app_id > 0

    # Retrieve
    apps = db.get_applications()
    assert len(apps) == 1
    assert apps[0].company == "TestCorp"
    assert apps[0].match_score == 85.5
    assert len(apps[0].questions) == 1
    assert apps[0].questions[0].answer_given == "Yes"


def test_deduplication(db):
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/456",
        job_title="Backend Dev",
        company="Acme",
    )
    db.save_application(record)

    assert db.is_already_applied("https://linkedin.com/jobs/view/456") is True
    assert db.is_already_applied("https://linkedin.com/jobs/view/789") is False


def test_stats(db):
    for i in range(3):
        db.save_application(ApplicationRecord(
            job_url=f"https://linkedin.com/jobs/view/{i}",
            job_title=f"Job {i}",
            company=f"Company {i}",
            status="applied" if i < 2 else "dry_run",
        ))

    stats = db.get_stats()
    assert stats["total_applications"] == 3
    assert stats["by_status"]["applied"] == 2
    assert stats["by_status"]["dry_run"] == 1


def test_needs_review(db):
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/999",
        job_title="ML Engineer",
        company="AI Corp",
        questions=[
            QuestionRecord(
                question_text="Describe your ML experience",
                field_type="textarea",
                answer_given="I have 5 years...",
                answer_source="llm",
                needs_review=True,
            ),
            QuestionRecord(
                question_text="Are you authorized?",
                field_type="radio",
                answer_given="Yes",
                answer_source="config",
                needs_review=False,
            ),
        ],
    )
    db.save_application(record)

    review = db.get_needs_review()
    assert len(review) == 1
    assert review[0]["question_text"] == "Describe your ML experience"


def test_find_similar_jobs(db):
    db.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/100",
        job_title="Senior Software Engineer",
        company="BigTech",
    ))
    db.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/101",
        job_title="Data Scientist",
        company="BigTech",
    ))

    similar = db.find_similar_jobs("Software Engineer", "BigTech")
    assert len(similar) == 2  # Both match by company


# ─── New tests for run tracking ────────────────────────────────


def test_start_and_finish_run(db):
    """Run tracking records start and finish correctly."""
    run_id = db.start_run(dry_run=True, max_apps=10, min_salary=80000, max_salary=150000)
    assert run_id is not None
    assert run_id > 0

    db.finish_run(
        run_id,
        status="completed",
        listings_found=50,
        listings_after_filter=30,
        applied_count=10,
        failed_count=2,
        skipped_count=5,
    )

    runs = db.get_runs()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "completed"
    assert runs[0]["dry_run"] == 1
    assert runs[0]["listings_found"] == 50
    assert runs[0]["applied_count"] == 10
    assert runs[0]["failed_count"] == 2
    assert runs[0]["skipped_count"] == 5
    assert runs[0]["finished_at"] is not None


def test_run_with_error(db):
    """Run that fails records error message."""
    run_id = db.start_run(dry_run=False, max_apps=25)
    db.finish_run(run_id, status="failed", error_message="Login failed")

    runs = db.get_runs()
    assert runs[0]["status"] == "failed"
    assert runs[0]["error_message"] == "Login failed"


def test_multiple_runs(db):
    """Multiple runs are tracked independently."""
    r1 = db.start_run(dry_run=True, max_apps=5)
    db.finish_run(r1, status="completed", applied_count=3)

    r2 = db.start_run(dry_run=False, max_apps=10)
    db.finish_run(r2, status="completed", applied_count=8)

    runs = db.get_runs()
    assert len(runs) == 2
    # Most recent first
    assert runs[0]["id"] == r2
    assert runs[0]["applied_count"] == 8
    assert runs[1]["id"] == r1
    assert runs[1]["applied_count"] == 3


def test_save_application_with_run_id(db):
    """Applications can be linked to a run."""
    run_id = db.start_run(dry_run=True, max_apps=10)

    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/200",
        job_title="Developer",
        company="RunCo",
    )
    app_id = db.save_application(record, run_id=run_id)

    # Verify run_id was saved
    row = db.conn.execute(
        "SELECT run_id FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["run_id"] == run_id


def test_save_application_without_run_id(db):
    """Applications can be saved without a run (backwards compat)."""
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/201",
        job_title="Developer",
        company="OldCo",
    )
    app_id = db.save_application(record)

    row = db.conn.execute(
        "SELECT run_id FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["run_id"] is None


# ─── Approve answer tests ─────────────────────────────────────


def test_approve_answer(db):
    """Approving a question clears the needs_review flag and returns data."""
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/300",
        job_title="Analyst",
        company="ReviewCo",
        questions=[
            QuestionRecord(
                question_text="What motivates you?",
                field_type="textarea",
                answer_given="Building impactful products",
                answer_source="llm",
                needs_review=True,
            ),
        ],
    )
    db.save_application(record)

    # Get the question needing review
    review_items = db.get_needs_review()
    assert len(review_items) == 1
    q_id = review_items[0]["id"]

    # Approve it
    result = db.approve_answer(q_id)
    assert result is not None
    assert result["question_text"] == "What motivates you?"
    assert result["answer_given"] == "Building impactful products"

    # Should no longer appear in review queue
    assert len(db.get_needs_review()) == 0


def test_approve_nonexistent_answer(db):
    """Approving a non-existent question returns None."""
    result = db.approve_answer(9999)
    assert result is None


# ─── Enriched stats tests ─────────────────────────────────────


def test_stats_answer_sources(db):
    """Stats should include answer source breakdown."""
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/400",
        job_title="Engineer",
        company="StatsCo",
        questions=[
            QuestionRecord(question_text="Q1", field_type="radio", answer_given="A1", answer_source="config"),
            QuestionRecord(question_text="Q2", field_type="text", answer_given="A2", answer_source="config"),
            QuestionRecord(question_text="Q3", field_type="textarea", answer_given="A3", answer_source="llm", needs_review=True),
            QuestionRecord(question_text="Q4", field_type="text", answer_given="A4", answer_source="profile"),
        ],
    )
    db.save_application(record)

    stats = db.get_stats()
    by_source = stats["answers_by_source"]
    assert by_source["config"] == 2
    assert by_source["llm"] == 1
    assert by_source["profile"] == 1


def test_stats_salary(db):
    """Stats should include salary statistics."""
    db.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/500",
        job_title="Dev 1",
        company="SalaryCo",
        salary_min=100000,
        salary_max=150000,
        salary_raw="$100K-$150K",
    ))
    db.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/501",
        job_title="Dev 2",
        company="SalaryCo",
        salary_min=120000,
        salary_max=180000,
        salary_raw="$120K-$180K",
    ))
    db.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/502",
        job_title="Dev 3",
        company="NoSalaryCo",
    ))

    stats = db.get_stats()
    salary = stats["salary"]
    assert salary["with_salary"] == 2
    assert salary["without_salary"] == 1
    assert salary["range_min"] == 100000
    assert salary["range_max"] == 180000
    assert salary["avg_min"] is not None


def test_stats_failure_reasons(db):
    """Stats should include top failure reasons."""
    for i in range(3):
        db.save_application(ApplicationRecord(
            job_url=f"https://linkedin.com/jobs/view/60{i}",
            job_title=f"Failed {i}",
            company="FailCo",
            status="failed",
            notes="Max form steps exceeded",
        ))
    db.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/603",
        job_title="Failed 3",
        company="FailCo",
        status="failed",
        notes="No Easy Apply button found",
    ))

    stats = db.get_stats()
    failures = stats["top_failure_reasons"]
    assert len(failures) >= 1
    assert failures[0]["reason"] == "Max form steps exceeded"
    assert failures[0]["count"] == 3


def test_stats_recent_runs(db):
    """Stats should include recent runs."""
    r1 = db.start_run(dry_run=True, max_apps=5)
    db.finish_run(r1, status="completed", applied_count=3)

    stats = db.get_stats()
    assert len(stats["recent_runs"]) == 1
    assert stats["recent_runs"][0]["applied_count"] == 3


# ─── Migration tests ──────────────────────────────────────────


def test_reconnect_idempotent(tmp_path):
    """Connecting twice to the same DB should not fail."""
    db_path = tmp_path / "idem_test.db"
    db1 = TrackingDB(db_path)
    db1.connect()
    db1.save_application(ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/700",
        job_title="Test",
        company="IdempotentCo",
    ))
    db1.close()

    # Re-open — migrations should handle existing columns gracefully
    db2 = TrackingDB(db_path)
    db2.connect()
    apps = db2.get_applications()
    assert len(apps) == 1
    assert apps[0].company == "IdempotentCo"
    db2.close()


def test_salary_fields_in_application(db):
    """Salary fields should be stored and retrieved correctly."""
    record = ApplicationRecord(
        job_url="https://linkedin.com/jobs/view/800",
        job_title="Paid Dev",
        company="MoneyCo",
        salary_min=95000,
        salary_max=140000,
        salary_raw="$95K-$140K per year",
    )
    db.save_application(record)

    apps = db.get_applications()
    assert apps[0].salary_min == 95000
    assert apps[0].salary_max == 140000
    assert apps[0].salary_raw == "$95K-$140K per year"
