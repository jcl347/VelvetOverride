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
