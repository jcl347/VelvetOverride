"""ATS keyword match scorer — evaluates resume-to-JD alignment."""

from __future__ import annotations

import re
from typing import Any

from velvetoverride.utils.logging import get_logger

log = get_logger(__name__)


class ATSScorer:
    """Scores a resume's keyword coverage against a job description."""

    def __init__(self, target_coverage: float = 0.70) -> None:
        self._target_coverage = target_coverage

    def score(self, resume_data: dict[str, Any], jd_keywords: list[str]) -> float:
        """Score resume data against extracted JD keywords.

        Returns a score from 0 to 100.
        """
        if not jd_keywords:
            return 100.0

        # Build a normalized text blob from all resume content
        resume_text = self._extract_resume_text(resume_data).lower()

        # Count how many JD keywords appear in the resume
        matched = 0
        matched_keywords = []
        missing_keywords = []

        for keyword in jd_keywords:
            keyword_lower = keyword.lower().strip()
            if not keyword_lower:
                continue
            # Check for the keyword or common variations
            if self._keyword_present(keyword_lower, resume_text):
                matched += 1
                matched_keywords.append(keyword)
            else:
                missing_keywords.append(keyword)

        total = len(jd_keywords)
        coverage = matched / total if total > 0 else 1.0
        target = max(self._target_coverage, 1e-6)  # guard against a 0 target
        score = min(coverage / target * 100, 100)

        log.info(
            "ats.scored",
            score=f"{score:.1f}",
            coverage=f"{coverage:.1%}",
            matched=matched,
            total=total,
            missing=missing_keywords[:5],
        )

        return round(score, 1)

    def _extract_resume_text(self, resume_data: dict[str, Any]) -> str:
        """Flatten all resume data into searchable text."""
        parts = []

        # Summary
        parts.append(resume_data.get("summary", ""))

        # Experience bullets + per-role technologies (which the tailoring
        # pipeline injects specifically for ATS keyword coverage)
        for job in resume_data.get("experience", []):
            parts.append(job.get("title", ""))
            parts.append(job.get("company", ""))
            for bullet in job.get("bullets", []):
                if isinstance(bullet, str):
                    parts.append(bullet)
                elif isinstance(bullet, dict):
                    parts.append(bullet.get("text", ""))
            parts.extend(job.get("technologies", []) or [])

        # Projects (name, detail, tech)
        for proj in resume_data.get("projects", []):
            parts.append(proj.get("name", ""))
            parts.append(proj.get("detail", ""))
            parts.extend(proj.get("tech", []) or [])

        # Skills
        skills = resume_data.get("skills", {})
        for category, skill_list in skills.items():
            if isinstance(skill_list, list):
                parts.extend(skill_list)

        # Education
        for edu in resume_data.get("education", []):
            parts.append(edu.get("field", ""))
            parts.append(edu.get("degree", ""))
            parts.append(edu.get("institution", ""))

        # Certifications
        for cert in resume_data.get("certifications", []):
            parts.append(cert.get("name", ""))

        return " ".join(parts)

    def _keyword_present(self, keyword: str, text: str) -> bool:
        """Check if a keyword is present, handling common variations."""
        # Direct match
        if keyword in text:
            return True

        # Handle acronyms and abbreviations
        # e.g., "ci/cd" matches "ci cd" or "cicd"
        normalized = keyword.replace("/", " ").replace("-", " ")
        if normalized in text:
            return True

        no_space = keyword.replace(" ", "").replace("/", "").replace("-", "")
        if no_space and no_space in text.replace(" ", "").replace("/", "").replace("-", ""):
            return True

        return False

    def get_suggestions(self, resume_data: dict[str, Any], jd_keywords: list[str]) -> dict:
        """Return detailed scoring with improvement suggestions."""
        resume_text = self._extract_resume_text(resume_data).lower()

        matched = []
        missing = []
        for kw in jd_keywords:
            if self._keyword_present(kw.lower(), resume_text):
                matched.append(kw)
            else:
                missing.append(kw)

        coverage = len(matched) / len(jd_keywords) if jd_keywords else 1.0

        return {
            "score": self.score(resume_data, jd_keywords),
            "coverage": round(coverage * 100, 1),
            "matched_keywords": matched,
            "missing_keywords": missing,
            "suggestion": (
                "Consider adding these missing keywords naturally into your experience "
                "bullets or skills section."
                if missing
                else "Great keyword coverage!"
            ),
        }
