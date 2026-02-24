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
