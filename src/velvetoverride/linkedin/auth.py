"""LinkedIn authentication — login and session management."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from velvetoverride.browser.captcha import detect_captcha, handle_captcha
from velvetoverride.browser.stealth import random_delay
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"


async def _read_login_errors(page: Page) -> list[str]:
    """Read visible red error/validation messages on the login page."""
    messages: list[str] = []
    try:
        err = page.locator(
            '#error-for-username, #error-for-password, '
            '.form__label--error, [role="alert"], '
            '.alert-content, div.form-toast--error, '
            'div[error-for], .artdeco-inline-feedback--error'
        )
        for i in range(min(await err.count(), 6)):
            el = err.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = " ".join((await el.text_content() or "").split()).strip()
                if text and text not in messages:
                    messages.append(text[:200])
            except Exception:
                continue
    except Exception:
        pass
    return messages


async def _has_auth_cookie(page: Page) -> bool:
    """Passively check for LinkedIn's authenticated-session cookie (no navigation)."""
    try:
        cookies = await page.context.cookies("https://www.linkedin.com")
        return any(c.get("name") == "li_at" and c.get("value") for c in cookies)
    except Exception:
        return False


async def is_logged_in(page: Page) -> bool:
    """Check if the current session is authenticated.

    NOTE: this NAVIGATES to the feed. Do not call it from the manual-login poll
    loop (use `_has_auth_cookie` there) — navigating would wipe a user's
    in-progress sign-in.
    """
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

    # Try scripted credential entry. LinkedIn frequently serves a variant page
    # (account picker, consent wall, challenge) with no #username field — if the
    # form isn't there quickly, fall back to manual login rather than hanging.
    try:
        email_input = page.locator("#username")
        await email_input.wait_for(state="visible", timeout=12000)
        await email_input.fill("")
        await email_input.type(email, delay=random.randint(50, 150))
        await random_delay(0.5, 1.5)

        password_input = page.locator("#password")
        await password_input.fill("")
        await password_input.type(password, delay=random.randint(50, 150))
        await random_delay(0.5, 1.0)

        await page.locator('button[type="submit"]').click()
        await random_delay(3.0, 6.0)
    except Exception as e:
        log.warning("auth.form_not_scriptable", error=str(e)[:120])

    # Surface any red validation/error banners the login page shows
    for err in await _read_login_errors(page):
        log.warning("auth.login_error_message", message=err)

    # Check for security checkpoint (CAPTCHA, verification)
    if await detect_captcha(page):
        log.warning("auth.security_checkpoint", url=page.url)
        resolved = await handle_captcha(page, config)
        if not resolved:
            log.error("auth.checkpoint_unresolved")
            return False
        await random_delay(2.0, 4.0)

    # Verify we landed on feed
    if "/feed" in page.url:
        log.info("auth.login_success")
        return True

    # ── Manual-login fallback ──
    # Scripted login didn't land us on the feed. Leave the (headed) browser open
    # and poll for the user to finish logging in by hand — enter credentials,
    # solve any CAPTCHA / 2FA — up to the configured captcha timeout.
    timeout_s = int(config.settings.get("captcha", {}).get("timeout", 300))

    # Best-effort: if the page offers Google SSO (common when the account is
    # linked to Google), surface the Google account picker to speed up the
    # one-time manual sign-in. Completion is still done by the user.
    try:
        google_btn = page.locator(
            'button:has-text("Continue with Google"), '
            'button:has-text("Sign in with Google"), '
            'a:has-text("Continue with Google"), '
            '[aria-label*="Google"]'
        )
        if await google_btn.count() > 0 and await google_btn.first.is_visible():
            log.info("auth.clicking_google_sso")
            await google_btn.first.click()
            await random_delay(2.0, 4.0)
    except Exception as e:
        log.debug("auth.google_sso_click_failed", error=str(e)[:100])

    log.warning(
        "auth.manual_login_required",
        msg=f"Please finish signing in to LinkedIn in the open Chrome window "
            f"(e.g. 'Continue with Google'); waiting up to {timeout_s}s.",
    )
    waited = 0
    poll = 5
    while waited < timeout_s:
        await random_delay(float(poll), float(poll))
        waited += poll
        try:
            if "/feed" in page.url:
                log.info("auth.login_success", via="manual")
                return True
            # PASSIVE check only — do NOT navigate here. Force-navigating to the
            # feed while the user is mid-way through the Google account picker /
            # 2FA / SSO password would reload the tab and wipe their input every
            # few seconds, making manual login impossible. LinkedIn sets the
            # `li_at` cookie once authenticated; detect that instead.
            if await _has_auth_cookie(page):
                # Confirm ONCE now that a real session exists.
                if await is_logged_in(page):
                    log.info("auth.login_success", via="manual_session")
                    return True
        except Exception:
            pass
        if waited % 30 == 0:
            log.info("auth.waiting_for_manual_login", waited=waited, timeout=timeout_s)

    log.error("auth.login_failed", url=page.url)
    return False
