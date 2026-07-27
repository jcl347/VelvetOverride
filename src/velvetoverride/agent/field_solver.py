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

        # ── Tier 3c: Citizenship / work-authorization status → prefer US citizen ──
        citizenship = self._check_citizenship(field)
        if citizenship is not None:
            log.info("solver.citizenship", label=field.label[:60], answer=citizenship)
            return citizenship, "config", False

        # ── Tier 3d: Résumé-method choice → always "Upload resume" ──
        # LinkedIn offers "Upload resume" vs "Tailor resume with AI"; the AI
        # option discards our own tailored PDF, so always choose Upload.
        resume_method = self._check_resume_method(field)
        if resume_method is not None:
            log.info("solver.resume_method", chose=resume_method)
            return resume_method, "config", False

        # ── Tier 4: Profile data ──
        profile_answer = self._check_profile(field)
        if profile_answer is not None:
            return profile_answer, "profile", False

        # ── Tier 4b: Location / work-arrangement preference (Seattle or Remote) ──
        location_answer = self._check_location_preference(field)
        if location_answer is not None:
            return location_answer, "profile", False

        # ── Tier 4c: Time-zone questions answered from the profile's time_zone ──
        tz_answer = self._check_timezone(field)
        if tz_answer is not None:
            log.info("solver.timezone", label=field.label[:60], answer=tz_answer)
            return tz_answer, "profile", False

        # ── Tier 4d: "Today's date" / signature date → the real current date ──
        # Deterministic so the LLM never hallucinates a stale date (it once
        # answered "06/17/2024" on a form dated years later).
        today_answer = self._check_current_date(field)
        if today_answer is not None:
            log.info("solver.current_date", label=field.label[:60], answer=today_answer)
            return today_answer, "config", False

        # ── Tier 5: EEO handling ──
        eeo_answer = self._check_eeo(field)
        if eeo_answer is not None:
            return eeo_answer, "config", False

        # ── Tier 5a: Consent / acknowledgement / "I agree" → affirm ──
        # A consent CHECKBOX is ticked; a consent RADIO/DROPDOWN ("Please confirm
        # you read and understand the above" → "I Agree") picks the affirmative
        # option (never a "Do Not Agree" one). These gates block form submission
        # otherwise ("This field is required"). Unrelated checkboxes (marketing,
        # "I am a veteran") are NOT auto-ticked — they fall through below.
        consent = self._check_consent(field)
        if consent is not None:
            log.info("solver.consent", label=field.label[:60], answer=consent)
            return consent, "config", False

        # ── Tier 5a-bis: Safety — never blindly TICK a checkbox we can't read ──
        # An unlabeled / unidentifiable checkbox must stay UNCHECKED. Left to the
        # LLM it answers "Yes" for an empty prompt and opts the user into
        # something unknown (marketing, an affiliation, a false certification).
        if field.field_type == FieldType.CHECKBOX:
            lbl = (label or "").strip()
            if not lbl or lbl in ("unknown_field",) or len(lbl) < 3:
                log.info("solver.checkbox_unlabeled_unchecked", label=field.label[:60])
                return "No", "config", True

        # ── Tier 5b: yes/no fallback ──
        # With an LLM configured, DEFER unmatched yes/no questions to ChatGPT
        # (Tier 6) so we CHECK the specific question instead of blindly guessing
        # "Yes" (which once claimed a security clearance the applicant lacked).
        # The deterministic negatives (sponsorship, security clearance,
        # affiliation, non-compete) already matched at the config tiers above and
        # never reach here. Only when NO LLM is configured do we use the
        # conservative "generally yes" heuristic so the form still completes.
        if self._llm is None:
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
            total = numeric.get("total_experience_years")
            numeric_text = field.field_type in (FieldType.NUMERIC, FieldType.TEXT)
            # STRONG general / field / career-length markers -> the real total,
            # even if the question also names a technology. "Professional
            # experience in AI/ML or Software Engineering" is a seniority question
            # (~11 yrs), NOT "years with ML" (4) — so these win BEFORE the per-tech
            # resolver, stopping an incidental "ml"/"ai" token from capping it.
            # ~11 yrs since Aug 2015 (matches the EE resume timeline). Numeric /
            # short-text ONLY — never a textarea like "Describe your experience".
            strong_general = any(m in label for m in (
                "total years", "overall experience", "total experience",
                "overall years", "industry experience", "software engineering",
                "software development", "software engineer", "programming",
                "coding experience", "development experience", "in the industry",
                "as a software", "as an engineer", "as a developer",
                "years of experience in software",
            ))
            if total is not None and strong_general and numeric_text:
                return str(total)
            # A SPECIFIC technology in the profile -> that tech's real years.
            # ("professional experience WITH Python" -> 5, not the total.)
            years = self._resolve_experience_years(label)
            if years is not None:
                return years
            # WEAK general markers (bare "professional / work experience") with no
            # specific tech in the question -> the real career total.
            weak_general = any(m in label for m in (
                "professional experience", "work experience", "years of professional",
            ))
            if total is not None and weak_general and numeric_text:
                return str(total)
            # Unknown SPECIFIC technology (e.g. Kubernetes, not in the profile):
            # answer a small, honest default rather than deferring to the LLM,
            # which tends to echo the TOTAL career length (e.g. "11 years") for a
            # tool the applicant barely uses. Accuracy over inflation — when
            # unsure, a smaller number is the safer, truthful choice.
            if numeric_text:
                return str(self._config.technology_experience.get("default", 1))
            if numeric.get("llm_for_unknown_tech", True) and self._llm:
                return None  # → Tier 6 LLM fallback (non-numeric only)
            return str(self._config.technology_experience.get("default", 1))

        # Salary
        salary = numeric.get("salary", {})
        if any(p.lower() in label for p in salary.get("patterns", [])):
            return salary.get("answer", "0")

        # Education
        education = self._answers.get("education", {})
        # Field of study / major -> the actual subject from the profile, NEVER
        # the degree LEVEL. Must precede the degree matching below so a "field of
        # study" or "major" question returns "Computer Science", not "Master's
        # Degree" (which matches no option and stalls a required dropdown).
        if field.field_type in (FieldType.TEXT, FieldType.DROPDOWN) and any(
            k in label for k in (
                "field of study", "field of degree", "area of study",
                "course of study", "study field", "major", "discipline",
                "concentration", "what did you study",
            )
        ):
            fld = self._most_relevant_field()
            if fld:
                return fld
        # A graduation-YEAR / date question must be answered from the real
        # education dates (which match the EE resume) BEFORE the generic "degree"
        # match below — otherwise a label containing "degree" (e.g. "year you
        # earned your degree") wrongly returns the degree NAME into a date field.
        # Gated to numeric/short-text fields AND a date word, so a plain
        # "highest degree" dropdown/text still returns the degree name below.
        if (
            field.field_type in (FieldType.NUMERIC, FieldType.TEXT)
            and any(w in label for w in ("year", "date", "when"))
            and any(g in label for g in ("graduat", "degree", "completion"))
        ):
            grad = self._most_recent_grad_year()
            if grad:
                return grad
        # "Have you completed a Bachelor's Degree?" is a Yes/No question — answer
        # Yes/No, NOT the degree name. Checked BEFORE degree_patterns (whose bare
        # "degree" would otherwise return "Master's Degree" into a Yes/No radio),
        # and gated to choice fields so a name isn't typed into free text.
        has_degree = education.get("has_degree", {})
        if field.field_type in (FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX) \
                and any(p.lower() in label for p in has_degree.get("patterns", [])):
            return "Yes" if has_degree.get("answer", True) else "No"

        # "What is your highest degree?" / "Education level" — the degree NAME.
        if any(p.lower() in label for p in education.get("degree_patterns", [])):
            return education.get("answer", "Bachelor's Degree")

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
        "referred by a team member", "referred by a staff", "connected to anyone",
        "ever been employed by", "ever employed by", "ever worked for",
        "ever worked at", "previously worked for", "previously worked at",
        "previously been employed", "current team member", "a team member of",
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

        ft = field.field_type

        # Conditional follow-up like "If so, which company and the dates you were
        # employed there" — the applicant is NOT employed by / affiliated with the
        # hiring company, so answer N/A instead of leaking real employment history
        # (the LLM would otherwise list real employers and dates here).
        if ft in (FieldType.TEXT, FieldType.TEXTAREA) and (
            "if so" in label or "if yes" in label or "if applicable" in label
        ) and any(w in label for w in (
            "employ", "affiliat", "which company", "relationship", "related",
            "team member",
        )):
            return "N/A"

        choice = (FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX)
        if ft in choice:
            if any(p in label for p in self._AFFILIATION_QUESTIONS):
                return "No"
            # "current/former [Company] team member" and "referred by a current
            # [Company] team member": the company name sits between the words, so
            # a single contiguous pattern can't match — require "team member"
            # (or "staff member") plus an affiliation-context word.
            if any(t in label for t in ("team member", "staff member")) and any(
                c in label for c in ("current", "former", "referred", "employee", "staff")
            ):
                return "No"

        # Checkbox options like "Company Employee" / "Company Alumni" / "Other
        # (contractor)" — never tick these.
        if ft == FieldType.CHECKBOX and any(
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

    # Time-zone aliases so "PST/PDT/PT" all match "pacific", etc.
    _TZ_ALIASES = {
        "pacific": ("pacific", "pst", "pdt", "pacific time", "us/pacific", "west coast"),
        "mountain": ("mountain", "mst", "mdt", "mountain time"),
        "central": ("central", "cst", "cdt", "central time"),
        "eastern": ("eastern", "est", "edt", "eastern time", "east coast"),
    }

    def _check_timezone(self, field: FormField) -> str | None:
        """Answer a time-zone yes/no question from the profile's `time_zone`.

        "Are you in the Eastern or Central time zone?" -> Yes only if the user's
        OWN zone is named, otherwise No. Choice fields only.
        """
        if field.field_type not in (FieldType.RADIO, FieldType.DROPDOWN):
            return None
        label = (field.label or "").lower()
        if not any(k in label for k in ("time zone", "timezone", "time-zone")):
            return None
        tz = str(self._config.personal.get("time_zone", "")).strip().lower()
        if not tz:
            return None
        aliases = self._TZ_ALIASES.get(tz, (tz,))
        return "Yes" if any(a in label for a in aliases) else "No"

    # Option phrasings that indicate US citizenship in a status field.
    _US_CITIZEN_OPT_KW = (
        "u.s. citizen", "us citizen", "u.s citizen", "united states citizen",
        "american citizen", "citizen of the united states", "u.s. citizenship",
    )

    def _is_us_citizen(self) -> bool:
        p = self._config.personal
        cz = str(p.get("citizenship", "")).lower()
        wa = str(p.get("work_authorization", "")).lower()
        return (any(k in cz for k in ("united states", "u.s", "usa", "american"))
                or "us citizen" in wa or "u.s. citizen" in wa)

    def _check_resume_method(self, field: FormField) -> str | None:
        """Résumé-method choice ("Upload resume" vs "Tailor resume with AI") →
        pick the Upload option so OUR tailored PDF is used, not LinkedIn's AI
        re-tailoring (which discards it). Returns the upload option or None.
        """
        if field.field_type not in (FieldType.RADIO, FieldType.DROPDOWN):
            return None
        opts = field.options or []
        upload_opt = next(
            (o for o in opts if o and "upload" in o.lower() and "resume" in o.lower()),
            None,
        )
        # Only act when this really is the method choice (an "AI tailor" sibling
        # option, or a small option set), so we don't hijack unrelated radios.
        has_ai_sibling = any(
            o and ("tailor" in o.lower() or "with ai" in o.lower()) for o in opts
        )
        if upload_opt and (has_ai_sibling or len(opts) <= 3):
            return upload_opt
        # Fallback: option list wasn't captured, but the LABEL itself carries both
        # choices concatenated ("Upload resume Tailor resume with AI"). Still pick
        # Upload so we never let LinkedIn re-tailor over our own PDF.
        label = (field.label or "").lower()
        if "upload resume" in label and ("tailor" in label or "with ai" in label):
            return "Upload resume"
        return None

    def _check_citizenship(self, field: FormField) -> str | None:
        """Citizenship / work-authorization / visa STATUS questions -> prefer
        "US Citizen". Only fires when the profile says the applicant is a US
        citizen. Choice fields pick the US-citizen option (or, failing that, a
        "does not require sponsorship / not applicable" option, never a visa
        type); a bare "are you a citizen?" yes/no -> Yes; a citizenship text
        field -> "U.S. Citizen".
        """
        if not self._is_us_citizen():
            return None
        label = (field.label or "").lower()
        status_kw = (
            "citizenship", "citizen", "work authorization", "work authorisation",
            "employment authorization", "authorization status", "immigration status",
            "visa status", "residency status", "legal status", "work eligibility",
        )
        if not any(k in label for k in status_kw):
            return None
        ft = field.field_type
        if ft in (FieldType.RADIO, FieldType.DROPDOWN):
            opts = field.options or []
            # 1) An explicit "US Citizen" option wins.
            for o in opts:
                if any(k in (o or "").lower() for k in self._US_CITIZEN_OPT_KW):
                    return o
            # 2) A bare "are you a citizen?" yes/no -> Yes.
            yesno = {op.strip().lower() for op in opts if op}
            if yesno and yesno <= {"yes", "no", "maybe", ""} and "citizen" in label:
                return "Yes"
            # 3) Visa-status dropdown with no citizen option -> the "authorized /
            #    no sponsorship / not applicable" option, never a visa type.
            for o in opts:
                ol = (o or "").lower()
                if any(k in ol for k in (
                    "does not require", "do not require sponsorship", "no sponsorship",
                    "not require sponsorship", "authorized to work", "no visa required",
                    "not applicable", "n/a", "none",
                )):
                    return o
            return None
        if ft == FieldType.TEXT and "citizen" in label:
            return "U.S. Citizen"
        return None

    # Date questions that mean "the date you are filling this out" (i.e. today),
    # as opposed to a birth/start/graduation/availability date, which must NOT
    # be answered with today's date.
    _TODAY_KW = (
        "today's date", "todays date", "today date", "current date",
        "date signed", "signature date", "date of signature", "date of signing",
        "date completed", "date of completion", "date of application",
        "application date", "date of submission", "date submitted",
        "date of this application", "date certified", "certification date",
        "date acknowledged",
    )
    _NOT_TODAY_KW = (
        "birth", "start", "available", "availab", "graduat", "notice",
        "expir", "hire", "end date", "when did", "when will", "of last",
        "of birth", "from", "employment date", "termination", "resign",
        "issue date", "issued", "expected", "anticipated", "onboard",
    )

    def _check_current_date(self, field: FormField) -> str | None:
        """A "today's date" / signature-date field → the real current date
        (MM/DD/YYYY). Deterministic so the LLM never fills a stale/wrong date."""
        if field.field_type not in (FieldType.TEXT, FieldType.NUMERIC):
            return None
        label = (field.label or "").lower().strip()
        if not label:
            return None
        # Never treat a birth/start/graduation/availability date as "today".
        if any(k in label for k in self._NOT_TODAY_KW):
            return None
        stripped = label.replace("*", "").strip()
        is_today = (
            any(k in label for k in self._TODAY_KW)
            # A bare "date" label on a form (next to a name/signature) means today.
            or stripped in ("date", "date:", "date of today", "today")
        )
        if not is_today:
            return None
        return datetime.now().strftime("%m/%d/%Y")

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

    # First-person affirmative markers that identify a consent/acknowledgement
    # CHOICE (radio/dropdown), and the negatives we must never select.
    _CONSENT_AFFIRM = (
        "i agree", "i accept", "i consent", "i acknowledge", "i confirm",
        "i understand", "i have read", "i certify", "yes, i",
    )
    _CONSENT_NEG = (
        "do not", "don't", "dont", "disagree", "decline", "not agree",
        "i do not", "i don't", "opt out", "opt-out",
    )

    def _check_consent(self, field: FormField) -> str | None:
        """Answer a consent / acknowledgement / "I agree" gate.

        CHECKBOX → "Yes" (tick). RADIO/DROPDOWN → the affirmative option
        ("I Agree"/"I Accept"), never a "Do Not Agree"/"Decline" option. Fires
        when the LABEL reads like a consent prompt, or (for choice fields) when
        the OPTION set is a first-person agreement ("I Agree"/"I Do Not Agree").
        """
        ft = field.field_type
        label = (field.label or "").lower()
        opts = field.options or []
        opts_l = [(o or "").lower() for o in opts]
        is_consent_q = self._is_consent_checkbox(label)
        options_are_consent = ft in (FieldType.RADIO, FieldType.DROPDOWN) and any(
            any(m in ol for m in self._CONSENT_AFFIRM) for ol in opts_l
        )
        if not (is_consent_q or options_are_consent):
            return None
        if ft == FieldType.CHECKBOX:
            return "Yes"
        if ft in (FieldType.RADIO, FieldType.DROPDOWN):
            # Pick the affirmative option, skipping any negation/decline option.
            for o in opts:
                ol = (o or "").lower()
                if any(n in ol for n in self._CONSENT_NEG):
                    continue
                if any(m in ol for m in self._CONSENT_AFFIRM) or \
                        ol.strip() in ("agree", "accept", "yes"):
                    return o
            return None
        return None

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

    def _most_recent_grad_year(self) -> str | None:
        """Most recent education graduation YEAR from the profile.

        The profile education dates match Jordan_Limperis_EE.pdf, so a
        "what year did you graduate?" form field is answered from the real
        record. Returns the latest 4-digit year (e.g. "2022"), or None.
        """
        years: list[str] = []
        for edu in self._config.profile.get("education", []) or []:
            m = re.match(r"\s*(\d{4})", str(edu.get("graduation_date", "")))
            if m:
                years.append(m.group(1))
        return max(years) if years else None

    def _most_relevant_field(self) -> str | None:
        """The field of study / major from the profile's first (most recent)
        education entry — e.g. 'Computer Science'. Answers 'field of study' /
        'major' form fields with the real subject, never the degree level."""
        for edu in self._config.profile.get("education", []) or []:
            f = str(edu.get("field", "")).strip()
            if f:
                return f
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

        loc = personal.get("city", "")
        if personal.get("state"):
            loc = f"{loc}, {personal.get('state')}".strip(", ")
        if personal.get("time_zone"):
            loc = f"{loc} ({personal.get('time_zone')} Time Zone)"
        parts = [
            f"Name: {personal.get('first_name', '')} {personal.get('last_name', '')}",
            f"Location: {loc}",
        ]
        if personal.get("work_authorization"):
            parts.append(f"Work authorization: {personal.get('work_authorization')}")
        elif personal.get("citizenship"):
            parts.append(f"Citizenship: {personal.get('citizenship')}")

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
