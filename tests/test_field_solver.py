"""Tests for the hybrid field solver."""

from unittest.mock import MagicMock

import pytest
import yaml

from velvetoverride.agent.field_solver import FieldSolver
from velvetoverride.linkedin.fields import FormField
from velvetoverride.tracking.models import FieldType
from velvetoverride.utils.config import Config


@pytest.fixture
def config():
    answers = {
        "yes_no": {
            "legally_authorized": {
                "patterns": ["legally authorized", "authorized to work"],
                "answer": True,
            },
            "visa_sponsorship": {
                "patterns": ["sponsorship", "require visa"],
                "answer": False,
            },
        },
        "eeo": {
            "patterns": ["gender", "race", "veteran", "disability"],
            "decline_keywords": ["decline", "prefer not"],
        },
        "numeric": {
            "experience_patterns": ["years of experience", "how many years"],
            "salary": {
                "patterns": ["salary", "compensation"],
                "answer": "150000",
            },
        },
        "education": {
            "degree_patterns": ["highest degree"],
            "answer": "Bachelor's Degree",
            "has_degree": {
                "patterns": ["do you have a degree"],
                "answer": True,
            },
        },
        "text_defaults": {
            "linkedin_url": {"patterns": ["linkedin"]},
            "website": {"patterns": ["website", "portfolio", "github"]},
        },
        "learned": {
            "What is your preferred programming language?": {
                "answer": "Python",
                "field_type": "text",
                "added_date": "2026-01-01",
            }
        },
    }

    profile = {
        "personal": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@test.com",
            "phone": "+1 555-0100",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "website": "https://janedoe.dev",
            "city": "San Francisco",
        },
        "summary": "Software engineer.",
        "skills": {"languages": ["Python", "Go"]},
        "technology_experience": {"python": 5, "go": 3, "default": 1},
    }

    return Config(
        settings={"bot": {}, "search": {}, "browser": {}, "llm": {}, "resume": {}, "tracking": {}},
        profile=profile,
        answers=answers,
    )


def _make_field(label: str, field_type: FieldType, options: list[str] | None = None) -> FormField:
    """Create a mock FormField."""
    mock_locator = MagicMock()
    return FormField(
        label=label,
        field_type=field_type,
        locator=mock_locator,
        options=options,
    )


class TestFieldSolver:
    @pytest.fixture
    def solver(self, config):
        return FieldSolver(config, llm=None)

    @pytest.mark.asyncio
    async def test_learned_answer(self, solver):
        field = _make_field("What is your preferred programming language?", FieldType.TEXT)
        answer, source, review = await solver.solve(field)
        assert answer == "Python"
        assert source == "learned"
        assert review is False

    @pytest.mark.asyncio
    async def test_yes_no_authorized(self, solver):
        field = _make_field("Are you legally authorized to work in the US?", FieldType.RADIO)
        answer, source, review = await solver.solve(field)
        assert answer == "Yes"
        assert source == "config"

    @pytest.mark.asyncio
    async def test_yes_no_visa(self, solver):
        field = _make_field("Will you require visa sponsorship?", FieldType.RADIO)
        answer, source, review = await solver.solve(field)
        assert answer == "No"
        assert source == "config"

    @pytest.mark.asyncio
    async def test_experience_years_python(self, solver):
        field = _make_field("How many years of experience do you have with Python?", FieldType.NUMERIC)
        answer, source, _ = await solver.solve(field)
        assert answer == "5"
        assert source == "config"

    @pytest.mark.asyncio
    async def test_experience_years_unknown_tech(self, solver):
        field = _make_field("How many years of experience with Rust?", FieldType.NUMERIC)
        answer, source, _ = await solver.solve(field)
        assert answer == "1"  # default

    @pytest.mark.asyncio
    async def test_salary(self, solver):
        field = _make_field("What is your desired salary?", FieldType.NUMERIC)
        answer, source, _ = await solver.solve(field)
        assert answer == "150000"

    @pytest.mark.asyncio
    async def test_profile_email(self, solver):
        field = _make_field("Email address", FieldType.TEXT)
        answer, source, _ = await solver.solve(field)
        assert answer == "jane@test.com"
        assert source == "profile"

    @pytest.mark.asyncio
    async def test_profile_city(self, solver):
        field = _make_field("City", FieldType.TEXT)
        answer, source, _ = await solver.solve(field)
        assert answer == "San Francisco"

    @pytest.mark.asyncio
    async def test_eeo_dropdown(self, solver):
        field = _make_field(
            "Gender",
            FieldType.DROPDOWN,
            options=["Male", "Female", "Non-binary", "Prefer not to say"],
        )
        answer, source, _ = await solver.solve(field)
        assert answer == "Prefer not to say"

    @pytest.mark.asyncio
    async def test_eeo_decline(self, solver):
        field = _make_field(
            "Veteran status",
            FieldType.DROPDOWN,
            options=["Yes", "No", "I decline to answer"],
        )
        answer, source, _ = await solver.solve(field)
        assert answer == "I decline to answer"

    @pytest.mark.asyncio
    async def test_education(self, solver):
        field = _make_field("What is your highest degree?", FieldType.DROPDOWN)
        answer, source, _ = await solver.solve(field)
        assert answer == "Bachelor's Degree"

    @pytest.mark.asyncio
    async def test_linkedin_url(self, solver):
        field = _make_field("LinkedIn profile URL", FieldType.TEXT)
        answer, source, _ = await solver.solve(field)
        assert answer == "https://linkedin.com/in/janedoe"

    @pytest.mark.asyncio
    async def test_website(self, solver):
        field = _make_field("Portfolio website", FieldType.TEXT)
        answer, source, _ = await solver.solve(field)
        assert answer == "https://janedoe.dev"

    @pytest.mark.asyncio
    async def test_no_answer_without_llm(self, solver):
        field = _make_field("What is your favorite color?", FieldType.TEXT)
        answer, source, _ = await solver.solve(field)
        assert answer is None
        assert source == "skip"

    def test_learn_answer(self, solver):
        solver.learn_answer("What color is the sky?", "Blue", "text")
        learned = solver._answers["learned"]
        assert "What color is the sky?" in learned
        assert learned["What color is the sky?"]["answer"] == "Blue"


class TestNameRouting:
    """Name fields must be identified carefully: combined labels get the full
    name, and non-applicant/third-party 'name' fields never get the applicant's
    name (they fall through to the LLM for an appropriate answer)."""

    @pytest.fixture
    def solver(self, config):
        return FieldSolver(config, llm=None)

    @pytest.mark.asyncio
    async def test_first_name(self, solver):
        answer, source, _ = await solver.solve(_make_field("First Name", FieldType.TEXT))
        assert answer == "Jane"
        assert source == "profile"

    @pytest.mark.asyncio
    async def test_last_name(self, solver):
        answer, source, _ = await solver.solve(_make_field("Last Name", FieldType.TEXT))
        assert answer == "Doe"

    @pytest.mark.asyncio
    async def test_combined_first_and_last_name_gets_full_name(self, solver):
        # Regression: "First and Last Name" used to return only the surname.
        answer, source, _ = await solver.solve(_make_field("First and Last Name", FieldType.TEXT))
        assert answer == "Jane Doe"
        assert source == "profile"

    @pytest.mark.asyncio
    async def test_full_name(self, solver):
        answer, _, _ = await solver.solve(_make_field("Full Name", FieldType.TEXT))
        assert answer == "Jane Doe"

    @pytest.mark.asyncio
    async def test_bare_name_label(self, solver):
        answer, _, _ = await solver.solve(_make_field("Name", FieldType.TEXT))
        assert answer == "Jane Doe"

    @pytest.mark.asyncio
    async def test_referral_name_not_applicant(self, solver):
        # Regression: a referral field asking for someone else's name must NOT
        # receive the applicant's name. With no LLM it falls through to skip.
        field = _make_field(
            "Were you referred to Artera? If so, please provide their first and last name.",
            FieldType.TEXT,
        )
        answer, source, _ = await solver.solve(field)
        assert answer not in ("Doe", "Jane", "Jane Doe")
        assert source != "profile"

    @pytest.mark.asyncio
    async def test_company_name_not_applicant(self, solver):
        answer, source, _ = await solver.solve(_make_field("Company Name", FieldType.TEXT))
        assert answer not in ("Doe", "Jane", "Jane Doe")

    @pytest.mark.asyncio
    async def test_emergency_contact_name_not_applicant(self, solver):
        answer, source, _ = await solver.solve(_make_field("Emergency contact name", FieldType.TEXT))
        assert answer not in ("Doe", "Jane", "Jane Doe")

    @pytest.mark.asyncio
    async def test_reference_name_not_applicant(self, solver):
        answer, _, _ = await solver.solve(_make_field("Reference full name", FieldType.TEXT))
        assert answer not in ("Doe", "Jane", "Jane Doe")


class TestAddressRouting:
    """Street-address fields fill from profile (never LLM-guessed), without
    catching 'Email address' or URL fields that also contain 'address'."""

    def _solver(self, config, street="16611 48th Ave W"):
        if street is not None:
            config.profile["personal"]["street_address"] = street
        return FieldSolver(config, llm=None)

    @pytest.mark.asyncio
    async def test_street_address_from_profile(self, config):
        solver = self._solver(config)
        answer, source, _ = await solver.solve(_make_field("Street Address", FieldType.TEXT))
        assert answer == "16611 48th Ave W"
        assert source == "profile"

    @pytest.mark.asyncio
    async def test_address_line_1(self, config):
        solver = self._solver(config)
        answer, _, _ = await solver.solve(_make_field("Address Line 1", FieldType.TEXT))
        assert answer == "16611 48th Ave W"

    @pytest.mark.asyncio
    async def test_email_address_not_treated_as_street(self, config):
        solver = self._solver(config)
        answer, _, _ = await solver.solve(_make_field("Email address", FieldType.TEXT))
        assert answer == "jane@test.com"

    @pytest.mark.asyncio
    async def test_linkedin_url_not_treated_as_street(self, config):
        solver = self._solver(config)
        answer, _, _ = await solver.solve(_make_field("LinkedIn URL", FieldType.TEXT))
        assert answer == "https://linkedin.com/in/janedoe"

    @pytest.mark.asyncio
    async def test_no_street_on_file_falls_through(self, config):
        # Unset street → must not fabricate; with no LLM it falls to skip.
        config.profile["personal"].pop("street_address", None)
        solver = FieldSolver(config, llm=None)
        answer, source, _ = await solver.solve(_make_field("Street Address", FieldType.TEXT))
        assert answer is None
        assert source == "skip"


class TestSkipFields:
    """LinkedIn's 'I'm looking for…' box is left blank, not auto-answered."""

    @pytest.fixture
    def solver(self, config):
        return FieldSolver(config, llm=None)

    @pytest.mark.asyncio
    async def test_im_looking_for_is_skipped(self, solver):
        for label in ("I'm looking for…", "I am looking for", "Im looking for a role"):
            answer, source, _ = await solver.solve(_make_field(label, FieldType.TEXT))
            assert answer is None, label
            assert source == "skip", label

    @pytest.mark.asyncio
    async def test_genuine_looking_for_question_not_skipped(self, solver):
        # "What are you looking for in a role" is a real question — not skipped
        # (no LLM here, so it resolves via other tiers, but must not be 'skip').
        assert not solver._should_skip_field("what are you looking for in your next role?")

    @pytest.mark.asyncio
    async def test_config_extendable_skip(self, config):
        config.answers["skip_fields"] = ["salary expectations story"]
        solver = FieldSolver(config, llm=None)
        answer, source, _ = await solver.solve(
            _make_field("Salary expectations story", FieldType.TEXTAREA))
        assert answer is None
        assert source == "skip"


class TestCheckboxSafety:
    """An unlabeled/unidentifiable checkbox must never be auto-ticked."""

    @pytest.fixture
    def solver(self, config):
        return FieldSolver(config, llm=None)

    @pytest.mark.asyncio
    async def test_unlabeled_checkbox_not_checked(self, solver):
        for label in ("", "unknown_field", "  "):
            f = _make_field(label, FieldType.CHECKBOX)
            answer, source, _ = await solver.solve(f)
            assert answer == "No", f"label={label!r}"

    @pytest.mark.asyncio
    async def test_consent_checkbox_still_checked(self, solver):
        f = _make_field("I agree to the terms and privacy policy", FieldType.CHECKBOX)
        answer, source, _ = await solver.solve(f)
        assert answer == "Yes"

    @pytest.mark.asyncio
    async def test_unlabeled_checkbox_never_reaches_llm(self, config):
        # Even with an LLM available, an unlabeled checkbox is decided locally.
        class _BoomLLM:
            def answer_field(self, **k): raise AssertionError("LLM must not be called")
        solver = FieldSolver(config, _BoomLLM())
        f = _make_field("unknown_field", FieldType.CHECKBOX)
        answer, _, _ = await solver.solve(f)
        assert answer == "No"


class TestAffiliationRouting:
    """Employer/affiliation questions -> No; must not hijack unrelated fields."""

    @pytest.fixture
    def solver(self, config):
        return FieldSolver(config, llm=None)

    @pytest.mark.asyncio
    async def test_employee_question_answered_no(self, solver):
        for label in ("Are you a current employee of Acme?",
                      "Are you a former employee?",
                      "Do you currently work for this company?"):
            answer, source, _ = await solver.solve(_make_field(label, FieldType.RADIO,
                                                               options=["Yes", "No"]))
            assert answer == "No", label

    @pytest.mark.asyncio
    async def test_affiliation_checkbox_option_not_checked(self, solver):
        for label in ("Acme Employee", "Acme Alumni", "Company Contractor"):
            answer, _, _ = await solver.solve(_make_field(label, FieldType.CHECKBOX))
            assert answer == "No", label

    @pytest.mark.asyncio
    async def test_does_not_hijack_experience_or_authorization(self, solver):
        # "how many employees" is experience; must NOT become "No"
        assert solver._check_affiliation(_make_field("How many employees did you manage?",
                                                     FieldType.NUMERIC)) is None
        # legally authorized to work is handled elsewhere as Yes
        assert solver._check_affiliation(_make_field("Are you legally authorized to work in the US?",
                                                     FieldType.RADIO)) is None

    @pytest.mark.asyncio
    async def test_affiliation_beats_generally_yes_default(self, solver):
        # Without the rule this benign-looking yes/no would default to "Yes".
        f = _make_field("Are you affiliated with anyone at the company?",
                        FieldType.RADIO, options=["Yes", "No"])
        answer, _, _ = await solver.solve(f)
        assert answer == "No"

    @pytest.mark.asyncio
    async def test_team_member_and_ever_employed_answered_no(self, solver):
        # Regression: real form answered these "Yes" via the LLM because the
        # company name sits between "current" and "team member", and only
        # "currently employed by" was listed. All must be "No".
        for label in (
            "Are you a current Consensus team member?",
            "Have you ever been employed by Consensus Cloud Solutions or any of it's affiliates?",
            "Were you referred by a current Consensus team member?",
        ):
            answer, source, _ = await solver.solve(
                _make_field(label, FieldType.RADIO, options=["Yes", "No"]))
            assert answer == "No", label
            assert source == "config", label

    @pytest.mark.asyncio
    async def test_conditional_employment_followup_is_na_not_history(self, solver):
        # "If so, which company and dates were you employed" must NOT leak real
        # employment history — the applicant was not employed here.
        f = _make_field(
            "If so, please let us know which company and the approximate dates you were employed there.",
            FieldType.TEXTAREA)
        answer, _, _ = await solver.solve(f)
        assert answer == "N/A"

    @pytest.mark.asyncio
    async def test_benign_team_member_question_not_hijacked(self, solver):
        # A generic teamwork question must NOT be forced to "No".
        assert solver._check_affiliation(
            _make_field("Are you comfortable working as a team member?",
                        FieldType.RADIO, options=["Yes", "No"])) is None

    @pytest.mark.asyncio
    async def test_affiliation_no_into_free_text_is_avoided(self, solver):
        # An employer-tie phrase in a free-text box must not get "No" typed in.
        assert solver._check_affiliation(
            _make_field("Who is your current employer?", FieldType.TEXT)) is None


class TestReviewBugFixes:
    """Regression tests for the multi-agent bug review."""

    def _cfg(self, **personal):
        base = {"first_name": "Jordan", "last_name": "Limperis",
                "email": "j@x.com", "city": "Seattle", "state": "Washington",
                "zip": "98026", "phone": "555-0100"}
        base.update(personal)
        return Config(profile={"personal": base},
                      answers={"yes_no": {"prev_emp": {"patterns": ["worked at"], "answer": False},
                                          "relocate": {"patterns": ["relocate"], "answer": True}},
                               "eeo": {"patterns": ["gender", "race"], "decline_keywords": ["decline", "prefer not"]}})

    @pytest.mark.asyncio
    async def test_referee_field_not_applicant_name(self):
        fs = FieldSolver(self._cfg(), llm=None)
        for label in ("Referee's last name", "Referee first name"):
            ans, src, _ = await fs.solve(_make_field(label, FieldType.TEXT))
            assert ans not in ("Limperis", "Jordan"), label

    @pytest.mark.asyncio
    async def test_state_substring_not_matched_in_textarea(self):
        fs = FieldSolver(self._cfg(), llm=None)
        # "Personal statement" contains "state" but must NOT get "Washington"
        ans, src, _ = await fs.solve(_make_field("Personal statement", FieldType.TEXTAREA))
        assert ans != "Washington"
        # and "capacity" must not get the city
        ans2, _, _ = await fs.solve(_make_field("In what capacity did you work?", FieldType.TEXT))
        assert ans2 != "Seattle"

    @pytest.mark.asyncio
    async def test_state_still_fills_a_real_state_field(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, src, _ = await fs.solve(_make_field("State", FieldType.TEXT))
        assert ans == "Washington"

    @pytest.mark.asyncio
    async def test_config_yesno_not_typed_into_textarea(self):
        fs = FieldSolver(self._cfg(), llm=None)
        # "Please list companies you have worked at" matches "worked at" but is a
        # textarea — must not get "No".
        ans, src, _ = await fs.solve(_make_field("Please list companies you have worked at",
                                                 FieldType.TEXTAREA))
        assert ans not in ("No", "Yes")

    @pytest.mark.asyncio
    async def test_config_yesno_still_answers_radio(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, src, _ = await fs.solve(_make_field("Have you worked at a competitor?",
                                                 FieldType.RADIO, options=["Yes", "No"]))
        assert ans == "No"

    @pytest.mark.asyncio
    async def test_eeo_dropdown_no_decline_option_does_not_pick_real_value(self):
        fs = FieldSolver(self._cfg(), llm=None)
        # A gender dropdown with NO decline option must not select "Female" etc.
        f = _make_field("Gender", FieldType.DROPDOWN, options=["Male", "Female", "Non-binary"])
        ans, _, _ = await fs.solve(f)
        assert ans not in ("Male", "Female", "Non-binary")

    @pytest.mark.asyncio
    async def test_eeo_dropdown_picks_decline_when_present(self):
        fs = FieldSolver(self._cfg(), llm=None)
        f = _make_field("Race/Ethnicity", FieldType.DROPDOWN,
                        options=["White", "Asian", "Prefer not to answer"])
        ans, _, _ = await fs.solve(f)
        assert ans == "Prefer not to answer"

    @pytest.mark.asyncio
    async def test_learned_sentinel_does_not_poison_unlabeled(self):
        cfg = self._cfg()
        cfg.answers["learned"] = {"unknown_field": {"answer": "LEAKED", "field_type": "text"}}
        fs = FieldSolver(cfg, llm=None)
        ans, src, _ = await fs.solve(_make_field("unknown_field", FieldType.CHECKBOX))
        assert ans != "LEAKED"  # checkbox safety wins, not the learned sentinel


def _exp_config() -> Config:
    """Config with a real total-experience value, generic experience triggers,
    and real education graduation dates (matching the EE resume)."""
    answers = {
        "yes_no": {},
        "eeo": {"patterns": [], "decline_keywords": []},
        "numeric": {
            "llm_for_unknown_tech": True,
            "total_experience_years": 11,
            "experience_patterns": [
                "years of experience", "how many years", "experience with",
                "professional experience", "work experience", "total years",
                "overall experience",
            ],
            "salary": {"patterns": ["salary"], "answer": "150000"},
        },
        "education": {
            "degree_patterns": ["highest degree", "education level", "degree", "qualification"],
            "answer": "Master's Degree",
            "has_degree": {"patterns": ["do you have a degree"], "answer": True},
        },
        "text_defaults": {},
        "learned": {},
    }
    profile = {
        "personal": {"first_name": "Jane", "last_name": "Doe"},
        "technology_experience": {"python": 5, "machine learning": 4, "default": 2},
        "education": [
            {"institution": "GSU", "graduation_date": "2022-12", "field": "Computer Science"},
            {"institution": "Cornell", "graduation_date": "2015-05", "field": "Neuroscience"},
        ],
    }
    return Config(
        settings={"bot": {}, "search": {}, "browser": {}, "llm": {}, "resume": {}, "tracking": {}},
        profile=profile,
        answers=answers,
    )


def _screening_config() -> Config:
    """Config exercising security-clearance, degree-completion, and the LLM
    fallback for unmatched yes/no questions."""
    answers = {
        "yes_no": {
            "visa_sponsorship": {"patterns": ["sponsorship", "h-1b"], "answer": False},
            "security_clearance": {
                "patterns": ["security clearance", "clearance"], "answer": False},
            "drug_test": {"patterns": ["drug test"], "answer": True},
        },
        "eeo": {"patterns": [], "decline_keywords": []},
        "numeric": {"experience_patterns": [], "salary": {"patterns": [], "answer": "1"}},
        "education": {
            "degree_patterns": ["highest degree", "education level"],
            "answer": "Master's Degree",
            "has_degree": {
                "patterns": ["bachelor", "master's degree", "do you have a degree",
                             "completed the following level of education"],
                "answer": True,
            },
        },
        "text_defaults": {},
        "learned": {},
    }
    profile = {"personal": {"first_name": "J", "last_name": "L"},
               "technology_experience": {"default": 1}, "education": []}
    return Config(
        settings={"bot": {}, "search": {}, "browser": {}, "llm": {}, "resume": {}, "tracking": {}},
        profile=profile, answers=answers)


class _MockLLM:
    """Records answer_field calls; returns a fixed reply."""
    def __init__(self, reply="Yes"):
        self.reply, self.calls = reply, []

    def answer_field(self, question, **kwargs):
        self.calls.append(question)
        return self.reply


def _tz_config() -> Config:
    """Pacific-time, US-citizen profile with a visa-status rule."""
    answers = {
        "yes_no": {
            "visa_sponsorship": {"patterns": ["sponsorship", "require visa"], "answer": False},
            "on_work_visa": {
                "patterns": ["on a tn visa", "tn visa", "on an h-1b", "on opt", "on f-1"],
                "answer": False},
        },
        "eeo": {"patterns": [], "decline_keywords": []},
        "numeric": {"experience_patterns": [], "salary": {"patterns": [], "answer": "1"}},
        "education": {"degree_patterns": [], "answer": "", "has_degree": {"patterns": [], "answer": True}},
        "text_defaults": {}, "learned": {},
    }
    profile = {
        "personal": {"first_name": "J", "last_name": "L", "city": "Seattle",
                     "state": "Washington", "time_zone": "Pacific",
                     "work_authorization": "US citizen; not on any work visa"},
        "technology_experience": {"default": 1}, "education": [],
    }
    return Config(
        settings={"bot": {}, "search": {}, "browser": {}, "llm": {}, "resume": {}, "tracking": {}},
        profile=profile, answers=answers)


class TestTimezoneAndVisa:
    """Time-zone and visa-status questions answered from the profile."""

    @pytest.mark.asyncio
    async def test_other_timezone_is_no(self):
        fs = FieldSolver(_tz_config(), llm=None)
        for label in ("Are you located in the Eastern or Central time zone?",
                      "Do you work Eastern time zone hours?"):
            ans, src, _ = await fs.solve(_make_field(label, FieldType.RADIO, options=["Yes", "No"]))
            assert ans == "No", label

    @pytest.mark.asyncio
    async def test_own_timezone_is_yes(self):
        fs = FieldSolver(_tz_config(), llm=None)
        for label in ("Are you in the Pacific time zone?",
                      "Do you work in the PST/PDT time zone?"):
            ans, _, _ = await fs.solve(_make_field(label, FieldType.RADIO, options=["Yes", "No"]))
            assert ans == "Yes", label

    @pytest.mark.asyncio
    async def test_on_tn_visa_is_no(self):
        fs = FieldSolver(_tz_config(), llm=None)
        for label in ("Are you currently on a TN Visa?",
                      "Are you currently on an H-1B or OPT?"):
            ans, src, _ = await fs.solve(_make_field(label, FieldType.RADIO, options=["Yes", "No"]))
            assert ans == "No", label
            assert src == "config", label

    @pytest.mark.asyncio
    async def test_llm_context_has_timezone_and_work_auth(self):
        fs = FieldSolver(_tz_config(), llm=None)
        summary = fs._build_profile_summary()
        assert "Pacific Time Zone" in summary
        assert "not on any work visa" in summary


class TestResumeMethod:
    """The "Upload resume" vs "Tailor resume with AI" choice must pick Upload so
    the bot's own tailored PDF is used, not LinkedIn's AI re-tailoring."""

    @pytest.mark.asyncio
    async def test_picks_upload_resume(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, src, _ = await fs.solve(_make_field(
            "Upload resume Tailor resume with AI", FieldType.RADIO,
            options=["Upload resume", "Tailor resume with AI"]))
        assert ans == "Upload resume" and src == "config"

    @pytest.mark.asyncio
    async def test_normal_yesno_radio_not_hijacked(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "Are you willing to relocate?", FieldType.RADIO, options=["Yes", "No"]))
        assert ans != "Upload resume"

    @pytest.mark.asyncio
    async def test_picks_upload_when_options_not_captured(self):
        # LinkedIn's résumé widget sometimes yields no option list; the label
        # still carries both choices concatenated. Must still pick Upload, never
        # let the LLM choose "Tailor resume with AI".
        fs = FieldSolver(_tz_config(), llm=None)
        ans, src, _ = await fs.solve(_make_field(
            "Upload resume Tailor resume with AI", FieldType.RADIO, options=[]))
        assert ans == "Upload resume" and src == "config"


class TestCurrentDate:
    """A 'Today's date' / signature-date field must be answered with the real
    current date, never an LLM-hallucinated stale date."""

    def _today(self):
        from datetime import datetime
        return datetime.now().strftime("%m/%d/%Y")

    @pytest.mark.asyncio
    async def test_todays_date_uses_current_date(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, src, _ = await fs.solve(_make_field("Today's Date *", FieldType.TEXT))
        assert ans == self._today() and src == "config"

    @pytest.mark.asyncio
    async def test_bare_date_label_uses_current_date(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field("Date", FieldType.TEXT))
        assert ans == self._today()

    @pytest.mark.asyncio
    async def test_signature_date_uses_current_date(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field("Signature Date", FieldType.TEXT))
        assert ans == self._today()

    @pytest.mark.asyncio
    async def test_date_of_birth_is_not_today(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field("Date of Birth", FieldType.TEXT))
        assert ans != self._today()

    @pytest.mark.asyncio
    async def test_start_date_is_not_today(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field("Earliest start date", FieldType.TEXT))
        assert ans != self._today()

    @pytest.mark.asyncio
    async def test_graduation_date_is_not_today(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field("Graduation date", FieldType.TEXT))
        assert ans != self._today()


class TestConsentChoice:
    """Consent / acknowledgement gates must be affirmed across checkbox, radio,
    and dropdown. An unanswered "I Agree" gate blocks the whole form with "This
    field is required" (seen live), so radio/dropdown consent must resolve to the
    affirmative option and never a "Do Not Agree"/"Decline" one."""

    @pytest.mark.asyncio
    async def test_consent_checkbox_ticked(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, src, _ = await fs.solve(_make_field(
            "Please confirm you read and understand the above.", FieldType.CHECKBOX))
        assert ans == "Yes" and src == "config"

    @pytest.mark.asyncio
    async def test_consent_radio_picks_i_agree(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, src, _ = await fs.solve(_make_field(
            "Please confirm you read and understand the above.", FieldType.RADIO,
            options=["I Agree"]))
        assert ans == "I Agree" and src == "config"

    @pytest.mark.asyncio
    async def test_consent_radio_skips_disagree(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "Please confirm you read and understand the above.", FieldType.RADIO,
            options=["I Do Not Agree", "I Agree"]))
        assert ans == "I Agree"

    @pytest.mark.asyncio
    async def test_consent_dropdown_skips_placeholder_and_decline(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "Do you accept the terms and conditions?", FieldType.DROPDOWN,
            options=["Please select", "I Do Not Agree", "I Agree"]))
        assert ans == "I Agree"

    @pytest.mark.asyncio
    async def test_agreement_options_trigger_even_with_terse_label(self):
        fs = FieldSolver(_tz_config(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "Terms", FieldType.RADIO, options=["I Accept", "I Decline"]))
        assert ans == "I Accept"


class TestCitizenshipPreference:
    """Citizenship / work-authorization status questions prefer 'US Citizen'."""

    def _cfg(self):
        cfg = _tz_config()
        cfg.profile["personal"]["citizenship"] = "United States"
        return cfg

    @pytest.mark.asyncio
    async def test_citizenship_dropdown_picks_us_citizen(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, src, _ = await fs.solve(_make_field(
            "What is your citizenship status?", FieldType.DROPDOWN,
            options=["US Citizen", "Green Card", "H-1B", "Requires Sponsorship"]))
        assert ans == "US Citizen" and src == "config"

    @pytest.mark.asyncio
    async def test_are_you_a_citizen_is_yes(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "Are you a U.S. citizen?", FieldType.RADIO, options=["Yes", "No"]))
        assert ans == "Yes"

    @pytest.mark.asyncio
    async def test_work_auth_dropdown_picks_us_citizen(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "Work authorization status", FieldType.DROPDOWN,
            options=["U.S. Citizen", "Permanent Resident", "Requires Sponsorship"]))
        assert ans == "U.S. Citizen"

    @pytest.mark.asyncio
    async def test_citizenship_text_field(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, _, _ = await fs.solve(_make_field("Citizenship", FieldType.TEXT))
        assert ans == "U.S. Citizen"

    @pytest.mark.asyncio
    async def test_visa_status_without_citizen_option_picks_none(self):
        fs = FieldSolver(self._cfg(), llm=None)
        ans, _, _ = await fs.solve(_make_field(
            "What is your current visa status?", FieldType.DROPDOWN,
            options=["H-1B", "OPT", "F-1", "None", "Other"]))
        assert ans == "None"


class TestScreeningAndLLMFallback:
    """Configurable screening answers + ChatGPT fallback for unsure yes/no."""

    @pytest.mark.asyncio
    async def test_security_clearance_no_from_config(self):
        fs = FieldSolver(_screening_config(), llm=None)
        ans, src, _ = await fs.solve(
            _make_field("Do you currently hold a security clearance?", FieldType.RADIO,
                        options=["Yes", "No"]))
        assert ans == "No" and src == "config"

    @pytest.mark.asyncio
    async def test_degree_completion_is_yes_not_degree_name(self):
        # "Have you completed a Bachelor's Degree?" is Yes/No, not "Master's Degree".
        fs = FieldSolver(_screening_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("Have you completed the following level of education: Bachelor's Degree?",
                        FieldType.RADIO, options=["Yes", "No"]))
        assert ans == "Yes"

    @pytest.mark.asyncio
    async def test_highest_degree_still_returns_name(self):
        fs = FieldSolver(_screening_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("What is your highest degree?", FieldType.DROPDOWN))
        assert ans == "Master's Degree"

    @pytest.mark.asyncio
    async def test_unmatched_yesno_defers_to_llm_when_available(self):
        mock = _MockLLM(reply="Yes")
        fs = FieldSolver(_screening_config(), llm=mock)
        _, src, _ = await fs.solve(
            _make_field("Do you enjoy pair programming?", FieldType.RADIO,
                        options=["Yes", "No"]))
        assert src == "llm"          # went to ChatGPT, not the blind default
        assert mock.calls            # the LLM was actually consulted

    @pytest.mark.asyncio
    async def test_unmatched_yesno_uses_default_without_llm(self):
        fs = FieldSolver(_screening_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("Do you enjoy pair programming?", FieldType.RADIO,
                        options=["Yes", "No"]))
        assert ans == "Yes"          # heuristic default still works with no LLM

    @pytest.mark.asyncio
    async def test_security_clearance_no_even_with_llm(self):
        mock = _MockLLM(reply="Yes")  # the LLM would say Yes; config must win
        fs = FieldSolver(_screening_config(), llm=mock)
        ans, src, _ = await fs.solve(
            _make_field("Do you have an active security clearance?", FieldType.RADIO,
                        options=["Yes", "No"]))
        assert ans == "No" and src == "config"
        assert not mock.calls        # config short-circuited before the LLM


class TestExperienceAndGraduationDates:
    """Form date/years answers derive from the REAL career timeline (EE resume):
    a generic 'total years of experience' -> 11, graduation year -> real year,
    while per-technology and free-text answers stay untouched."""

    @pytest.mark.asyncio
    async def test_generic_total_experience_returns_real_career_length(self):
        fs = FieldSolver(_exp_config(), llm=None)
        for label in (
            "How many years of professional experience do you have?",
            "Total years of work experience",
            "Overall years of experience in the industry",
        ):
            ans, src, _ = await fs.solve(_make_field(label, FieldType.NUMERIC))
            assert ans == "11", label
            assert src == "config", label

    @pytest.mark.asyncio
    async def test_specific_technology_still_wins_over_total(self):
        fs = FieldSolver(_exp_config(), llm=None)
        # A tech-scoped question resolves to that tech's years, not the total.
        ans, _, _ = await fs.solve(
            _make_field("Years of professional experience with Python", FieldType.NUMERIC))
        assert ans == "5"

    @pytest.mark.asyncio
    async def test_total_experience_never_typed_into_textarea(self):
        fs = FieldSolver(_exp_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("Please describe your work experience", FieldType.TEXTAREA))
        assert ans != "11"

    @pytest.mark.asyncio
    async def test_unknown_specific_tech_not_answered_as_total(self):
        fs = FieldSolver(_exp_config(), llm=None)
        # Unknown tech must NOT get the total (11); it gets the small honest
        # default (2) — accuracy over inflation.
        for tech in ("Kubernetes", "Rust", "Terraform", "COBOL"):
            ans, src, _ = await fs.solve(
                _make_field(f"How many years of experience with {tech}?", FieldType.NUMERIC))
            assert ans == "2", tech          # small default, not 11
            assert src == "config", tech

    @pytest.mark.asyncio
    async def test_unknown_tech_uses_small_default_even_with_llm(self):
        # The LLM once echoed the total career length (11) for a barely-used tool.
        # A numeric years-with-<unknown tech> question must resolve to the small
        # deterministic default BEFORE the LLM is ever consulted.
        mock = _MockLLM(reply="11")
        fs = FieldSolver(_exp_config(), llm=mock)
        ans, src, _ = await fs.solve(
            _make_field("How many years of experience with Kubernetes?", FieldType.NUMERIC))
        assert ans == "2" and src == "config"
        assert not mock.calls                # never reached the (inflating) LLM

    @pytest.mark.asyncio
    async def test_general_software_engineering_years_still_total(self):
        fs = FieldSolver(_exp_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("How many years of software engineering experience?", FieldType.NUMERIC))
        assert ans == "11"                   # general career question keeps the total

    @pytest.mark.asyncio
    async def test_graduation_year_from_real_education_dates(self):
        fs = FieldSolver(_exp_config(), llm=None)
        for label in (
            "What year did you graduate?",
            "Graduation date",
            "In what year did you earn your degree?",  # the latent 'degree' bug
        ):
            ans, _, _ = await fs.solve(_make_field(label, FieldType.NUMERIC))
            assert ans == "2022", label

    @pytest.mark.asyncio
    async def test_plain_degree_question_still_returns_degree_name(self):
        fs = FieldSolver(_exp_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("What is your highest degree?", FieldType.DROPDOWN))
        assert ans == "Master's Degree"

    @pytest.mark.asyncio
    async def test_field_of_study_returns_the_subject_not_the_degree(self):
        # Regression: a "field of study" / "major" question used to return
        # "Master's Degree", which matches no option and stalls a required
        # dropdown. It must return the actual field.
        fs = FieldSolver(_exp_config(), llm=None)
        for label, ft in (
            ("What is your field of study?", FieldType.DROPDOWN),
            ("Major", FieldType.TEXT),
            ("Area of study", FieldType.DROPDOWN),
        ):
            ans, _, _ = await fs.solve(_make_field(label, ft))
            assert ans == "Computer Science", label

    @pytest.mark.asyncio
    async def test_field_of_study_not_triggered_for_essay(self):
        # A free-text essay mentioning "major" must not get the field-of-study
        # subject typed into it.
        fs = FieldSolver(_exp_config(), llm=None)
        ans, _, _ = await fs.solve(
            _make_field("Describe a major challenge you overcame", FieldType.TEXTAREA))
        assert ans != "Computer Science"
