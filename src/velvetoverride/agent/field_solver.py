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

        # ── Tier 4b: Location / work-arrangement preference (Seattle or Remote) ──
        location_answer = self._check_location_preference(field)
        if location_answer is not None:
            return location_answer, "profile", False

        # ── Tier 5: EEO handling ──
        eeo_answer = self._check_eeo(field)
        if eeo_answer is not None:
            return eeo_answer, "config", False

        # ── Tier 5a: Required consent/terms checkboxes must be ticked ──
        # LinkedIn blocks the form with "Select checkbox to proceed" otherwise.
        if field.field_type == FieldType.CHECKBOX:
            log.debug("solver.checkbox_consent", label=field.label[:60])
            return "Yes", "config", False

        # ── Tier 5b: "Generally yes" default for unknown yes/no questions ──
        # Config negatives (sponsorship, non-compete, prior employee) already
        # matched above; anything still unresolved that is a yes/no field
        # defaults to Yes per user preference.
        yes_default = self._check_yes_no_default(field)
        if yes_default is not None:
            return yes_default, "config", True

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

        # Numeric questions (years of experience). If the technology isn't in
        # the profile, return None so the LLM answers it honestly rather than
        # emitting a blind default.
        numeric = self._answers.get("numeric", {})
        exp_patterns = numeric.get("experience_patterns", [])
        if any(p.lower() in label for p in exp_patterns):
            years = self._resolve_experience_years(label)
            if years is not None:
                return years
            if numeric.get("llm_for_unknown_tech", True) and self._llm:
                return None  # → Tier 6 LLM fallback
            return str(self._config.technology_experience.get("default", 1))

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

    def _check_yes_no_default(self, field: FormField) -> str | None:
        """Default unknown yes/no questions to 'Yes' (user preference).

        Only fires for radio/dropdown fields whose options are essentially
        Yes/No, so we don't put "Yes" into free-text or numeric fields.
        """
        if field.field_type not in (FieldType.RADIO, FieldType.DROPDOWN):
            return None
        options = field.options or []
        if not options:
            return None
        opt_low = {o.strip().lower() for o in options}
        yesno = {"yes", "no"}
        # Options are a yes/no set (allow an extra "maybe"/blank)
        if not (opt_low & yesno) or not opt_low.issubset(yesno | {"", "maybe", "n/a"}):
            return None
        for o in options:
            if o.strip().lower() == "yes":
                log.info("solver.yes_default", label=field.label)
                return o
        return None

    def _check_location_preference(self, field: FormField) -> str | None:
        """Answer location / work-arrangement questions with Seattle or Remote.

        For text fields returns the city; for dropdown/radio picks the option
        that matches Seattle, then Remote, then Washington.
        """
        label = field.label.lower()
        location_kw = (
            "preferred location", "work location", "which location", "office location",
            "location preference", "where would you like", "desired location",
            "work arrangement", "work model", "on-site or remote", "remote or",
        )
        if not any(k in label for k in location_kw):
            return None

        personal = self._config.personal
        city = personal.get("city", "Seattle")

        # For option-based fields, prefer Seattle, then Remote, then Washington
        if field.field_type in (FieldType.DROPDOWN, FieldType.RADIO) and field.options:
            for pref in ("seattle", "remote", "washington", "hybrid"):
                for opt in field.options:
                    if pref in opt.lower():
                        return opt
            return field.options[0]

        # Free-text location field
        return city

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

    def _resolve_experience_years(self, label: str) -> str | None:
        """Match a technology in the question to the profile's years.

        Uses word-boundary matching (so "ml" doesn't match "HTML" and "ai"
        doesn't match "email") and prefers the longest key ("machine learning"
        over "ml"). Returns None when no technology matches, so the caller can
        fall through to the LLM for an honest answer instead of guessing.
        """
        tech_exp = self._config.technology_experience
        label_lower = label.lower()

        candidates = [(t, y) for t, y in tech_exp.items() if t != "default"]
        # Longest key first: "machine learning" should win over "ml"
        candidates.sort(key=lambda kv: len(kv[0]), reverse=True)

        for tech, years in candidates:
            pattern = r"\b" + re.escape(str(tech).lower()) + r"\b"
            if re.search(pattern, label_lower):
                log.debug("solver.years_matched", tech=tech, years=years, label=label[:60])
                return str(years)

        log.info("solver.years_unknown_tech", label=label[:80])
        return None

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

        # Years-per-technology so "how many years of X?" is answered from real
        # data (and closely-related tech can be reasoned about honestly).
        tech = self._config.technology_experience
        if tech:
            years = ", ".join(f"{k}: {v}" for k, v in tech.items() if k != "default")
            parts.append(f"Years of experience per technology: {years}")

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
