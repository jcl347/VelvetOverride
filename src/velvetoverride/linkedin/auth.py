"""LinkedIn authentication — login and session management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from velvetoverride.browser.stealth import human_type, random_delay
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"


async def is_logged_in(page: Page) -> bool:
    """Check if the current session is authenticated."""
    await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=15000)
    # If we land on the feed, we're logged in
    url = page.url
    if "/feed" in url:
        return True
    # If redirected to login or auth wall, we're not
    return False


async def login(page: Page, config: Config) -> bool:
    """Log in to LinkedIn using credentials from config.

    Returns True if login succeeded.
    """
    email = config.linkedin_email
    password = config.linkedin_password

    if not email or not password:
        log.error("auth.missing_credentials")
        raise ValueError(
            "LinkedIn credentials not configured. "
            "Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in config/.env"
        )

    # Check if already logged in via persistent session
    if await is_logged_in(page):
        log.info("auth.session_reused")
        return True

    log.info("auth.logging_in", email=email[:3] + "***")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await random_delay(1.0, 2.5)

    # Fill email
    email_input = page.locator("#username")
    await email_input.fill("")
    await email_input.type(email, delay=random_delay)
    await random_delay(0.5, 1.5)

    # Fill password
    password_input = page.locator("#password")
    await password_input.fill("")
    await password_input.type(password, delay=random_delay)
    await random_delay(0.5, 1.0)

    # Click sign in
    await page.locator('button[type="submit"]').click()
    await random_delay(3.0, 6.0)

    # Check for security checkpoint (CAPTCHA, verification)
    current_url = page.url
    if "checkpoint" in current_url or "challenge" in current_url:
        log.warning(
            "auth.security_checkpoint",
            url=current_url,
            msg="Manual intervention required — solve the CAPTCHA in the browser window.",
        )
        # Wait up to 120 seconds for human to solve checkpoint
        try:
            await page.wait_for_url("**/feed/**", timeout=120000)
        except Exception:
            log.error("auth.checkpoint_timeout")
            return False

    # Verify we landed on feed
    if "/feed" in page.url:
        log.info("auth.login_success")
        return True

    log.error("auth.login_failed", url=page.url)
    return False
