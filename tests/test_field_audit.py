"""Tests for the field-routing audit heuristic and the DB methods that back
the run-end audit and the dashboard Feedback view."""

from __future__ import annotations

import pytest

from velvetoverride.tracking.audit import flag_routing_problem
from velvetoverride.tracking.database import TrackingDB
from velvetoverride.tracking.models import ApplicationRecord, QuestionRecord


@pytest.fixture
def db(tmp_path):
    d = TrackingDB(tmp_path / "t.db")
    d.connect()
    yield d
    d.close()


# ── flag_routing_problem heuristic ──

class TestFlagRoutingProblem:
    def test_clean_name_ok(self):
        assert flag_routing_problem("First name", "Jordan", "text", "profile") is None

    def test_name_with_sentence_flagged(self):
        problem = flag_routing_problem(
            "Name",
            "An experienced Data Analyst role focused on data mining and visualization.",
            "text", "llm",
        )
        assert problem and "name" in problem

    def test_name_with_digits_flagged(self):
        assert flag_routing_problem("Full name", "Jordan 98206", "text", "profile")

    def test_company_name_field_not_treated_as_person(self):
        # "company name" should not trip the person-name rule
        assert flag_routing_problem("Company name", "Acme Robotics Inc", "text", "profile") is None

    def test_valid_zip_ok(self):
        assert flag_routing_problem("Zip code", "98206", "text", "profile") is None

    def test_bad_zip_flagged(self):
        problem = flag_routing_problem("Postal code", "981", "text", "llm")
        assert problem and "zip" in problem

    def test_zip9_ok(self):
        assert flag_routing_problem("Zip", "981064321", "text", "profile") is None

    def test_email_without_at_flagged(self):
        assert flag_routing_problem("Email", "jordan.example.com", "text", "profile")

    def test_email_ok(self):
        assert flag_routing_problem("Email address", "a@b.com", "text", "profile") is None

    def test_phone_too_short_flagged(self):
        assert flag_routing_problem("Phone", "12345", "text", "profile")

    def test_phone_ok(self):
        assert flag_routing_problem("Mobile phone", "585-305-3419", "text", "profile") is None

    def test_phone_country_code_not_flagged(self):
        # country/area code fields are short by nature — must not trip phone rule
        assert flag_routing_problem("Phone country code", "+1", "text", "profile") is None

    def test_numeric_field_non_numeric_flagged(self):
        assert flag_routing_problem("Years of experience", "several", "numeric", "llm")

    def test_numeric_field_ok(self):
        assert flag_routing_problem("Years of experience", "5", "numeric", "profile") is None

    def test_long_llm_text_flagged(self):
        long = "x" * 200
        problem = flag_routing_problem("Why do you want this role", long, "text", "llm")
        assert problem and "LLM" in problem

    def test_long_textarea_llm_ok(self):
        # A long cover letter in a TEXTAREA is expected — only single-line text is flagged
        long = "x" * 200
        assert flag_routing_problem("Cover letter", long, "textarea", "llm") is None

    def test_empty_answer_not_flagged(self):
        assert flag_routing_problem("Name", "", "text", "profile") is None

    def test_missing_marker_not_flagged(self):
        assert flag_routing_problem("Name", "MISSING_PROFILE_FIELD", "text", "profile") is None


# ── get_run_questions / flag_question ──

def _seed(db, run_id, questions):
    rec = ApplicationRecord(
        job_url="https://www.linkedin.com/jobs/view/123",
        job_id="123", job_title="Data Scientist", company="Acme",
        status="applied", questions=questions,
    )
    return db.save_application(rec, run_id=run_id)


class TestRunQuestionAudit:
    def test_get_run_questions_returns_context(self, db):
        run_id = db.start_run(dry_run=False, max_apps=5)
        _seed(db, run_id, [
            QuestionRecord("First name", "text", "Jordan", "profile"),
            QuestionRecord("Name", "text", "A long sentence about a data role here.", "llm"),
        ])
        rows = db.get_run_questions(run_id)
        assert len(rows) == 2
        assert rows[0]["company"] == "Acme"
        assert rows[0]["job_title"] == "Data Scientist"
        assert {r["question_text"] for r in rows} == {"First name", "Name"}

    def test_flag_question_sets_needs_review(self, db):
        run_id = db.start_run(dry_run=False, max_apps=5)
        _seed(db, run_id, [QuestionRecord("Name", "text", "not a name 999", "llm")])
        rows = db.get_run_questions(run_id)
        qid = rows[0]["id"]
        assert rows[0]["needs_review"] == 0
        db.flag_question(qid)
        assert any(r["question_text"] == "Name" for r in db.get_needs_review())

    def test_audit_flow_flags_only_bad_rows(self, db):
        run_id = db.start_run(dry_run=False, max_apps=5)
        _seed(db, run_id, [
            QuestionRecord("First name", "text", "Jordan", "profile"),
            QuestionRecord("Zip code", "text", "12", "llm"),
        ])
        flagged = [
            r for r in db.get_run_questions(run_id)
            if flag_routing_problem(r["question_text"], r["answer_given"],
                                    r["field_type"], r["answer_source"])
        ]
        assert len(flagged) == 1
        assert flagged[0]["question_text"] == "Zip code"


class TestCoverLetters:
    def test_captures_textarea_and_cover_letter_source(self, db):
        run_id = db.start_run(dry_run=False, max_apps=5)
        _seed(db, run_id, [
            QuestionRecord("First name", "text", "Jordan", "profile"),   # excluded
            QuestionRecord("Cover letter", "textarea", "Dear team, I am...", "cover_letter"),
            QuestionRecord("Summary", "textarea", "I'm excited about this role because...", "llm"),
        ])
        letters = db.get_cover_letters()
        fields = {r["question_text"] for r in letters}
        assert "Cover letter" in fields
        assert "Summary" in fields
        assert "First name" not in fields  # short profile answer is not a letter

    def test_captures_long_llm_text(self, db):
        run_id = db.start_run(dry_run=False, max_apps=5)
        long = "I am looking for a challenging role that leverages my experience " * 3
        _seed(db, run_id, [
            QuestionRecord("I'm looking for…", "text", long, "llm"),
            QuestionRecord("Phone", "text", "585-305-3419", "profile"),  # excluded
        ])
        letters = db.get_cover_letters()
        fields = {r["question_text"] for r in letters}
        assert "I'm looking for…" in fields
        assert "Phone" not in fields

    def test_excludes_empty_answers(self, db):
        run_id = db.start_run(dry_run=False, max_apps=5)
        _seed(db, run_id, [QuestionRecord("Additional info", "textarea", "", "llm")])
        assert db.get_cover_letters() == []

    def test_endpoint_returns_letters(self, tmp_path):
        from velvetoverride.web import dashboard
        db_path = tmp_path / "endpoint.db"
        d = TrackingDB(db_path)
        d.connect()
        run_id = d.start_run(dry_run=False, max_apps=5)
        _seed(d, run_id, [
            QuestionRecord("Cover letter", "textarea", "Dear hiring team, ...", "cover_letter"),
        ])
        d.close()
        app = dashboard.create_app(str(db_path))
        resp = app.test_client().get("/api/cover_letters")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["company"] == "Acme"
        assert data[0]["text"].startswith("Dear hiring team")
        assert data[0]["field"] == "Cover letter"
