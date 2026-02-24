"""Export application tracking data to CSV/JSON for human review."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TYPE_CHECKING

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.tracking.database import TrackingDB

log = get_logger(__name__)


def export_csv(db: TrackingDB, output_path: str | Path) -> Path:
    """Export all applications to CSV."""
    path = Path(output_path) / "applications.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    applications = db.get_applications(limit=10000)

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ID", "Job URL", "Job Title", "Company", "Location",
            "Status", "Match Score", "Resume Version", "Applied At",
            "Salary Min", "Salary Max", "Salary Raw",
            "Questions Count", "Needs Review", "Notes",
        ])
        for app in applications:
            needs_review = sum(1 for q in app.questions if q.needs_review)
            writer.writerow([
                app.id, app.job_url, app.job_title, app.company,
                app.location, app.status, f"{app.match_score:.1f}",
                app.resume_version, app.applied_at,
                app.salary_min or "", app.salary_max or "", app.salary_raw,
                len(app.questions), needs_review, app.notes,
            ])

    log.info("export.csv", path=str(path), count=len(applications))
    return path


def export_json(db: TrackingDB, output_path: str | Path) -> Path:
    """Export all applications to JSON with full question details."""
    path = Path(output_path) / "applications.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    applications = db.get_applications(limit=10000)
    data = []
    for app in applications:
        data.append({
            "id": app.id,
            "job_url": app.job_url,
            "job_title": app.job_title,
            "company": app.company,
            "location": app.location,
            "job_description": app.job_description,
            "status": app.status,
            "match_score": app.match_score,
            "resume_version": app.resume_version,
            "applied_at": app.applied_at,
            "salary_min": app.salary_min,
            "salary_max": app.salary_max,
            "salary_raw": app.salary_raw,
            "notes": app.notes,
            "questions": [
                {
                    "question": q.question_text,
                    "field_type": q.field_type,
                    "answer": q.answer_given,
                    "source": q.answer_source,
                    "needs_review": q.needs_review,
                }
                for q in app.questions
            ],
        })

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    log.info("export.json", path=str(path), count=len(data))
    return path


def export_review_queue(db: TrackingDB, output_path: str | Path) -> Path:
    """Export only questions needing human review."""
    path = Path(output_path) / "review_queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    items = db.get_needs_review()
    with open(path, "w") as f:
        json.dump(items, f, indent=2, default=str)

    log.info("export.review_queue", path=str(path), count=len(items))
    return path
