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

        # ── Tier 0: Intentionally-skipped fields ──
        # Optional prompts the user does NOT want auto-answered (e.g. LinkedIn's
        # "I'm looking for…" preferences box). Left blank instead of getting a
        # generated paragraph. Extendable via answers.yaml `skip_fields`.
        if self._should_skip_field(label):
            log.info("solver.skip_field", label=field.label[:60])
            return None, "skip", False

        # ── Tier 1: Learned answers ──
        # Require a strong match (normalized equality, or one string containing
        # the other AND the shorter being reasonably long) to avoid a short
        # learned key poisoning unrelated fields. Tolerate malformed entries.
        learned = self._answers.get("learned", {})
        # Never match learned answers for an unidentified field — the sentinel
        # "unknown_field" would otherwise let one learned entry answer every
        # unlabeled control (and bypass the unlabeled-checkbox safety below).
        if learned and label and label != "unknown_field":
            for question_text, entry in learned.items():
                if not isinstance(entry, dict) or "answer" not in entry:
                    continue
                q = str(question_text).lower().strip()
                if not q or len(q) < 6:
                    continue
                # Containment match requires the SHORTER string to be substantial,
                # otherwise an empty/short label ("" is a substring of everything)
                # would inherit an unrelated learned answer.
                strong = q == label or (
                    min(len(q), len(label)) >= 12 and (q in label or label in q)
                )
                if strong:
                    log.debug("solver.learned_match", label=field.label)
                    return entry.get("answer"), "learned", False

        # ── Tier 2: File upload → resume ──
        if field.field_type == FieldType.FILE_UPLOAD:
            return self._handle_file_upload(field)

        # ── Tier 3: Config-based answers ──
        config_answer = self._check_config(field)
        if config_answer is not None:
            return config_answer, "config", False

        # ── Tier 3b: Employer / affiliation → "No" ──
        # "Are you a current employee / affiliated with / related to someone at
        # <company>?" is answered No, and an employment/affiliation checkbox
        # option is left unchecked. Runs before the "generally Yes" default and
        # the LLM so these never get a wrong affirmative.
        affiliation = self._check_affiliation(field)
        if affiliation is not None:
            log.info("solver.affiliation_no", label=field.label[:70])
            return affiliation, "config", False

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

        # ── Tier 5a: Consent/terms checkboxes get ticked ──
        # Only checkboxes whose label looks like a consent/acknowledgement are
        # auto-ticked (LinkedIn blocks the form with "Select checkbox to proceed"
        # otherwise). Other checkboxes fall through to the LLM so we don't blindly
        # opt into unrelated things (marketing, "I am a veteran", etc.).
        if field.field_type == FieldType.CHECKBOX and self._is_consent_checkbox(label):
            log.debug("solver.checkbox_consent", label=field.label[:60])
            return "Yes", "config", False

        # ── Tier 5a-bis: Safety — never blindly TICK a checkbox we can't read ──
        # An unlabeled / unidentifiable checkbox must stay UNCHECKED. Left to the
        # LLM it answers "Yes" for an empty prompt and opts the user into
        # something unknown (marketing, an affiliation, a false certification).
        if field.field_type == FieldType.CHECKBOX:
            lbl = (label or "").strip()
            if not lbl or lbl in ("unknown_field",) or len(lbl) < 3:
                log.info("solver.checkbox_unlabeled_unchecked", label=field.label[:60])
                return "No", "config", True

        # ── Tier 5b: "Generally yes" default for unknown yes/no questions ──
        # Config negatives (sponsorship, non-compete, prior employee) already
        # matched above; anything still unresolved that is a yes/no field
        # defaults to Yes per user preference.
        yes_default = self._check_yes_no_default(field)
        if yes_default is not None:
            return yes_default, "config", True

        # ── Tier 5c: Cover letter / summary / "why interested" ──
        # Make a compelling case from the applicant's real background + the JD.
        cover = self._check_cover_letter(field, job_description, job_title, company)
        if cover is not None:
            return cover, "cover_letter", True

        # ── Tier 6: LLM fallback ──
        if self._llm:
            return self._ask_llm(field, job_description, job_title, company)

        log.warning("solver.no_answer", label=field.label)
        return None, "skip", False

    def _check_cover_letter(
        self, field: FormField, job_description: str, job_title: str, company: str
    ) -> str | None:
        """For cover-letter / summary / motivation textareas, generate a
        compelling answer grounded in the applicant's real background + the JD."""
        if self._llm is None:
            return None
        label = field.label.lower()
        # Never treat a contact/identity/factual field as a cover-letter prompt.
        # These must be answered precisely (or from profile), not with a
        # narrative — this is what keeps non-cover-letter questions from getting
        # an inappropriate essay.
        if any(x in label for x in (
            "name", "email", "phone", "zip", "postal", "city", "state",
            "country", "address", "url", "linkedin", "github", "website",
            "salary", "compensation", "how many years", "years of experience",
            "notice period", "start date", "today's date", "date of birth",
            "date available", "available date", "postal code", "area code",
            "willing to", "authorized", "sponsorship", "relocate", "gpa",
        )):
            return None
        # Genuine open-ended / motivation prompts that deserve a compelling
        # narrative. Anything not matching falls through to the plain LLM
        # answerer, which addresses the specific question without an essay.
        explicit = ("cover letter", "letter of interest")
        cover_kw = explicit + (
            "why are you interested", "why do you want", "why this role",
            "why this company", "why should we", "why you", "tell us why",
            "what interests you", "motivat", "what makes you", "good fit",
            "why are you a", "why would you", "tell us about yourself",
            "message to", "note to the", "cover note", "pitch",
            "anything else you", "additional information",
        )
        if field.field_type == FieldType.TEXTAREA:
            if not any(k in label for k in cover_kw):
                return None
        elif field.field_type == FieldType.TEXT:
            if not any(k in label for k in explicit):
                return None
        else:
            return None
        try:
            text = self._llm.generate_cover_letter_snippet(
                field.label, job_title, company, job_description,
                self._build_profile_summary(),
            )
            log.info("solver.cover_letter", label=field.label[:50], chars=len(text or ""))
            return text or None
        except Exception as e:
            log.warning("solver.cover_letter_failed", error=str(e)[:80])
            return None

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
                    # A Yes/No answer belongs only in a choice field. A textarea
                    # like "Please list companies you have worked at" happens to
                    # contain "worked at" — do NOT type "No" into it.
                    if field.field_type not in (
                        FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX
                    ):
                        continue
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

    # Optional prompts left blank by default (not required for submission).
    # Narrow on purpose: only LinkedIn's "I'm looking for…" self-statement box,
    # NOT genuine questions like "what are you looking for in a role" (which the
    # cover-letter tier can answer well). Extend via answers.yaml `skip_fields`.
    _DEFAULT_SKIP_PATTERNS = (
        "i'm looking for", "i am looking for", "im looking for",
    )

    def _should_skip_field(self, label: str) -> bool:
        """True if this field should be left blank rather than auto-answered."""
        patterns = list(self._DEFAULT_SKIP_PATTERNS)
        # User-extendable list from answers.yaml (strings, case-insensitive).
        extra = self._answers.get("skip_fields", []) or []
        patterns += [str(p).lower() for p in extra if str(p).strip()]
        return any(p in label for p in patterns)

    # Phrases that mean "are you an employee of / affiliated with / related to
    # someone at the hiring company?" — the applicant is not, so answer No.
    _AFFILIATION_QUESTIONS = (
        "are you a current employee", "are you an employee", "currently an employee",
        "current or former employee", "are you a former employee", "former employee",
        "are you currently employed by", "currently employed by", "employed by this",
        "do you currently work for", "do you work for", "are you affiliated",
        "affiliation with", "any affiliation", "do you have an affiliation",
        "have an affiliation", "are you related to", "related to anyone",
        "immediate family member", "family member who", "do you have a relationship with",
        "relationship to this", "referred by a current", "referred by an employee",
        "connected to anyone",
    )
    # Single-word-ish checkbox OPTIONS that assert an employment/affiliation tie.
    _AFFILIATION_OPTIONS = (
        "employee", "alumni", "alumnus", "associate", "contractor",
        "affiliate", "intern at", "board member", "family member",
    )

    def _check_affiliation(self, field: FormField) -> str | None:
        """Return "No" for employer/affiliation questions and options.

        Works with the (now-detectable) hidden radio/checkbox structures: a
        Yes/No radio gets "No" (the "No" option is selected), and an affiliation
        checkbox option gets "No" (left unchecked). Returns None if unrelated.
        """
        label = (field.label or "").lower()
        if not label or label == "unknown_field":
            return None
        # Don't hijack "how many employees…" (experience) or authorization
        # questions that merely contain the word "work".
        if any(x in label for x in ("how many", "number of employees",
                                    "authorized to work", "eligible to work",
                                    "right to work", "legally")):
            return None
        if any(p in label for p in self._AFFILIATION_QUESTIONS):
            return "No"
        # Checkbox options like "Company Employee" / "Company Alumni" / "Other
        # (contractor)" — never tick these.
        if field.field_type == FieldType.CHECKBOX and any(
            w in label for w in self._AFFILIATION_OPTIONS
        ):
            return "No"
        return None

    def _name_answer(self, label: str) -> str | None:
        """Resolve name fields (first/last/full/preferred) from the profile.

        Guards against non-person "name" fields (company/file/user name) so a
        long form-name label never gets the applicant's name — and so name
        fields never fall through to the LLM (which caused a job summary to be
        typed into a name box).
        """
        p = self._config.personal
        first = str(p.get("first_name", "")).strip()
        last = str(p.get("last_name", "")).strip()
        full = f"{first} {last}".strip()

        # NOT the applicant's name — either a non-person "name" (company, file)
        # or SOMEONE ELSE'S name (a referrer, emergency contact, reference).
        # These must never receive the applicant's name; returning None lets them
        # fall through to the LLM, which answers appropriately (e.g. "N/A — not
        # referred") instead of typing "Limperis" into a referral box.
        if any(x in label for x in (
            "company", "organization", "organisation", "employer", "file",
            "username", "user name", "reference", "referee", "referred",
            "referral", "referrer", "who referred", "their ", "his ", "her ",
            "manager", "supervisor", "colleague", "co-worker", "coworker",
            "school", "university", "institution", "product",
            "emergency", "next of kin", "spouse", "contact name", "parent",
            "guardian", "recruiter", "someone", "person who",
        )):
            return None

        # Combined "First and Last Name" / "Full name" / "Legal name" → full name.
        # Checked BEFORE the individual first/last branches so a combined label
        # doesn't match "last name" and return only the surname.
        if ("first" in label and "last" in label and "name" in label) or any(
            k in label for k in (
                "full name", "full legal name", "legal name", "complete name",
                "name in full", "your name", "candidate name", "applicant name",
            )
        ) or label.strip() == "name":
            return full or None

        if any(k in label for k in ("first name", "given name", "legal first", "forename")):
            return first or None
        if any(k in label for k in ("last name", "family name", "surname", "legal last")):
            return last or None
        if "middle name" in label:
            return ""  # explicit empty — don't let it reach the LLM
        if any(k in label for k in ("preferred name", "nickname", "goes by")):
            return first or None
        return None

    def _check_profile(self, field: FormField) -> str | None:
        """Check if the field can be answered from profile data."""
        label = field.label.lower()
        personal = self._config.personal

        # Phone country code FIRST — must beat the generic "country" mapping,
        # which would otherwise return the country name into a code dropdown.
        if "country code" in label or "phone code" in label or "dialing code" in label:
            return personal.get("phone_country_code", "United States (+1)")

        # Name fields — resolved explicitly so they never reach the LLM.
        name = self._name_answer(label)
        if name is not None:
            return name

        # Street address — from profile so the LLM never invents one. Guard
        # against "email address", URL/website fields, and "IP address" which
        # also contain the word "address".
        is_street = "street" in label or (
            "address" in label and not any(
                x in label for x in ("email", "e-mail", "url", "web",
                                     "linkedin", "github", "ip ")
            )
        )
        if is_street:
            street = (personal.get("street_address") or personal.get("address")
                      or personal.get("street") or "")
            if street:
                return str(street)
            # No street on file → fall through (LLM). Encourage adding
            # personal.street_address to the profile to avoid a guessed value.

        # NOTE: name fields are handled ONLY by _name_answer above — do not add
        # "first name"/"last name" here, or a non-applicant field like "provide
        # their first and last name" would naively match and get the surname.
        mappings = {
            "email": personal.get("email"),
            "phone": personal.get("phone"),
            "city": personal.get("city"),
            "state": personal.get("state"),
            "country": personal.get("country"),
            "zip": personal.get("zip"),
            "postal": personal.get("zip"),
        }

        # Identity values belong in short fields, never a free-text narrative —
        # and match on WORD BOUNDARIES so "state" != "statement", "city" !=
        # "capacity", "phone" != "smartphone".
        if field.field_type != FieldType.TEXTAREA:
            for pattern, value in mappings.items():
                if pattern == "country" and "code" in label:
                    continue
                if value and re.search(r"\b" + re.escape(pattern) + r"\b", label):
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
        # Do NOT auto-"Yes" adverse/legal questions — those must be answered
        # truthfully; leave them for the LLM (which is flagged for review).
        low = (field.label or "").lower()
        adverse_kw = (
            "convicted", "felony", "criminal", "misdemeanor", "terminated",
            "fired", "disciplin", "lawsuit", "non-compete", "noncompete",
            "debarred", "sanction", "restricted", "banned",
        )
        if any(k in low for k in adverse_kw):
            return None
        for o in options:
            if o.strip().lower() == "yes":
                log.info("solver.yes_default", label=field.label)
                return o
        return None

    def _location_preferences(self) -> list[str]:
        """Ordered list of preferred location/work-arrangement tokens.

        Built from the USER's own profile + search config (not hardcoded), so a
        non-Seattle user gets the right answers. Order: their city, their state,
        each configured search location, then remote/hybrid.
        """
        personal = self._config.personal
        prefs: list[str] = []
        for v in (personal.get("city"), personal.get("state")):
            if v:
                prefs.append(str(v).split(",")[0].strip())
        for loc in self._config.search.get("locations", []) or []:
            prefs.append(str(loc).split(",")[0].strip())
        # Configurable work-arrangement fallback (settings.yaml search.remote)
        remote_cfg = self._config.search.get("remote", []) or ["remote", "hybrid"]
        prefs.extend(["remote" if r == "remote" else r for r in remote_cfg])
        # De-dup, drop empties, lowercase
        seen, out = set(), []
        for p in prefs:
            pl = p.lower().strip()
            if pl and pl not in seen:
                seen.add(pl)
                out.append(pl)
        return out or ["remote"]

    def _check_location_preference(self, field: FormField) -> str | None:
        """Answer location / work-arrangement questions from the user's config."""
        label = field.label.lower()
        location_kw = (
            "preferred location", "work location", "which location", "office location",
            "location preference", "where would you like", "desired location",
            "work arrangement", "work model", "on-site or remote", "remote or",
        )
        if not any(k in label for k in location_kw):
            return None

        prefs = self._location_preferences()

        # Option-based: pick the first option matching the user's preference order
        if field.field_type in (FieldType.DROPDOWN, FieldType.RADIO) and field.options:
            for pref in prefs:
                for opt in field.options:
                    if pref in opt.lower():
                        return opt
            return field.options[0]

        # Free-text location field → the user's city (or first preference)
        return self._config.personal.get("city") or prefs[0].title()

    @staticmethod
    def _is_consent_checkbox(label: str) -> bool:
        """True if a checkbox label looks like a terms/consent acknowledgement."""
        low = (label or "").lower()
        consent_kw = (
            "agree", "consent", "terms", "privacy", "acknowledge", "authorize",
            "certify", "confirm", "i understand", "i have read", "gdpr",
            "processing of my", "accept",
        )
        return any(k in low for k in consent_kw)

    def _check_eeo(self, field: FormField) -> str | None:
        """Handle EEO / voluntary self-identification questions."""
        label = field.label.lower()
        eeo = self._answers.get("eeo", {})
        patterns = eeo.get("patterns", [])

        if not any(p.lower() in label for p in patterns):
            return None

        # For dropdowns/radios, pick the "decline to answer" option.
        if field.field_type in (FieldType.DROPDOWN, FieldType.RADIO) and field.options:
            opt = self._eeo_decline_option(field.options)
            if opt is not None:
                return opt
            # No decline-like option found — do NOT fall back to a real value
            # (e.g. the last option could be a specific race/gender). Return the
            # decline phrase; if it matches no option the field is left at its
            # default rather than disclosing a demographic the user withheld.
            return "Prefer not to say"

        return "Prefer not to say"

    def _eeo_decline_option(self, options: list[str]) -> str | None:
        """Return the option that declines to self-identify, or None."""
        kws = [str(k).lower() for k in
               self._answers.get("eeo", {}).get("decline_keywords", [])] + [
            "decline", "prefer not", "do not wish", "don't wish", "not to answer",
            "not to disclose", "not to say", "opt out", "choose not", "rather not",
            "no answer", "not specified", "prefer to self",
        ]
        for option in options:
            ol = (option or "").lower()
            if any(kw in ol for kw in kws):
                return option
        return None

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
            term = str(tech).lower()
            # Boundaries that tolerate symbol-bearing tech names (C++, C#, .NET,
            # Node.js) — \b would never match a term ending in + # or .
            pattern = r"(?<![\w.+#])" + re.escape(term) + r"(?![\w.+#])"
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
            # Include the two most recent roles with a couple of real impact
            # bullets each, so cover-letter narratives can cite concrete
            # achievements instead of generic claims.
            exp_lines = ["Recent experience (use these real facts; never invent):"]
            for role in experience[:2]:
                title = role.get("title", "")
                company = role.get("company", "")
                dates = f"{role.get('start_date', '')}–{role.get('end_date', '') or 'present'}"
                exp_lines.append(f"- {title} at {company} ({dates})")
                bullets = role.get("bullets", []) or []
                for b in bullets[:2]:
                    text = b.get("text", "") if isinstance(b, dict) else str(b)
                    if text:
                        exp_lines.append(f"    • {text}")
            parts.append("\n".join(exp_lines))

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
