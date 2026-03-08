"""Configuration loader — reads YAML configs and .env secrets."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def _find_config_dir() -> Path:
    """Walk up from CWD looking for a config/ directory with settings.yaml."""
    candidate = Path.cwd()
    for _ in range(6):
        cfg = candidate / "config"
        if (cfg / "settings.yaml").exists():
            return cfg
        candidate = candidate.parent
    raise FileNotFoundError(
        "Could not find config/settings.yaml. "
        "Run from the project root or set VELVET_CONFIG_DIR."
    )


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override dict into a copy of base.

    Lists and scalar values in override replace the base value entirely.
    Nested dicts are merged recursively.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _get_profiles_dir(config_dir: Path) -> Path:
    """Return the job_profiles directory path."""
    return config_dir / "job_profiles"


def list_job_profiles(config_dir: Path | None = None) -> list[dict[str, str]]:
    """List all available job profiles with their name and description.

    Returns a list of dicts with keys: slug, name, description, path.
    """
    if config_dir is None:
        env_dir = os.environ.get("VELVET_CONFIG_DIR")
        config_dir = Path(env_dir) if env_dir else _find_config_dir()

    profiles_dir = _get_profiles_dir(config_dir)
    if not profiles_dir.exists():
        return []

    results = []
    for path in sorted(profiles_dir.glob("*.yaml")):
        data = load_yaml(path)
        results.append({
            "slug": path.stem,
            "name": data.get("name", path.stem),
            "description": data.get("description", ""),
            "path": str(path),
        })
    return results


def load_job_profile(config_dir: Path, profile_name: str) -> dict[str, Any]:
    """Load a job profile YAML by slug name (filename without extension).

    Raises FileNotFoundError if the profile doesn't exist.
    """
    profiles_dir = _get_profiles_dir(config_dir)
    profile_path = profiles_dir / f"{profile_name}.yaml"
    if not profile_path.exists():
        available = [p.stem for p in profiles_dir.glob("*.yaml")] if profiles_dir.exists() else []
        raise FileNotFoundError(
            f"Job profile '{profile_name}' not found at {profile_path}. "
            f"Available profiles: {', '.join(available) or 'none'}"
        )
    return load_yaml(profile_path)


def _apply_job_profile(
    settings: dict[str, Any],
    profile: dict[str, Any],
    answers: dict[str, Any],
    job_profile: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Apply job profile overrides to base settings, profile, and answers.

    Job profile fields map as follows:
      - search → settings.search (deep merge)
      - salary → settings.salary (deep merge)
      - summary → profile.summary (replace)
      - skill_emphasis → profile.skill_emphasis (replace, used for scoring)
      - answers → answers (deep merge)

    Returns the mutated (settings, profile, answers) tuple.
    """
    # Search overrides → settings.search
    if "search" in job_profile:
        settings["search"] = _deep_merge(
            settings.get("search", {}), job_profile["search"]
        )

    # Salary overrides → settings.salary
    if "salary" in job_profile:
        settings["salary"] = _deep_merge(
            settings.get("salary", {}), job_profile["salary"]
        )

    # Summary override → profile.summary
    if "summary" in job_profile:
        profile["summary"] = job_profile["summary"]

    # Skill emphasis → profile.skill_emphasis (new field for scoring)
    if "skill_emphasis" in job_profile:
        profile["skill_emphasis"] = job_profile["skill_emphasis"]

    # Answer overrides → answers (deep merge)
    if "answers" in job_profile:
        answers = _deep_merge(answers, job_profile["answers"])

    return settings, profile, answers


@dataclass
class Config:
    """Merged view of all configuration sources."""

    settings: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)

    # Active job profile (None if no profile selected)
    active_job_profile: str | None = None

    # Secrets from .env
    linkedin_email: str = ""
    linkedin_password: str = ""
    anthropic_api_key: str = ""
    proxy_url: str | None = None

    # Resolved paths
    config_dir: Path = field(default_factory=lambda: Path("config"))
    project_root: Path = field(default_factory=lambda: Path.cwd())

    # Convenience accessors
    @property
    def bot(self) -> dict[str, Any]:
        return self.settings.get("bot", {})

    @property
    def search(self) -> dict[str, Any]:
        return self.settings.get("search", {})

    @property
    def browser(self) -> dict[str, Any]:
        return self.settings.get("browser", {})

    @property
    def llm(self) -> dict[str, Any]:
        return self.settings.get("llm", {})

    @property
    def resume_config(self) -> dict[str, Any]:
        return self.settings.get("resume", {})

    @property
    def tracking(self) -> dict[str, Any]:
        return self.settings.get("tracking", {})

    @property
    def personal(self) -> dict[str, Any]:
        return self.profile.get("personal", {})

    @property
    def technology_experience(self) -> dict[str, Any]:
        return self.profile.get("technology_experience", {})

    @property
    def skill_emphasis(self) -> list[str]:
        """Skills to emphasize for job match scoring (from active job profile)."""
        return self.profile.get("skill_emphasis", [])


def load_config(
    config_dir: Path | None = None,
    job_profile: str | None = None,
) -> Config:
    """Load all configuration files and return a merged Config object.

    Args:
        config_dir: Path to the config directory. Auto-detected if None.
        job_profile: Optional job profile slug to apply (e.g. "backend_engineer").
            Overrides search filters, summary, salary, and answers from
            config/job_profiles/<slug>.yaml.
    """
    if config_dir is None:
        env_dir = os.environ.get("VELVET_CONFIG_DIR")
        config_dir = Path(env_dir) if env_dir else _find_config_dir()

    config_dir = Path(config_dir)
    project_root = config_dir.parent

    # Load .env (try config/.env first, then project root .env)
    for env_path in [config_dir / ".env", project_root / ".env"]:
        if env_path.exists():
            load_dotenv(env_path)
            break

    settings = load_yaml(config_dir / "settings.yaml")
    profile = load_yaml(config_dir / "profile.yaml")
    answers = load_yaml(config_dir / "answers.yaml")

    # Apply job profile overrides if specified
    active_profile_name = job_profile
    if active_profile_name:
        jp = load_job_profile(config_dir, active_profile_name)
        settings, profile, answers = _apply_job_profile(settings, profile, answers, jp)

    return Config(
        settings=settings,
        profile=profile,
        answers=answers,
        active_job_profile=active_profile_name,
        linkedin_email=os.environ.get("LINKEDIN_EMAIL", ""),
        linkedin_password=os.environ.get("LINKEDIN_PASSWORD", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        proxy_url=os.environ.get("PROXY_URL"),
        config_dir=config_dir,
        project_root=project_root,
    )
