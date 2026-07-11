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
    job_id: str = ""
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
    # ChatGPT's honest assessment of experience/seniority fit
    fit_score: int | None = None
    fit_seniority: str = ""      # under_qualified | match | over_qualified
    fit_recommend: str = ""      # apply | stretch | skip
    fit_reasoning: str = ""
    fit_gaps: str = ""           # requirements not met, "; "-joined
    questions: list[QuestionRecord] = field(default_factory=list)
    id: int | None = None


@dataclass
class JobListing:
    """A scraped job listing (pre-application)."""

    url: str
    title: str
    company: str
    job_id: str = ""
    location: str = ""
    description: str = ""
    posted_date: str = ""
    easy_apply: bool = True
    match_score: float = 0.0
    salary_min: int | None = None
    salary_max: int | None = None
    salary_raw: str = ""
    skills_extracted: list[str] = field(default_factory=list)
    # Populated by the LLM fit evaluation before applying
    fit_score: int | None = None
    fit_seniority: str = ""
    fit_recommend: str = ""
    fit_reasoning: str = ""
    fit_gaps: str = ""
