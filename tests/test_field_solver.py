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
