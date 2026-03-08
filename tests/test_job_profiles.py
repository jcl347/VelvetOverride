"""Tests for job profiles feature."""

from pathlib import Path

import pytest
import yaml

from velvetoverride.utils.config import (
    Config,
    _apply_job_profile,
    _deep_merge,
    list_job_profiles,
    load_config,
    load_job_profile,
)


# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary config directory with base configs and job profiles."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    settings = {
        "bot": {"dry_run": True, "max_applications": 25},
        "search": {
            "keywords": ["Software Engineer"],
            "locations": ["United States"],
            "easy_apply_only": True,
            "experience_levels": ["entry_level", "associate"],
            "date_posted": "past_week",
            "min_match_score": 40,
        },
        "salary": {"min_annual": None, "max_annual": None},
        "browser": {"channel": "chrome", "headless": True},
        "llm": {"field_model": "claude-sonnet-4-6"},
        "resume": {"template": "default"},
        "tracking": {"database_path": str(tmp_path / "test.db")},
    }
    with open(config_dir / "settings.yaml", "w") as f:
        yaml.dump(settings, f)

    profile = {
        "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@test.com"},
        "summary": "A generic software engineer summary.",
        "experience": [
            {
                "company": "Acme",
                "title": "Engineer",
                "bullets": [{"text": "Did stuff", "skills": ["python"]}],
            }
        ],
        "skills": {"languages": ["Python", "Go"], "frameworks": ["React"]},
        "technology_experience": {"python": 5, "go": 3, "default": 1},
    }
    with open(config_dir / "profile.yaml", "w") as f:
        yaml.dump(profile, f)

    answers = {
        "yes_no": {
            "legally_authorized": {"patterns": ["legally authorized"], "answer": True}
        },
        "eeo": {"patterns": ["gender"], "decline_keywords": ["decline"]},
        "numeric": {
            "experience_patterns": ["years of experience"],
            "salary": {"patterns": ["salary"], "answer": "150000"},
        },
        "learned": {},
    }
    with open(config_dir / "answers.yaml", "w") as f:
        yaml.dump(answers, f)

    # Create job profiles
    profiles_dir = config_dir / "job_profiles"
    profiles_dir.mkdir()

    backend_profile = {
        "name": "Backend Engineer",
        "description": "Backend roles (Python, Go, APIs)",
        "search": {
            "keywords": ["Backend Engineer", "Platform Engineer"],
            "experience_levels": ["mid_senior"],
        },
        "salary": {"min_annual": 140000},
        "summary": "Backend engineer specializing in distributed systems.",
        "skill_emphasis": ["python", "go", "distributed systems", "kafka"],
        "answers": {
            "numeric": {"salary": {"answer": "160000"}},
        },
    }
    with open(profiles_dir / "backend_engineer.yaml", "w") as f:
        yaml.dump(backend_profile, f)

    frontend_profile = {
        "name": "Frontend Engineer",
        "description": "Frontend roles (React, TypeScript)",
        "search": {
            "keywords": ["Frontend Engineer", "UI Engineer"],
        },
        "summary": "Frontend engineer building modern web apps.",
        "skill_emphasis": ["react", "typescript", "javascript"],
    }
    with open(profiles_dir / "frontend_engineer.yaml", "w") as f:
        yaml.dump(frontend_profile, f)

    return config_dir


# ─── _deep_merge tests ──────────────────────────────────────────


def test_deep_merge_basic():
    base = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": 3, "c": 4}
    # Original should be untouched
    assert base == {"a": 1, "b": 2}


def test_deep_merge_nested():
    base = {"search": {"keywords": ["Engineer"], "locations": ["US"]}}
    override = {"search": {"keywords": ["Backend"]}}
    result = _deep_merge(base, override)
    assert result["search"]["keywords"] == ["Backend"]
    assert result["search"]["locations"] == ["US"]


def test_deep_merge_new_nested_key():
    base = {"a": {"x": 1}}
    override = {"a": {"y": 2}}
    result = _deep_merge(base, override)
    assert result == {"a": {"x": 1, "y": 2}}


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"x": [1, 2]}}
    override = {"a": {"x": [3]}}
    result = _deep_merge(base, override)
    assert result["a"]["x"] == [3]
    assert base["a"]["x"] == [1, 2]


# ─── list_job_profiles tests ────────────────────────────────────


def test_list_profiles(tmp_config_dir):
    profiles = list_job_profiles(tmp_config_dir)
    assert len(profiles) == 2
    slugs = [p["slug"] for p in profiles]
    assert "backend_engineer" in slugs
    assert "frontend_engineer" in slugs


def test_list_profiles_includes_metadata(tmp_config_dir):
    profiles = list_job_profiles(tmp_config_dir)
    backend = next(p for p in profiles if p["slug"] == "backend_engineer")
    assert backend["name"] == "Backend Engineer"
    assert "Backend roles" in backend["description"]
    assert backend["path"].endswith("backend_engineer.yaml")


def test_list_profiles_empty_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("bot: {}")
    profiles = list_job_profiles(config_dir)
    assert profiles == []


def test_list_profiles_no_profiles_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("bot: {}")
    # No job_profiles directory at all
    profiles = list_job_profiles(config_dir)
    assert profiles == []


# ─── load_job_profile tests ─────────────────────────────────────


def test_load_job_profile(tmp_config_dir):
    data = load_job_profile(tmp_config_dir, "backend_engineer")
    assert data["name"] == "Backend Engineer"
    assert data["search"]["keywords"] == ["Backend Engineer", "Platform Engineer"]


def test_load_job_profile_not_found(tmp_config_dir):
    with pytest.raises(FileNotFoundError, match="data_scientist"):
        load_job_profile(tmp_config_dir, "data_scientist")


def test_load_job_profile_error_lists_available(tmp_config_dir):
    with pytest.raises(FileNotFoundError, match="backend_engineer"):
        load_job_profile(tmp_config_dir, "nonexistent")


# ─── _apply_job_profile tests ───────────────────────────────────


def test_apply_profile_overrides_search():
    settings = {"search": {"keywords": ["Engineer"], "locations": ["US"]}}
    profile = {"summary": "Original"}
    answers = {}
    jp = {"search": {"keywords": ["Backend"], "experience_levels": ["mid_senior"]}}

    settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)
    assert settings["search"]["keywords"] == ["Backend"]
    assert settings["search"]["locations"] == ["US"]
    assert settings["search"]["experience_levels"] == ["mid_senior"]


def test_apply_profile_overrides_salary():
    settings = {"salary": {"min_annual": None, "max_annual": None}}
    profile = {}
    answers = {}
    jp = {"salary": {"min_annual": 140000}}

    settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)
    assert settings["salary"]["min_annual"] == 140000
    assert settings["salary"]["max_annual"] is None


def test_apply_profile_overrides_summary():
    settings = {}
    profile = {"summary": "Original summary"}
    answers = {}
    jp = {"summary": "Tailored backend summary"}

    settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)
    assert profile["summary"] == "Tailored backend summary"


def test_apply_profile_sets_skill_emphasis():
    settings = {}
    profile = {}
    answers = {}
    jp = {"skill_emphasis": ["python", "go"]}

    settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)
    assert profile["skill_emphasis"] == ["python", "go"]


def test_apply_profile_overrides_answers():
    settings = {}
    profile = {}
    answers = {"numeric": {"salary": {"answer": "150000"}}}
    jp = {"answers": {"numeric": {"salary": {"answer": "160000"}}}}

    settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)
    assert answers["numeric"]["salary"]["answer"] == "160000"


def test_apply_profile_preserves_unrelated_answers():
    settings = {}
    profile = {}
    answers = {
        "yes_no": {"legally_authorized": {"answer": True}},
        "numeric": {"salary": {"answer": "150000"}},
    }
    jp = {"answers": {"numeric": {"salary": {"answer": "160000"}}}}

    settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)
    # Salary overridden
    assert answers["numeric"]["salary"]["answer"] == "160000"
    # yes_no untouched
    assert answers["yes_no"]["legally_authorized"]["answer"] is True


# ─── load_config with job_profile tests ─────────────────────────


def test_load_config_no_profile(tmp_config_dir):
    config = load_config(tmp_config_dir)
    assert config.active_job_profile is None
    assert config.search["keywords"] == ["Software Engineer"]
    assert config.skill_emphasis == []


def test_load_config_with_backend_profile(tmp_config_dir):
    config = load_config(tmp_config_dir, job_profile="backend_engineer")
    assert config.active_job_profile == "backend_engineer"
    # Search keywords overridden
    assert config.search["keywords"] == ["Backend Engineer", "Platform Engineer"]
    # Locations preserved from base
    assert config.search["locations"] == ["United States"]
    # Experience levels overridden
    assert config.search["experience_levels"] == ["mid_senior"]
    # Summary overridden
    assert "distributed systems" in config.profile["summary"].lower()
    # Skill emphasis set
    assert "python" in config.skill_emphasis
    assert "kafka" in config.skill_emphasis
    # Salary override applied
    assert config.settings["salary"]["min_annual"] == 140000
    # Answer override applied
    assert config.answers["numeric"]["salary"]["answer"] == "160000"


def test_load_config_with_frontend_profile(tmp_config_dir):
    config = load_config(tmp_config_dir, job_profile="frontend_engineer")
    assert config.active_job_profile == "frontend_engineer"
    assert config.search["keywords"] == ["Frontend Engineer", "UI Engineer"]
    assert "web apps" in config.profile["summary"].lower()
    assert "react" in config.skill_emphasis


def test_load_config_invalid_profile(tmp_config_dir):
    with pytest.raises(FileNotFoundError, match="data_scientist"):
        load_config(tmp_config_dir, job_profile="data_scientist")


def test_load_config_base_unchanged_after_profile(tmp_config_dir):
    """Loading with a profile should not mutate base files on disk."""
    # Load with profile
    config1 = load_config(tmp_config_dir, job_profile="backend_engineer")
    assert config1.search["keywords"] == ["Backend Engineer", "Platform Engineer"]

    # Load without profile — should still have base values
    config2 = load_config(tmp_config_dir)
    assert config2.search["keywords"] == ["Software Engineer"]


def test_config_skill_emphasis_property(tmp_config_dir):
    config = load_config(tmp_config_dir, job_profile="backend_engineer")
    emphasis = config.skill_emphasis
    assert isinstance(emphasis, list)
    assert len(emphasis) == 4
    assert "python" in emphasis
