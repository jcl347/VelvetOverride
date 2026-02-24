"""CAPTCHA detection and resolution strategies.

Supports three strategies:
1. Manual — pause and wait for human to solve in the browser window
2. 2Captcha API — send CAPTCHA to a solving service
3. CapSolver API — AI-based CAPTCHA solving service

Strategy is configured via settings.yaml → captcha.strategy
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

CHECKPOINT_INDICATORS = ["checkpoint", "challenge", "captcha", "security-verification"]


async def detect_captcha(page: Page) -> bool:
    """Check if the current page is a CAPTCHA or security checkpoint."""
    url = page.url.lower()
    if any(indicator in url for indicator in CHECKPOINT_INDICATORS):
        return True

    # Check page content for CAPTCHA iframes or elements
    captcha_selectors = [
        'iframe[src*="captcha"]',
        'iframe[src*="recaptcha"]',
        'iframe[src*="challenge"]',
        "#captcha-internal",
        ".captcha-container",
        '[data-testid="captcha"]',
    ]
    for selector in captcha_selectors:
        if await page.locator(selector).count() > 0:
            return True

    return False


async def handle_captcha(page: Page, config: Config) -> bool:
    """Attempt to resolve a CAPTCHA using the configured strategy.

    Returns True if the CAPTCHA was resolved and the page moved past it.
    """
    captcha_cfg = config.settings.get("captcha", {})
    strategy = captcha_cfg.get("strategy", "manual")
    timeout = captcha_cfg.get("timeout", 300)

    log.warning(
        "captcha.detected",
        url=page.url,
        strategy=strategy,
    )

    match strategy:
        case "manual":
            return await _manual_solve(page, timeout)
        case "2captcha":
            api_key = captcha_cfg.get("api_key") or os.environ.get("CAPTCHA_API_KEY", "")
            if not api_key:
                log.warning("captcha.no_api_key", msg="Falling back to manual")
                return await _manual_solve(page, timeout)
            return await _2captcha_solve(page, api_key, timeout)
        case "capsolver":
            api_key = captcha_cfg.get("api_key") or os.environ.get("CAPTCHA_API_KEY", "")
            if not api_key:
                log.warning("captcha.no_api_key", msg="Falling back to manual")
                return await _manual_solve(page, timeout)
            return await _capsolver_solve(page, api_key, timeout)
        case _:
            log.warning("captcha.unknown_strategy", strategy=strategy)
            return await _manual_solve(page, timeout)


async def _manual_solve(page: Page, timeout: int) -> bool:
    """Wait for a human to manually solve the CAPTCHA in the browser window.

    Works best with VNC/desktop mode where the user can see and interact
    with the browser. Emits periodic reminders to stderr.
    """
    log.warning(
        "captcha.manual_required",
        timeout=timeout,
        msg=f"Solve the CAPTCHA in the browser window within {timeout}s",
    )

    poll_interval = 5
    elapsed = 0

    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        # Check if we moved past the checkpoint
        if not await detect_captcha(page):
            log.info("captcha.resolved", method="manual", elapsed=elapsed)
            return True

        if elapsed % 30 == 0:
            log.warning("captcha.still_waiting", elapsed=elapsed, remaining=timeout - elapsed)

    log.error("captcha.timeout", method="manual", timeout=timeout)
    return False


async def _2captcha_solve(page: Page, api_key: str, timeout: int) -> bool:
    """Solve CAPTCHA using the 2Captcha API service.

    Supports reCAPTCHA v2 (most common on LinkedIn).
    Pricing: ~$1-3 per 1,000 solves.
    Docs: https://2captcha.com/2captcha-api

    Falls back to manual if the API solve fails.
    """
    try:
        import httpx
    except ImportError:
        log.warning("captcha.httpx_missing", msg="httpx required for 2captcha; falling back to manual")
        return await _manual_solve(page, timeout)

    # Look for reCAPTCHA sitekey
    sitekey = await _extract_recaptcha_sitekey(page)
    if not sitekey:
        log.warning("captcha.no_sitekey", msg="Cannot find reCAPTCHA sitekey; falling back to manual")
        return await _manual_solve(page, timeout)

    page_url = page.url

    log.info("captcha.2captcha_submitting", sitekey=sitekey[:20])

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Step 1: Submit the CAPTCHA
        submit_resp = await client.post(
            "https://2captcha.com/in.php",
            data={
                "key": api_key,
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page_url,
                "json": "1",
            },
        )
        submit_data = submit_resp.json()

        if submit_data.get("status") != 1:
            log.error("captcha.2captcha_submit_failed", response=submit_data)
            return await _manual_solve(page, timeout)

        task_id = submit_data["request"]
        log.info("captcha.2captcha_submitted", task_id=task_id)

        # Step 2: Poll for the solution
        for _ in range(timeout // 5):
            await asyncio.sleep(5)

            result_resp = await client.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
            )
            result_data = result_resp.json()

            if result_data.get("status") == 1:
                token = result_data["request"]
                log.info("captcha.2captcha_solved", task_id=task_id)

                # Inject the solution token
                await _inject_recaptcha_token(page, token)
                await asyncio.sleep(2)

                if not await detect_captcha(page):
                    log.info("captcha.resolved", method="2captcha")
                    return True

            elif result_data.get("request") != "CAPCHA_NOT_READY":
                log.error("captcha.2captcha_error", response=result_data)
                break

    log.warning("captcha.2captcha_failed", msg="Falling back to manual")
    return await _manual_solve(page, timeout)


async def _capsolver_solve(page: Page, api_key: str, timeout: int) -> bool:
    """Solve CAPTCHA using the CapSolver API service.

    AI-based solving, typically faster than human-powered services.
    Pricing: ~$0.80-3 per 1,000 solves.
    Docs: https://docs.capsolver.com/

    Falls back to manual if the API solve fails.
    """
    try:
        import httpx
    except ImportError:
        log.warning("captcha.httpx_missing", msg="httpx required for capsolver; falling back to manual")
        return await _manual_solve(page, timeout)

    sitekey = await _extract_recaptcha_sitekey(page)
    if not sitekey:
        log.warning("captcha.no_sitekey", msg="Falling back to manual")
        return await _manual_solve(page, timeout)

    page_url = page.url

    log.info("captcha.capsolver_submitting", sitekey=sitekey[:20])

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Step 1: Create task
        create_resp = await client.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "ReCaptchaV2TaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
        )
        create_data = create_resp.json()

        if create_data.get("errorId", 1) != 0:
            log.error("captcha.capsolver_create_failed", response=create_data)
            return await _manual_solve(page, timeout)

        task_id = create_data.get("taskId")
        log.info("captcha.capsolver_submitted", task_id=task_id)

        # Step 2: Poll for result
        for _ in range(timeout // 3):
            await asyncio.sleep(3)

            result_resp = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            result_data = result_resp.json()

            status = result_data.get("status")
            if status == "ready":
                token = result_data.get("solution", {}).get("gRecaptchaResponse", "")
                if token:
                    log.info("captcha.capsolver_solved", task_id=task_id)
                    await _inject_recaptcha_token(page, token)
                    await asyncio.sleep(2)

                    if not await detect_captcha(page):
                        log.info("captcha.resolved", method="capsolver")
                        return True

            elif status == "failed":
                log.error("captcha.capsolver_failed", response=result_data)
                break

    log.warning("captcha.capsolver_failed", msg="Falling back to manual")
    return await _manual_solve(page, timeout)


async def _extract_recaptcha_sitekey(page: Page) -> str | None:
    """Extract the reCAPTCHA sitekey from the page."""
    # Try iframe src
    iframe = page.locator('iframe[src*="recaptcha"]')
    if await iframe.count() > 0:
        src = await iframe.first.get_attribute("src") or ""
        if "k=" in src:
            return src.split("k=")[1].split("&")[0]

    # Try data-sitekey attribute
    sitekey_el = page.locator("[data-sitekey]")
    if await sitekey_el.count() > 0:
        return await sitekey_el.first.get_attribute("data-sitekey")

    # Try grecaptcha render parameter in scripts
    sitekey = await page.evaluate("""() => {
        const el = document.querySelector('[data-sitekey]');
        if (el) return el.getAttribute('data-sitekey');
        const iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
        for (const iframe of iframes) {
            const match = iframe.src.match(/[?&]k=([^&]+)/);
            if (match) return match[1];
        }
        return null;
    }""")

    return sitekey


async def _inject_recaptcha_token(page: Page, token: str) -> None:
    """Inject a solved reCAPTCHA token into the page and submit."""
    await page.evaluate(f"""(token) => {{
        // Set the textarea value
        const textarea = document.getElementById('g-recaptcha-response');
        if (textarea) {{
            textarea.style.display = 'block';
            textarea.value = token;
        }}
        // Also try setting via all response textareas (multiple reCAPTCHAs)
        document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{
            el.value = token;
        }});
        // Trigger the callback if available
        if (typeof ___grecaptcha_cfg !== 'undefined') {{
            const clients = ___grecaptcha_cfg.clients;
            if (clients) {{
                Object.keys(clients).forEach(key => {{
                    const client = clients[key];
                    if (client && client.rr && client.rr.l) {{
                        client.rr.l.callback(token);
                    }}
                }});
            }}
        }}
    }}""", token)

    # Try clicking submit/verify button
    verify_btn = page.locator('button:has-text("Verify"), input[type="submit"]')
    if await verify_btn.count() > 0:
        await verify_btn.first.click()
