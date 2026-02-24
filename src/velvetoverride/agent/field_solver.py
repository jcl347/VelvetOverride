"""Hybrid field solver — config lookup first, LLM fallback for unknowns."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from velvetoverride.tracking.models import FieldType
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.agent.llm import LLMClient
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.utils.config import Config

log = get_logger(__name__)


class FieldSolver:
    """Resolves form field answers using a tiered strategy:

    1. Learned answers (from prior human corrections)
    2. Predetermined answers from answers.yaml config
    3. Profile data (personal info, technology experience)
    4. LLM fallback for genuinely novel questions
    """

    def __init__(self, config: Config, llm: LLMClient | None = None) -> None:
        self._config = config
        self._llm = llm
        self._answers = config.answers
        self._profile = config.profile

    async def solve(
        self,
        field: FormField,
        job_description: str = "",
        job_title: str = "",
        company: str = "",
    ) -> tuple[str | None, str, bool]:
        """Determine the answer for a form field.

        Returns: (answer, source, needs_review)
          - answer: the value to enter, or None if no answer could be determined
          - source: "learned", "config", "profile", "llm", or "skip"
          - needs_review: True if a human should verify this answer
        """
        label = field.label.lower().strip()

        # ── Tier 1: Learned answers ──
        learned = self._answers.get("learned", {})
        if learned:
            for question_text, entry in learned.items():
                if question_text.lower() in label or label in question_text.lower():
                    log.debug("solver.learned_match", label=field.label)
                    return entry["answer"], "learned", False

        # ── Tier 2: File upload → resume ──
        if field.field_type == FieldType.FILE_UPLOAD:
            return self._handle_file_upload(field)

        # ── Tier 3: Config-based answers ──
        config_answer = self._check_config(field)
        if config_answer is not None:
            return config_answer, "config", False

        # ── Tier 4: Profile data ──
        profile_answer = self._check_profile(field)
        if profile_answer is not None:
            return profile_answer, "profile", False

        # ── Tier 5: EEO handling ──
        eeo_answer = self._check_eeo(field)
        if eeo_answer is not None:
            return eeo_answer, "config", False

        # ── Tier 6: LLM fallback ──
        if self._llm:
            return self._ask_llm(field, job_description, job_title, company)

        log.warning("solver.no_answer", label=field.label)
        return None, "skip", False

    def _handle_file_upload(self, field: FormField) -> tuple[str | None, str, bool]:
        """Handle resume/file upload fields."""
        # The resume path is set by the orchestrator before applying
        # Return None here — the apply flow handles resume upload specially
        return None, "skip", False

    def _check_config(self, field: FormField) -> str | None:
        """Check predetermined answers in answers.yaml."""
        label = field.label.lower()

        # Yes/No questions
        yes_no = self._answers.get("yes_no", {})
        for _key, entry in yes_no.items():
            patterns = entry.get("patterns", [])
            if any(p.lower() in label for p in patterns):
                answer = entry.get("answer")
                if isinstance(answer, bool):
                    return "Yes" if answer else "No"
                return str(answer)

        # Numeric questions (years of experience)
        numeric = self._answers.get("numeric", {})
        exp_patterns = numeric.get("experience_patterns", [])
        if any(p.lower() in label for p in exp_patterns):
            return self._resolve_experience_years(label)

        # Salary
        salary = numeric.get("salary", {})
        if any(p.lower() in label for p in salary.get("patterns", [])):
            return salary.get("answer", "0")

        # Education
        education = self._answers.get("education", {})
        if any(p.lower() in label for p in education.get("degree_patterns", [])):
            return education.get("answer", "Bachelor's Degree")

        has_degree = education.get("has_degree", {})
        if any(p.lower() in label for p in has_degree.get("patterns", [])):
            answer = has_degree.get("answer", True)
            return "Yes" if answer else "No"

        return None

    def _check_profile(self, field: FormField) -> str | None:
        """Check if the field can be answered from profile data."""
        label = field.label.lower()
        personal = self._config.personal

        mappings = {
            "first name": personal.get("first_name"),
            "last name": personal.get("last_name"),
            "email": personal.get("email"),
            "phone": personal.get("phone"),
            "city": personal.get("city"),
            "state": personal.get("state"),
            "country": personal.get("country"),
            "zip": personal.get("zip"),
            "postal": personal.get("zip"),
        }

        for pattern, value in mappings.items():
            if pattern in label and value:
                return value

        # LinkedIn URL
        text_defaults = self._answers.get("text_defaults", {})
        linkedin_cfg = text_defaults.get("linkedin_url", {})
        if any(p.lower() in label for p in linkedin_cfg.get("patterns", [])):
            return personal.get("linkedin_url", "")

        # Website / GitHub / Portfolio
        website_cfg = text_defaults.get("website", {})
        if any(p.lower() in label for p in website_cfg.get("patterns", [])):
            return personal.get("website") or personal.get("github", "")

        # Phone country code (dropdown)
        if "country code" in label or "phone code" in label:
            return personal.get("phone_country_code", "United States (+1)")

        return None

    def _check_eeo(self, field: FormField) -> str | None:
        """Handle EEO / voluntary self-identification questions."""
        label = field.label.lower()
        eeo = self._answers.get("eeo", {})
        patterns = eeo.get("patterns", [])

        if not any(p.lower() in label for p in patterns):
            return None

        # For dropdowns, try to find a "decline" option
        if field.field_type == FieldType.DROPDOWN and field.options:
            decline_kws = eeo.get("decline_keywords", [])
            for option in field.options:
                if any(kw.lower() in option.lower() for kw in decline_kws):
                    return option
            # If no decline option found, return the last option (often "prefer not to say")
            return field.options[-1]

        # For radio buttons
        if field.field_type == FieldType.RADIO and field.options:
            decline_kws = eeo.get("decline_keywords", [])
            for option in field.options:
                if any(kw.lower() in option.lower() for kw in decline_kws):
                    return option

        return "Prefer not to say"

    def _resolve_experience_years(self, label: str) -> str:
        """Match a technology from the question to profile experience years."""
        tech_exp = self._config.technology_experience
        default = tech_exp.get("default", 1)

        label_lower = label.lower()
        for tech, years in tech_exp.items():
            if tech == "default":
                continue
            if tech.lower() in label_lower:
                return str(years)

        return str(default)

    def _ask_llm(
        self,
        field: FormField,
        job_description: str,
        job_title: str,
        company: str,
    ) -> tuple[str, str, bool]:
        """Fall back to Claude for novel questions."""
        assert self._llm is not None

        profile_summary = self._build_profile_summary()

        answer = self._llm.answer_field(
            question=field.label,
            field_type=field.field_type.value,
            options=field.options,
            job_description=job_description,
            job_title=job_title,
            company=company,
            profile_summary=profile_summary,
        )

        log.info("solver.llm_used", label=field.label, answer=answer[:60])
        # LLM answers always flagged for human review
        return answer, "llm", True

    def _build_profile_summary(self) -> str:
        """Build a concise profile summary for LLM context."""
        personal = self._config.personal
        profile = self._config.profile

        parts = [
            f"Name: {personal.get('first_name', '')} {personal.get('last_name', '')}",
            f"Location: {personal.get('city', '')}",
        ]

        summary = profile.get("summary", "")
        if summary:
            parts.append(f"Summary: {summary}")

        skills = profile.get("skills", {})
        if skills:
            all_skills = []
            for category_skills in skills.values():
                if isinstance(category_skills, list):
                    all_skills.extend(category_skills)
            if all_skills:
                parts.append(f"Skills: {', '.join(all_skills[:20])}")

        education = profile.get("education", [])
        if education:
            edu = education[0]
            parts.append(f"Education: {edu.get('degree', '')} in {edu.get('field', '')} from {edu.get('institution', '')}")

        experience = profile.get("experience", [])
        if experience:
            latest = experience[0]
            parts.append(f"Current role: {latest.get('title', '')} at {latest.get('company', '')}")

        return "\n".join(parts)

    def learn_answer(self, question: str, answer: str, field_type: str) -> None:
        """Store a human-corrected answer for future use.

        This implements the Answer Memory innovation — the bot gets smarter
        over time as humans correct its responses.
        """
        learned = self._answers.setdefault("learned", {})
        learned[question] = {
            "answer": answer,
            "field_type": field_type,
            "added_date": datetime.utcnow().strftime("%Y-%m-%d"),
        }
        log.info("solver.answer_learned", question=question[:60], answer=answer[:30])
