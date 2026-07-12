"""Tests for the dashboard config view, security, and external-apply config."""

import pytest

from velvetoverride.tracking.database import TrackingDB
from velvetoverride.web.dashboard import create_app


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "d.db"
    TrackingDB(db_path).close()
    app = create_app(str(db_path), resume_dir=tmp_path / "resumes")
    (tmp_path / "resumes").mkdir(exist_ok=True)
    return app.test_client()


# ── /api/config ── (runs at repo root so load_config finds the real config)

def test_config_endpoint_returns_search_config(client):
    data = client.get("/api/config").get_json()
    assert "search" in data
    assert "keywords" in data["search"]
    assert "locations" in data["search"]
    assert "external_apply" in data
    assert "resume" in data


def test_config_endpoint_never_leaks_secrets(client):
    import json
    blob = json.dumps(client.get("/api/config").get_json()).lower()
    assert "password" not in blob
    assert "_api_key" not in blob
    assert "sk-" not in blob


# ── resume file serving: path traversal ──

def test_resume_traversal_blocked(client):
    assert client.get("/resume/..%2f..%2fconfig%2f.env").status_code == 404
    assert client.get("/resume/../../pyproject.toml").status_code == 404


def test_resume_sibling_dir_blocked(tmp_path):
    """A sibling dir starting with 'resumes' must not pass the guard."""
    (tmp_path / "resumes").mkdir()
    evil = tmp_path / "resumes_evil"
    evil.mkdir()
    (evil / "x.pdf").write_bytes(b"%PDF-1.4 secret")
    db_path = tmp_path / "d.db"
    TrackingDB(db_path).close()
    client = create_app(str(db_path), resume_dir=tmp_path / "resumes").test_client()
    # startswith('resumes') would wrongly allow this; is_relative_to blocks it
    assert client.get("/resume/../resumes_evil/x.pdf").status_code == 404


def test_resume_valid_file_served(tmp_path):
    rd = tmp_path / "resumes"
    rd.mkdir()
    (rd / "ok.pdf").write_bytes(b"%PDF-1.4 hello")
    db_path = tmp_path / "d.db"
    TrackingDB(db_path).close()
    client = create_app(str(db_path), resume_dir=rd).test_client()
    assert client.get("/resume/ok.pdf").status_code == 200


# ── external-apply config merge (new section wins, legacy fallback) ──

def _flow(settings):
    from velvetoverride.linkedin.apply import ApplicationFlow
    from velvetoverride.utils.config import Config
    f = ApplicationFlow.__new__(ApplicationFlow)
    f._config = Config(settings=settings)
    return f


def test_external_cfg_new_section_wins():
    f = _flow({"external_apply": {"enabled": False, "submit": True, "max_pages": 3}})
    cfg = f._external_cfg()
    assert cfg["enabled"] is False
    assert cfg["submit"] is True
    assert cfg["max_pages"] == 3


def test_external_cfg_legacy_fallback():
    f = _flow({"bot": {"external_apply": False, "external_max_pages": 5}})
    cfg = f._external_cfg()
    assert cfg["enabled"] is False
    assert cfg["max_pages"] == 5
    assert cfg["submit"] is False  # safe default


def test_external_cfg_default_enabled():
    f = _flow({})
    cfg = f._external_cfg()
    assert cfg["enabled"] is True
    assert cfg["submit"] is False
