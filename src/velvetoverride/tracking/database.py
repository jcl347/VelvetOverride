"""SQLite database for application tracking."""

from __future__ import annotations

import json
import sqlite3
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
    screenshot_path TEXT DEFAULT ''
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

CREATE INDEX IF NOT EXISTS idx_applications_url ON applications(job_url);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_questions_review ON questions(needs_review);
"""


class TrackingDB:
    """SQLite-backed application tracking database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        log.info("tracking.db_connected", path=str(self._db_path))

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

    def is_already_applied(self, job_url: str) -> bool:
        """Check if we've already applied to this job URL."""
        row = self.conn.execute(
            "SELECT id FROM applications WHERE job_url = ?", (job_url,)
        ).fetchone()
        return row is not None

    def save_application(self, record: ApplicationRecord) -> int:
        """Insert an application record and its questions. Returns the ID."""
        cursor = self.conn.execute(
            """INSERT INTO applications
               (job_url, job_title, company, location, job_description,
                status, resume_version, match_score, applied_at, notes, screenshot_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    def get_stats(self) -> dict:
        """Return summary statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        by_status = {}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM applications GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]

        review_count = self.conn.execute(
            "SELECT COUNT(*) FROM questions WHERE needs_review = 1"
        ).fetchone()[0]

        return {
            "total_applications": total,
            "by_status": by_status,
            "questions_needing_review": review_count,
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
