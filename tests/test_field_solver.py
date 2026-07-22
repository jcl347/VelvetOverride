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
