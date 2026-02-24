"""Human-like behavior simulation for anti-detection."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page

log = get_logger(__name__)


async def random_delay(min_sec: float = 1.0, max_sec: float = 4.0) -> None:
    """Sleep a random duration to mimic human thinking time."""
    delay = random.uniform(min_sec, max_sec)
    await asyncio.sleep(delay)


async def human_type(page: Page, selector: str, text: str) -> None:
    """Type text character-by-character with variable speed."""
    element = page.locator(selector)
    await element.click()
    # Clear existing content
    await element.fill("")
    for char in text:
        await element.press_sequentially(char, delay=random.randint(30, 120))
    log.debug("stealth.typed", selector=selector, length=len(text))


async def human_scroll(page: Page, direction: str = "down", amount: int | None = None) -> None:
    """Scroll the page in a human-like fashion."""
    if amount is None:
        amount = random.randint(200, 600)
    delta = amount if direction == "down" else -amount
    await page.mouse.wheel(0, delta)
    await random_delay(0.3, 1.0)


async def human_click(page: Page, selector: str) -> None:
    """Click an element with a slight random offset and delay."""
    element = page.locator(selector)
    bbox = await element.bounding_box()
    if bbox:
        # Click with slight random offset from center
        x = bbox["x"] + bbox["width"] / 2 + random.uniform(-3, 3)
        y = bbox["y"] + bbox["height"] / 2 + random.uniform(-3, 3)
        await page.mouse.click(x, y)
    else:
        await element.click()
    log.debug("stealth.clicked", selector=selector)


async def diversify_activity(page: Page) -> None:
    """Occasionally perform non-application actions to look natural.

    Call this between applications to break up robotic patterns.
    """
    actions = [
        _check_notifications,
        _scroll_feed,
        _idle_pause,
    ]
    action = random.choice(actions)
    await action(page)


async def _check_notifications(page: Page) -> None:
    log.debug("stealth.diversify", action="check_notifications")
    await page.goto("https://www.linkedin.com/notifications/", wait_until="domcontentloaded")
    await random_delay(2.0, 5.0)
    await human_scroll(page, "down")
    await random_delay(1.0, 3.0)


async def _scroll_feed(page: Page) -> None:
    log.debug("stealth.diversify", action="scroll_feed")
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    for _ in range(random.randint(2, 5)):
        await human_scroll(page, "down")
        await random_delay(1.0, 3.0)


async def _idle_pause(page: Page) -> None:
    log.debug("stealth.diversify", action="idle_pause")
    await random_delay(5.0, 15.0)
