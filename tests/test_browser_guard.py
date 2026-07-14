"""The apply loop must abort when the browser/context dies, rather than
recording a phantom failure for every remaining listing."""

from velvetoverride.main import _is_browser_closed


def test_detects_playwright_closed_errors():
    for msg in (
        "Page.goto: Target page, context or browser has been closed",
        "Locator.count: Target page, context or browser has been closed",
        "Target closed",
        "Browser has been closed",
        "page has been closed",
    ):
        assert _is_browser_closed(Exception(msg)), msg


def test_ignores_unrelated_errors():
    for msg in (
        "Timeout 30000ms exceeded",
        "No Next/Submit button found",
        "net::ERR_NAME_NOT_RESOLVED",
        "",
    ):
        assert not _is_browser_closed(Exception(msg)), msg
