"""Tests for job search URL building."""

from velvetoverride.linkedin.search import build_search_url
from velvetoverride.utils.config import Config


def _make_config(**search_overrides) -> Config:
    search = {
        "keywords": ["Software Engineer"],
        "locations": ["United States"],
        "easy_apply_only": True,
        "experience_levels": ["mid_senior"],
        "date_posted": "past_week",
        "remote": ["remote"],
        "job_types": ["full_time"],
    }
    search.update(search_overrides)
    return Config(
        settings={"search": search, "bot": {}, "browser": {}, "llm": {}, "resume": {}, "tracking": {}},
        profile={},
        answers={},
    )


class TestBuildSearchUrl:
    def test_basic_url(self):
        config = _make_config()
        url = build_search_url(config)
        assert "linkedin.com/jobs/search" in url
        assert "keywords=Software+Engineer" in url
        assert "location=United+States" in url

    def test_easy_apply_filter(self):
        config = _make_config(easy_apply_only=True)
        url = build_search_url(config)
        assert "f_AL=true" in url

    def test_experience_level(self):
        config = _make_config(experience_levels=["entry_level", "mid_senior"])
        url = build_search_url(config)
        assert "f_E=" in url
        assert "2" in url  # entry_level
        assert "4" in url  # mid_senior

    def test_date_posted(self):
        config = _make_config(date_posted="past_24h")
        url = build_search_url(config)
        assert "f_TPR=r86400" in url

    def test_remote_filter(self):
        config = _make_config(remote=["remote", "hybrid"])
        url = build_search_url(config)
        assert "f_WT=" in url

    def test_job_type(self):
        config = _make_config(job_types=["full_time", "contract"])
        url = build_search_url(config)
        assert "f_JT=" in url
