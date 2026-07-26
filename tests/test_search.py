"""Tests for job search URL building."""

import urllib.parse

from velvetoverride.linkedin.search import build_search_url, remote_override_for
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


def _wt(url: str) -> str:
    """Return the decoded f_WT value from a search URL ('' if absent)."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return q.get("f_WT", [""])[0]


class TestRemoteOverride:
    """Per-location work-type: a nationwide search should press Remote-only
    (f_WT=2) while a located search (Seattle) keeps all configured work types."""

    def test_global_remote_used_when_no_override(self):
        config = _make_config(remote=["on_site", "remote", "hybrid"])
        url = build_search_url(config, keyword="SWE", location="Seattle, Washington, United States")
        # on_site,remote,hybrid -> 1,2,3 (order preserved)
        assert _wt(url) == "1,2,3"

    def test_remote_override_forces_remote_only(self):
        config = _make_config(remote=["on_site", "remote", "hybrid"])
        url = build_search_url(config, keyword="SWE", location="United States",
                               remote_override=["remote"])
        assert _wt(url) == "2"  # Remote button pressed, on-site NOT included

    def test_seattle_search_keeps_location_and_all_work_types(self):
        config = _make_config(remote=["on_site", "remote", "hybrid"])
        url = build_search_url(config, keyword="SWE",
                               location="Seattle, Washington, United States")
        assert "location=Seattle" in url          # Seattle entered as location
        assert _wt(url) == "1,2,3"                  # on-site kept for local search

    def test_empty_override_list_yields_no_work_type_param(self):
        # An explicit empty list means "no work-type filter" (distinct from None).
        config = _make_config(remote=["remote"])
        url = build_search_url(config, location="United States", remote_override=[])
        assert "f_WT=" not in url


class TestRemoteOverrideFor:
    """Config wiring: remote_only_locations drives which searches press Remote."""

    def test_nationwide_location_is_remote_only(self):
        config = _make_config(remote_only_locations=["United States"])
        assert remote_override_for(config, "United States") == ["remote"]

    def test_seattle_location_uses_global(self):
        config = _make_config(remote_only_locations=["United States"])
        assert remote_override_for(config, "Seattle, Washington, United States") is None

    def test_case_and_whitespace_insensitive(self):
        config = _make_config(remote_only_locations=["  united states  "])
        assert remote_override_for(config, "UNITED STATES") == ["remote"]

    def test_no_remote_only_config_returns_none(self):
        config = _make_config()  # key absent entirely
        assert remote_override_for(config, "United States") is None

    def test_end_to_end_seattle_vs_us(self):
        # Mirrors the shipped settings: Seattle keeps all work types, US is remote-only.
        config = _make_config(
            remote=["on_site", "remote", "hybrid"],
            remote_only_locations=["United States"],
        )
        seattle = build_search_url(
            config, keyword="SWE", location="Seattle, Washington, United States",
            remote_override=remote_override_for(config, "Seattle, Washington, United States"))
        us = build_search_url(
            config, keyword="SWE", location="United States",
            remote_override=remote_override_for(config, "United States"))
        assert "location=Seattle" in seattle and _wt(seattle) == "1,2,3"
        assert _wt(us) == "2"
