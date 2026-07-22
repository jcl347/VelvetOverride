"""SQLite database for application tracking."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from velvetoverride.tracking.models import ApplicationRecord, QuestionRecord
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_url TEXT UNIQUE NOT NULL,
    job_id TEXT DEFAULT '',
    job_title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT DEFAULT '',
    job_description TEXT DEFAULT '',
    status TEXT DEFAULT 'applied',
    resume_version TEXT DEFAULT '',
    match_score REAL DEFAULT 0.0,
    applied_at TEXT NOT NULL,
    notes TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',
    salary_min INTEGER DEFAULT NULL,
    salary_max INTEGER DEFAULT NULL,
    salary_raw TEXT DEFAULT '',
    run_id INTEGER DEFAULT NULL,
    fit_score INTEGER DEFAULT NULL,
    fit_seniority TEXT DEFAULT '',
    fit_recommend TEXT DEFAULT '',
    fit_reasoning TEXT DEFAULT '',
    fit_gaps TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    question_text TEXT NOT NULL,
    field_type TEXT DEFAULT 'unknown',
    answer_given TEXT DEFAULT '',
    answer_source TEXT DEFAULT 'unknown',
    needs_review INTEGER DEFAULT 0,
    FOREIGN KEY (application_id) REFERENCES applications(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT NULL,
    status TEXT DEFAULT 'running',
    dry_run INTEGER DEFAULT 1,
    max_apps INTEGER DEFAULT 25,
    min_salary INTEGER DEFAULT NULL,
    max_salary INTEGER DEFAULT NULL,
    listings_found INTEGER DEFAULT 0,
    listings_after_filter INTEGER DEFAULT 0,
    applied_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    error_message TEXT DEFAULT '',
    config_snapshot TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    llm_calls INTEGER DEFAULT 0,
    est_cost_usd REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    run_id INTEGER DEFAULT NULL,
    stage TEXT DEFAULT '',
    company TEXT DEFAULT '',
    job_title TEXT DEFAULT '',
    job_url TEXT DEFAULT '',
    error_type TEXT DEFAULT '',
    message TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_applications_url ON applications(job_url);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_questions_review ON questions(needs_review);
CREATE INDEX IF NOT EXISTS idx_errors_run ON errors(run_id);
"""

# Indexes on migration-added columns (job_id, run_id). These MUST be created
# AFTER _run_migrations(), because on a pre-existing (upgraded) database the
# columns don't exist yet when SCHEMA runs — creating them in SCHEMA would
# crash connect() with "no such column".
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_applications_jobid ON applications(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_applications_run ON applications(run_id)",
]

# Migration: add columns to existing databases that lack them
MIGRATIONS = [
    "ALTER TABLE applications ADD COLUMN salary_min INTEGER DEFAULT NULL",
    "ALTER TABLE applications ADD COLUMN salary_max INTEGER DEFAULT NULL",
    "ALTER TABLE applications ADD COLUMN salary_raw TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN run_id INTEGER DEFAULT NULL",
    "ALTER TABLE applications ADD COLUMN job_id TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN fit_score INTEGER DEFAULT NULL",
    "ALTER TABLE applications ADD COLUMN fit_seniority TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN fit_recommend TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN fit_reasoning TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN fit_gaps TEXT DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN prompt_tokens INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN completion_tokens INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN total_tokens INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN llm_calls INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN est_cost_usd REAL DEFAULT 0.0",
]


class TrackingDB:
    """SQLite-backed application tracking database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._run_migrations()
        # Indexes on migrated columns must be created only after the columns exist
        for sql in POST_MIGRATION_INDEXES:
            try:
                self._conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        self._conn.commit()
        log.info("tracking.db_connected", path=str(self._db_path))

    def _run_migrations(self) -> None:
        """Apply schema migrations for existing databases."""
        for sql in MIGRATIONS:
            try:
                self.conn.execute(sql)
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Run tracking ──

    def start_run(
        self,
        dry_run: bool,
        max_apps: int,
        min_salary: int | None = None,
        max_salary: int | None = None,
        config_snapshot: str = "",
    ) -> int:
        """Record the start of a bot run. Returns the run ID."""
        cursor = self.conn.execute(
            """INSERT INTO runs
               (started_at, dry_run, max_apps, min_salary, max_salary, config_snapshot)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                1 if dry_run else 0,
                max_apps,
                min_salary,
                max_salary,
                config_snapshot,
            ),
        )
        self.conn.commit()
        run_id = cursor.lastrowid
        assert run_id is not None
        log.info("tracking.run_started", run_id=run_id)
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str = "completed",
        listings_found: int = 0,
        listings_after_filter: int = 0,
        applied_count: int = 0,
        failed_count: int = 0,
        skipped_count: int = 0,
        error_message: str = "",
    ) -> None:
        """Record the end of a bot run."""
        self.conn.execute(
            """UPDATE runs SET
               finished_at = ?, status = ?,
               listings_found = ?, listings_after_filter = ?,
               applied_count = ?, failed_count = ?, skipped_count = ?,
               error_message = ?
               WHERE id = ?""",
            (
                datetime.utcnow().isoformat(),
                status,
                listings_found,
                listings_after_filter,
                applied_count,
                failed_count,
                skipped_count,
                error_message,
                run_id,
            ),
        )
        self.conn.commit()
        log.info("tracking.run_finished", run_id=run_id, status=status)

    def record_run_usage(
        self,
        run_id: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        llm_calls: int,
        est_cost_usd: float,
    ) -> None:
        """Persist LLM token usage for a run."""
        self.conn.execute(
            """UPDATE runs SET
               prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
               llm_calls = ?, est_cost_usd = ?
               WHERE id = ?""",
            (prompt_tokens, completion_tokens, total_tokens, llm_calls, est_cost_usd, run_id),
        )
        self.conn.commit()

    def get_runs(self, limit: int = 20) -> list[dict]:
        """Get recent run records."""
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Error tracking ──

    def log_error(
        self,
        stage: str,
        message: str,
        run_id: int | None = None,
        error_type: str = "",
        company: str = "",
        job_title: str = "",
        job_url: str = "",
    ) -> None:
        """Record an error encountered during a run for later inspection."""
        try:
            self.conn.execute(
                """INSERT INTO errors
                   (occurred_at, run_id, stage, company, job_title, job_url,
                    error_type, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.utcnow().isoformat(),
                    run_id,
                    stage,
                    company,
                    job_title,
                    job_url,
                    error_type,
                    message[:2000],
                ),
            )
            self.conn.commit()
        except Exception as e:  # never let error logging crash the bot
            log.warning("tracking.log_error_failed", error=str(e))

    def get_errors(self, limit: int = 100, run_id: int | None = None) -> list[dict]:
        """Retrieve recorded errors, most recent first."""
        if run_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM errors WHERE run_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM errors ORDER BY occurred_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def error_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]

    def count_errors_by_stage(self, stage: str) -> int:
        """Count recorded events at a given stage (e.g. 'nav_assist' — the
        AI-assisted dynamic button resolutions surfaced on the dashboard)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM errors WHERE stage = ?", (stage,)
        ).fetchone()[0]

    # ── Application tracking ──

    # Statuses that count as a real, successful application (block re-apply).
    # 'failed' and 'skipped' are intentionally excluded so we CAN retry them.
    _APPLIED_STATUSES = ("applied", "dry_run", "needs_review")

    def get_run_questions(self, run_id: int) -> list[dict]:
        """All Q&A recorded during a run, with the job context (for auditing)."""
        rows = self.conn.execute(
            """SELECT q.id, q.question_text, q.field_type, q.answer_given,
                      q.answer_source, q.needs_review, a.company, a.job_title
               FROM questions q JOIN applications a ON q.application_id = a.id
               WHERE a.run_id = ? ORDER BY q.id""",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def flag_question(self, question_id: int) -> None:
        """Mark a question as needing human review."""
        self.conn.execute(
            "UPDATE questions SET needs_review = 1 WHERE id = ?", (question_id,)
        )
        self.conn.commit()

    def get_cover_letters(self, limit: int = 200) -> list[dict]:
        """Long-form narrative answers the bot wrote (cover letters, summaries,
        'why interested', 'I'm looking for…'), with job context, newest first.

        Captures anything tagged ``cover_letter``, any textarea answer, or a
        long (>150 char) LLM answer — the prose a human would want to read and
        reuse.
        """
        rows = self.conn.execute(
            """SELECT q.id, q.question_text, q.field_type, q.answer_given,
                      q.answer_source, a.company, a.job_title, a.job_url,
                      a.applied_at
               FROM questions q JOIN applications a ON q.application_id = a.id
               WHERE ( q.answer_source = 'cover_letter'
                       OR (q.field_type = 'textarea' AND q.answer_source != 'config')
                       OR (q.answer_source = 'llm' AND LENGTH(q.answer_given) > 150) )
                 AND TRIM(COALESCE(q.answer_given, '')) != ''
                 -- exclude LinkedIn's "I'm looking for…" box (not a cover letter)
                 AND lower(q.question_text) NOT LIKE '%looking for%'
               ORDER BY a.applied_at DESC, q.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_already_applied(self, job_url: str, job_id: str | None = None) -> bool:
        """Check if we've already SUCCESSFULLY applied to this job.

        Dedup is by canonical LinkedIn job ID first, then normalized URL. Only
        successful applications (applied / dry_run / needs_review) block a
        re-apply — a previously FAILED job is allowed to be retried.
        """
        placeholders = ",".join("?" for _ in self._APPLIED_STATUSES)
        if job_id:
            row = self.conn.execute(
                f"SELECT id FROM applications WHERE job_id = ? AND job_id != '' "
                f"AND status IN ({placeholders})",
                (job_id, *self._APPLIED_STATUSES),
            ).fetchone()
            if row is not None:
                return True

        row = self.conn.execute(
            f"SELECT id FROM applications WHERE job_url = ? AND status IN ({placeholders})",
            (job_url, *self._APPLIED_STATUSES),
        ).fetchone()
        return row is not None

    def save_application(self, record: ApplicationRecord, run_id: int | None = None) -> int:
        """Insert (or update) an application record and its questions.

        ``job_url`` is UNIQUE, and previously-FAILED jobs are allowed to be
        retried, so a retry must UPDATE the existing row rather than insert a
        duplicate (which would raise a UNIQUE constraint error).
        """
        # Match on job_id first (consistent with is_already_applied), then URL —
        # so a job re-scraped under a slightly different URL updates its row
        # instead of inserting a duplicate.
        existing = None
        if record.job_id:
            existing = self.conn.execute(
                "SELECT id FROM applications WHERE job_id = ? AND job_id != ''",
                (record.job_id,),
            ).fetchone()
        if existing is None:
            existing = self.conn.execute(
                "SELECT id FROM applications WHERE job_url = ?", (record.job_url,)
            ).fetchone()

        if existing is not None:
            app_id = existing["id"]
            self.conn.execute(
                """UPDATE applications SET
                   job_id = ?, job_title = ?, company = ?, location = ?,
                   job_description = ?, status = ?, resume_version = ?,
                   match_score = ?, applied_at = ?, notes = ?, screenshot_path = ?,
                   salary_min = ?, salary_max = ?, salary_raw = ?, run_id = ?,
                   fit_score = ?, fit_seniority = ?, fit_recommend = ?,
                   fit_reasoning = ?, fit_gaps = ?
                   WHERE id = ?""",
                (
                    record.job_id, record.job_title, record.company, record.location,
                    record.job_description, record.status, record.resume_version,
                    record.match_score, record.applied_at, record.notes,
                    record.screenshot_path, record.salary_min, record.salary_max,
                    record.salary_raw, run_id,
                    record.fit_score, record.fit_seniority, record.fit_recommend,
                    record.fit_reasoning, record.fit_gaps, app_id,
                ),
            )
            # Replace the prior attempt's answers with this attempt's — but only
            # if the new attempt actually recorded questions, so a zero-question
            # retry can't wipe an earlier attempt's Q&A audit trail.
            if record.questions:
                self.conn.execute("DELETE FROM questions WHERE application_id = ?", (app_id,))
            log.info("tracking.updated", app_id=app_id, company=record.company, status=record.status)
        else:
            cursor = self.conn.execute(
                """INSERT INTO applications
                   (job_url, job_id, job_title, company, location, job_description,
                    status, resume_version, match_score, applied_at, notes, screenshot_path,
                    salary_min, salary_max, salary_raw, run_id,
                    fit_score, fit_seniority, fit_recommend, fit_reasoning, fit_gaps)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.job_url,
                    record.job_id,
                    record.job_title,
                    record.company,
                    record.location,
                    record.job_description,
                    record.status,
                    record.resume_version,
                    record.match_score,
                    record.applied_at,
                    record.notes,
                    record.screenshot_path,
                    record.salary_min,
                    record.salary_max,
                    record.salary_raw,
                    run_id,
                    record.fit_score,
                    record.fit_seniority,
                    record.fit_recommend,
                    record.fit_reasoning,
                    record.fit_gaps,
                ),
            )
            app_id = cursor.lastrowid
        assert app_id is not None

        for q in record.questions:
            self.conn.execute(
                """INSERT INTO questions
                   (application_id, question_text, field_type, answer_given,
                    answer_source, needs_review)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    app_id,
                    q.question_text,
                    q.field_type,
                    q.answer_given,
                    q.answer_source,
                    1 if q.needs_review else 0,
                ),
            )

        self.conn.commit()
        log.info(
            "tracking.saved",
            app_id=app_id,
            company=record.company,
            title=record.job_title,
            status=record.status,
        )
        return app_id

    def get_applications(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ApplicationRecord]:
        """Retrieve application records, optionally filtered by status."""
        query = "SELECT * FROM applications"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY applied_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            questions = self._get_questions(row["id"])
            results.append(
                ApplicationRecord(
                    id=row["id"],
                    job_url=row["job_url"],
                    job_id=row["job_id"] if "job_id" in row.keys() else "",
                    job_title=row["job_title"],
                    company=row["company"],
                    location=row["location"],
                    job_description=row["job_description"],
                    status=row["status"],
                    resume_version=row["resume_version"],
                    match_score=row["match_score"],
                    applied_at=row["applied_at"],
                    notes=row["notes"],
                    screenshot_path=row["screenshot_path"],
                    salary_min=row["salary_min"],
                    salary_max=row["salary_max"],
                    salary_raw=row["salary_raw"],
                    fit_score=row["fit_score"] if "fit_score" in row.keys() else None,
                    fit_seniority=row["fit_seniority"] if "fit_seniority" in row.keys() else "",
                    fit_recommend=row["fit_recommend"] if "fit_recommend" in row.keys() else "",
                    fit_reasoning=row["fit_reasoning"] if "fit_reasoning" in row.keys() else "",
                    fit_gaps=row["fit_gaps"] if "fit_gaps" in row.keys() else "",
                    questions=questions,
                )
            )
        return results

    def get_needs_review(self) -> list[dict]:
        """Get all questions flagged for human review."""
        rows = self.conn.execute(
            """SELECT q.*, a.job_title, a.company, a.job_url
               FROM questions q
               JOIN applications a ON q.application_id = a.id
               WHERE q.needs_review = 1
               ORDER BY a.applied_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def approve_answer(self, question_id: int) -> dict | None:
        """Mark a question as reviewed and return it for learning. Returns the Q&A pair."""
        row = self.conn.execute(
            "SELECT * FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE questions SET needs_review = 0 WHERE id = ?", (question_id,)
        )
        self.conn.commit()
        return dict(row)

    def get_stats(self) -> dict:
        """Return comprehensive summary statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        by_status = {}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM applications GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]

        review_count = self.conn.execute(
            "SELECT COUNT(*) FROM questions WHERE needs_review = 1"
        ).fetchone()[0]

        # Answer source breakdown
        by_source = {}
        for row in self.conn.execute(
            "SELECT answer_source, COUNT(*) as cnt FROM questions GROUP BY answer_source"
        ).fetchall():
            by_source[row["answer_source"]] = row["cnt"]

        # Salary statistics
        salary_stats = self.conn.execute(
            """SELECT
                COUNT(CASE WHEN salary_min IS NOT NULL THEN 1 END) as with_salary,
                COUNT(CASE WHEN salary_min IS NULL THEN 1 END) as without_salary,
                MIN(salary_min) as min_salary,
                MAX(salary_max) as max_salary,
                AVG(salary_min) as avg_min,
                AVG(salary_max) as avg_max
               FROM applications"""
        ).fetchone()

        # Failure reasons (top 5)
        failure_notes = self.conn.execute(
            """SELECT notes, COUNT(*) as cnt FROM applications
               WHERE status = 'failed' AND notes != ''
               GROUP BY notes ORDER BY cnt DESC LIMIT 5"""
        ).fetchall()

        # Recent runs
        recent_runs = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 5"
        ).fetchall()

        return {
            "total_applications": total,
            "by_status": by_status,
            "questions_needing_review": review_count,
            "answers_by_source": by_source,
            "salary": {
                "with_salary": salary_stats["with_salary"] if salary_stats else 0,
                "without_salary": salary_stats["without_salary"] if salary_stats else 0,
                "range_min": salary_stats["min_salary"] if salary_stats else None,
                "range_max": salary_stats["max_salary"] if salary_stats else None,
                "avg_min": round(salary_stats["avg_min"]) if salary_stats and salary_stats["avg_min"] else None,
                "avg_max": round(salary_stats["avg_max"]) if salary_stats and salary_stats["avg_max"] else None,
            },
            "top_failure_reasons": [
                {"reason": r["notes"], "count": r["cnt"]} for r in failure_notes
            ],
            "recent_runs": [dict(r) for r in recent_runs],
        }

    def get_fit_summary(self) -> dict:
        """Aggregate the LLM's experience-fit verdicts."""
        row = self.conn.execute(
            """SELECT COUNT(*) AS evaluated, AVG(fit_score) AS avg_score
               FROM applications WHERE fit_score IS NOT NULL"""
        ).fetchone()

        by_recommend: dict[str, int] = {}
        for r in self.conn.execute(
            """SELECT fit_recommend AS k, COUNT(*) AS n FROM applications
               WHERE fit_score IS NOT NULL AND fit_recommend != '' GROUP BY k"""
        ).fetchall():
            by_recommend[r["k"]] = r["n"]

        by_seniority: dict[str, int] = {}
        for r in self.conn.execute(
            """SELECT fit_seniority AS k, COUNT(*) AS n FROM applications
               WHERE fit_score IS NOT NULL AND fit_seniority != '' GROUP BY k"""
        ).fetchall():
            by_seniority[r["k"]] = r["n"]

        return {
            "evaluated": row["evaluated"] if row else 0,
            "avg_score": round(row["avg_score"]) if row and row["avg_score"] else None,
            "by_recommend": by_recommend,
            "by_seniority": by_seniority,
        }

    def get_fit_rows(self, limit: int = 300) -> list[dict]:
        """Per-job fit verdicts, best fit first."""
        rows = self.conn.execute(
            """SELECT company, job_title, job_url, status, fit_score, fit_seniority,
                      fit_recommend, fit_reasoning, fit_gaps, applied_at
               FROM applications
               WHERE fit_score IS NOT NULL
               ORDER BY fit_score DESC, applied_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_similar_jobs(self, title: str, company: str) -> list[ApplicationRecord]:
        """Find potentially duplicate job listings."""
        rows = self.conn.execute(
            """SELECT * FROM applications
               WHERE company = ? OR job_title LIKE ?
               ORDER BY applied_at DESC LIMIT 20""",
            (company, f"%{title}%"),
        ).fetchall()
        return [
            ApplicationRecord(
                id=r["id"],
                job_url=r["job_url"],
                job_title=r["job_title"],
                company=r["company"],
                location=r["location"],
                status=r["status"],
                match_score=r["match_score"],
                applied_at=r["applied_at"],
            )
            for r in rows
        ]

    def _get_questions(self, application_id: int) -> list[QuestionRecord]:
        rows = self.conn.execute(
            "SELECT * FROM questions WHERE application_id = ?", (application_id,)
        ).fetchall()
        return [
            QuestionRecord(
                id=r["id"],
                application_id=r["application_id"],
                question_text=r["question_text"],
                field_type=r["field_type"],
                answer_given=r["answer_given"],
                answer_source=r["answer_source"],
                needs_review=bool(r["needs_review"]),
            )
            for r in rows
        ]
