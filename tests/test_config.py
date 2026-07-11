"""Tests for configuration loading."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from velvetoverride.utils.config import Config, load_config, load_yaml


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary config directory with minimal valid configs."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    settings = {
        "bot": {"dry_run": True, "max_applications": 5},
        "search": {"keywords": ["Engineer"], "locations": ["US"]},
        "browser": {"channel": "chrome", "headless": True},
        "llm": {"field_model": "claude-sonnet-4-6"},
        "resume": {"template": "default"},
        "tracking": {"database_path": str(tmp_path / "test.db")},
    }
    with open(config_dir / "settings.yaml", "w") as f:
        yaml.dump(settings, f)

    profile = {
        "personal": {"first_name": "Test", "last_name": "User", "email": "test@test.com"},
        "summary": "A test user.",
        "experience": [],
        "skills": {"languages": ["Python"]},
        "technology_experience": {"python": 3, "default": 0},
    }
    with open(config_dir / "profile.yaml", "w") as f:
        yaml.dump(profile, f)

    answers = {
        "yes_no": {
            "legally_authorized": {
                "patterns": ["legally authorized"],
                "answer": True,
            }
        },
        "eeo": {"patterns": ["gender"], "decline_keywords": ["decline"]},
        "numeric": {"experience_patterns": ["years of experience"]},
        "learned": {},
    }
    with open(config_dir / "answers.yaml", "w") as f:
        yaml.dump(answers, f)

    return config_dir


def test_load_yaml(tmp_config_dir):
    data = load_yaml(tmp_config_dir / "settings.yaml")
    assert data["bot"]["dry_run"] is True


def test_load_config(tmp_config_dir):
    config = load_config(tmp_config_dir)
    assert config.bot["dry_run"] is True
    assert config.personal["first_name"] == "Test"
    assert config.search["keywords"] == ["Engineer"]


def test_config_properties(tmp_config_dir):
    config = load_config(tmp_config_dir)
    assert isinstance(config.browser, dict)
    assert config.llm["field_model"] == "claude-sonnet-4-6"
    assert config.technology_experience["python"] == 3


def test_local_yaml_overrides_committed(tmp_path):
    """profile.local.yaml (gitignored real data) must win over profile.yaml."""
    from velvetoverride.utils.config import _load_local_first
    (tmp_path / "profile.yaml").write_text("personal:\n  first_name: Placeholder\n", encoding="utf-8")
    (tmp_path / "profile.local.yaml").write_text("personal:\n  first_name: Real\n", encoding="utf-8")
    data = _load_local_first(tmp_path, "profile")
    assert data["personal"]["first_name"] == "Real"


def test_falls_back_to_committed_when_no_local(tmp_path):
    from velvetoverride.utils.config import _load_local_first
    (tmp_path / "answers.yaml").write_text("yes_no: {}\n", encoding="utf-8")
    data = _load_local_first(tmp_path, "answers")
    assert "yes_no" in data
