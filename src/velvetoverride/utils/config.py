"""Configuration loader — reads YAML configs and .env secrets."""

from __future__ import annotations

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
    # Explicit UTF-8: Windows defaults to cp1252, which silently mangles
    # non-ASCII (an em dash becomes "â€”") everywhere it's later used.
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_local_first(config_dir: Path, name: str) -> dict[str, Any]:
    """Load ``<name>.local.yaml`` if present (gitignored, your real data),
    otherwise the committed ``<name>.yaml`` placeholder.

    This keeps personal info (name, email, phone) out of version control:
    copy profile.yaml -> profile.local.yaml and edit the local copy.
    """
    local = config_dir / f"{name}.local.yaml"
    if local.exists():
        return load_yaml(local)
    return load_yaml(config_dir / f"{name}.yaml")


@dataclass
class Config:
    """Merged view of all configuration sources."""

    settings: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)

    # Secrets from .env
    linkedin_email: str = ""
    linkedin_password: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
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
    def llm_provider(self) -> str:
        return self.llm.get("provider", "openai").lower()

    @property
    def has_llm(self) -> bool:
        """True if the active provider has an API key configured."""
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return bool(self.openai_api_key)

    @property
    def personal(self) -> dict[str, Any]:
        return self.profile.get("personal", {})

    @property
    def technology_experience(self) -> dict[str, Any]:
        return self.profile.get("technology_experience", {})


def load_config(config_dir: Path | None = None) -> Config:
    """Load all configuration files and return a merged Config object."""
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

    settings = _load_local_first(config_dir, "settings")
    profile = _load_local_first(config_dir, "profile")
    answers = _load_local_first(config_dir, "answers")

    return Config(
        settings=settings,
        profile=profile,
        answers=answers,
        linkedin_email=os.environ.get("LINKEDIN_EMAIL", ""),
        linkedin_password=os.environ.get("LINKEDIN_PASSWORD", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        proxy_url=os.environ.get("PROXY_URL"),
        config_dir=config_dir,
        project_root=project_root,
    )
