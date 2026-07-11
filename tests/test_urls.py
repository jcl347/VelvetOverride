"""Tests for LinkedIn job URL / job-ID dedup helpers."""

from velvetoverride.utils.urls import extract_job_id, normalize_job_url

CANONICAL = "https://www.linkedin.com/jobs/view/3812345678"


def test_extract_from_view_url():
    assert extract_job_id("https://www.linkedin.com/jobs/view/3812345678/") == "3812345678"


def test_extract_from_slug_url():
    url = "https://www.linkedin.com/jobs/view/senior-data-scientist-at-acme-3812345678"
    assert extract_job_id(url) == "3812345678"


def test_extract_from_current_job_id():
    url = "https://www.linkedin.com/jobs/search/?currentJobId=3812345678&keywords=data"
    assert extract_job_id(url) == "3812345678"


def test_extract_from_relative_url():
    assert extract_job_id("/jobs/view/3812345678?refId=abc") == "3812345678"


def test_extract_none():
    assert extract_job_id("https://www.linkedin.com/feed/") is None
    assert extract_job_id("") is None
    assert extract_job_id(None) is None


def test_all_shapes_normalize_identically():
    shapes = [
        "https://www.linkedin.com/jobs/view/3812345678/",
        "https://www.linkedin.com/jobs/view/data-scientist-3812345678",
        "https://www.linkedin.com/jobs/search/?currentJobId=3812345678&f=1",
        "/jobs/view/3812345678?refId=abc",
        "https://www.linkedin.com/jobs/view/3812345678/?trk=xyz",
    ]
    assert {normalize_job_url(s) for s in shapes} == {CANONICAL}


def test_normalize_without_id_strips_query():
    url = "https://example.com/careers/role?utm=1#top"
    assert normalize_job_url(url) == "https://example.com/careers/role"
