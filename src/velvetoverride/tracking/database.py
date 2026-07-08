"""SQLite database for application tracking."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
    run_id INTEGER DEFAULT NULL
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
    config_snapshot TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_applications_url ON applications(job_url);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_questions_review ON questions(needs_review);
CREATE INDEX IF NOT EXISTS idx_applications_run ON applications(run_id);
"""

# Migration: add columns to existing databases that lack them
MIGRATIONS = [
    "ALTER TABLE applications ADD COLUMN salary_min INTEGER DEFAULT NULL",
    "ALTER TABLE applications ADD COLUMN salary_max INTEGER DEFAULT NULL",
    "ALTER TABLE applications ADD COLUMN salary_raw TEXT DEFAULT ''",
    "ALTER TABLE applications ADD COLUMN run_id INTEGER DEFAULT NULL",
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._run_migrations()
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
                datetime.now(timezone.utc).isoformat(),
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
                datetime.now(timezone.utc).isoformat(),
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

    def get_runs(self, limit: int = 20) -> list[dict]:
        """Get recent run records."""
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Application tracking ──

    def is_already_applied(self, job_url: str) -> bool:
        """Check if we've already applied to this job URL."""
        row = self.conn.execute(
            "SELECT id FROM applications WHERE job_url = ?", (job_url,)
        ).fetchone()
        return row is not None

    def save_application(self, record: ApplicationRecord, run_id: int | None = None) -> int:
        """Insert an application record and its questions. Returns the ID."""
        cursor = self.conn.execute(
            """INSERT INTO applications
               (job_url, job_title, company, location, job_description,
                status, resume_version, match_score, applied_at, notes, screenshot_path,
                salary_min, salary_max, salary_raw, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.job_url,
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
