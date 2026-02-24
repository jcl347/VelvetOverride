"""Data models for application tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ApplicationStatus(Enum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    NEEDS_REVIEW = "needs_review"


class FieldType(Enum):
    TEXT = "text"
    NUMERIC = "numeric"
    RADIO = "radio"
    DROPDOWN = "dropdown"
    CHECKBOX = "checkbox"
    FILE_UPLOAD = "file_upload"
    TEXTAREA = "textarea"
    UNKNOWN = "unknown"


@dataclass
class QuestionRecord:
    """A single question/answer pair from an application form."""

    question_text: str
    field_type: str
    answer_given: str
    answer_source: str  # "config", "llm", "profile", "learned"
    needs_review: bool = False
    application_id: int | None = None
    id: int | None = None


@dataclass
class ApplicationRecord:
    """A single job application record."""

    job_url: str
    job_title: str
    company: str
    location: str = ""
    job_description: str = ""
    status: str = ApplicationStatus.APPLIED.value
    resume_version: str = ""
    match_score: float = 0.0
    applied_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    notes: str = ""
    screenshot_path: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_raw: str = ""
    questions: list[QuestionRecord] = field(default_factory=list)
    id: int | None = None


@dataclass
class JobListing:
    """A scraped job listing (pre-application)."""

    url: str
    title: str
    company: str
    location: str = ""
    description: str = ""
    posted_date: str = ""
    easy_apply: bool = True
    match_score: float = 0.0
    salary_min: int | None = None
    salary_max: int | None = None
    salary_raw: str = ""
    skills_extracted: list[str] = field(default_factory=list)
