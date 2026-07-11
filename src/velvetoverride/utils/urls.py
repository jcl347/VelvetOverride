"""LinkedIn job URL helpers — canonical job-ID extraction and URL normalization.

Used for robust de-duplication so the bot never re-applies to the same job even
when LinkedIn hands it back under a different URL shape (search result, collection
view, tracking params, currentJobId query, etc.).
"""

from __future__ import annotations

import re
import urllib.parse

# Matches the numeric job id in the common LinkedIn URL shapes:
#   https://www.linkedin.com/jobs/view/1234567890/
#   https://www.linkedin.com/jobs/view/some-title-1234567890
#   .../jobs/search/?currentJobId=1234567890
#   .../comm/jobs/view/1234567890
_VIEW_RE = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})", re.IGNORECASE)
_CURRENT_JOB_RE = re.compile(r"[?&]currentJobId=(\d{6,})", re.IGNORECASE)
_TRAILING_ID_RE = re.compile(r"(\d{8,})")


def extract_job_id(url: str | None) -> str | None:
    """Return the canonical numeric LinkedIn job ID from a URL, or None."""
    if not url:
        return None

    m = _VIEW_RE.search(url)
    if m:
        return m.group(1)

    m = _CURRENT_JOB_RE.search(url)
    if m:
        return m.group(1)

    # Fallback: a long standalone number in the path (last resort)
    try:
        path = urllib.parse.urlparse(url).path
    except ValueError:
        path = url
    m = _TRAILING_ID_RE.search(path)
    if m:
        return m.group(1)

    return None


def normalize_job_url(url: str | None) -> str:
    """Canonicalize a job URL for dedup.

    If a job ID can be extracted, collapse to the canonical
    ``https://www.linkedin.com/jobs/view/<id>`` form. Otherwise strip tracking
    query params and any trailing slash.
    """
    if not url:
        return ""

    job_id = extract_job_id(url)
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}"

    # No id — strip query/fragment and normalize
    parsed = urllib.parse.urlparse(url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean.rstrip("/")
