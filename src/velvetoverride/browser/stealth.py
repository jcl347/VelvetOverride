"""Human-like behavior simulation for anti-detection."""

from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page

log = get_logger(__name__)

_PAUSE_PROFILES: dict[str, tuple[float, float, float]] = {
    "reading": (0.8, 0.3, 4.0),
    "thinking": (2.5, 0.5, 8.0),
    "distraction": (6.0, 0.7, 20.0),
}


async def random_delay(min_sec: float = 1.0, max_sec: float = 4.0) -> None:
    """Sleep with a log-normal distribution clamped to [min_sec, max_sec].

    Log-normal produces the right-skewed timing distribution observed in real
    human interaction -- most pauses are short, with occasional longer ones.
    """
    mu = math.log((min_sec + max_sec) / 2)
    sigma = 0.4
    delay = random.lognormvariate(mu, sigma)
    delay = max(min_sec, min(delay, max_sec))
    await asyncio.sleep(delay)


async def natural_pause(activity: str = "reading") -> None:
    """Pause with timing calibrated to a type of human activity."""
    mu, sigma, ceiling = _PAUSE_PROFILES.get(
        activity, _PAUSE_PROFILES["reading"]
    )
    delay = random.lognormvariate(math.log(mu), sigma)
    delay = max(0.3, min(delay, ceiling))
    log.debug("stealth.pause", activity=activity, delay_sec=round(delay, 2))
    await asyncio.sleep(delay)


async def human_type(page: Page, selector: str, text: str) -> None:
    """Type text character-by-character with variable speed."""
    element = page.locator(selector)
    await element.click()
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


async def randomize_scroll_pattern(page: Page, direction: str = "down") -> None:
    """Perform a multi-segment scroll with variable speed and pauses."""
    segments = random.randint(2, 5)
    for i in range(segments):
        amount = random.randint(100, 500)
        await human_scroll(page, direction, amount=amount)

        if i < segments - 1:
            # Occasionally micro-scroll in the opposite direction, like a
            # human overshooting and correcting.
            if random.random() < 0.3:
                micro = random.randint(30, 120)
                opposite = "up" if direction == "down" else "down"
                await human_scroll(page, opposite, amount=micro)

            await natural_pause("reading")

    log.debug("stealth.scroll_pattern", direction=direction, segments=segments)


async def _bezier_mouse_move(page: Page, target_x: float, target_y: float) -> None:
    """Move mouse along a cubic bezier curve to the target coordinates.

    Straight-line mouse teleportation is a strong bot signal. Bezier curves
    with randomized control points produce the imprecise arcing trajectories
    that real wrist/finger movements create.
    """
    viewport = page.viewport_size
    if viewport is None:
        await page.mouse.move(target_x, target_y)
        return

    start_x = random.uniform(0, viewport["width"])
    start_y = random.uniform(0, viewport["height"])

    cp1_x = start_x + (target_x - start_x) * random.uniform(0.2, 0.5) + random.uniform(-80, 80)
    cp1_y = start_y + (target_y - start_y) * random.uniform(0.1, 0.4) + random.uniform(-80, 80)
    cp2_x = start_x + (target_x - start_x) * random.uniform(0.5, 0.8) + random.uniform(-40, 40)
    cp2_y = start_y + (target_y - start_y) * random.uniform(0.6, 0.9) + random.uniform(-40, 40)

    steps = random.randint(18, 35)
    for i in range(steps + 1):
        t = i / steps

        # Smoothstep ease-in-out: acceleration then deceleration
        t = t * t * (3.0 - 2.0 * t)

        inv = 1.0 - t
        bx = (
            (inv ** 3) * start_x
            + 3 * (inv ** 2) * t * cp1_x
            + 3 * inv * (t ** 2) * cp2_x
            + (t ** 3) * target_x
        )
        by = (
            (inv ** 3) * start_y
            + 3 * (inv ** 2) * t * cp1_y
            + 3 * inv * (t ** 2) * cp2_y
            + (t ** 3) * target_y
        )

        bx += random.uniform(-1.5, 1.5)
        by += random.uniform(-1.5, 1.5)

        await page.mouse.move(bx, by)
        await asyncio.sleep(random.uniform(0.004, 0.018))


async def human_click(page: Page, selector: str) -> None:
    """Move along a bezier curve then click with slight positional jitter."""
    element = page.locator(selector)
    bbox = await element.bounding_box()
    if bbox:
        x = bbox["x"] + bbox["width"] / 2 + random.uniform(-3, 3)
        y = bbox["y"] + bbox["height"] / 2 + random.uniform(-3, 3)
        await _bezier_mouse_move(page, x, y)
        await asyncio.sleep(random.uniform(0.03, 0.12))
        await page.mouse.click(x, y)
    else:
        await element.click()
    log.debug("stealth.clicked", selector=selector)


async def diversify_activity(page: Page) -> None:
    """Perform a weighted-random non-application action between applies.

    Idle pause is weighted heavily since it carries zero navigation risk.
    In-page actions are preferred over hard navigation to avoid detectable
    URL pattern changes mid-session.
    """
    weighted_actions: list[tuple[int, object]] = [
        (40, _idle_pause),
        (15, _scroll_current_page),
        (12, _hover_profile_pics),
        (10, _peek_messaging),
        (10, _scroll_up_then_down),
        (8, _scroll_feed),
        (5, _check_notifications),
    ]

    total = sum(w for w, _ in weighted_actions)
    roll = random.randint(1, total)
    cumulative = 0
    chosen = weighted_actions[0][1]
    for weight, action in weighted_actions:
        cumulative += weight
        if roll <= cumulative:
            chosen = action
            break

    await chosen(page)  # type: ignore[operator]


async def _idle_pause(page: Page) -> None:
    log.debug("stealth.diversify", action="idle_pause")
    await natural_pause("distraction")


async def _scroll_current_page(page: Page) -> None:
    log.debug("stealth.diversify", action="scroll_current_page")
    await randomize_scroll_pattern(page, "down")
    await natural_pause("reading")


async def _hover_profile_pics(page: Page) -> None:
    log.debug("stealth.diversify", action="hover_profile_pics")
    pics = page.locator(
        "img.presence-entity__image, "
        "img.EntityPhoto-circle-3, "
        "img[alt*='photo']"
    )
    count = await pics.count()
    if count > 0:
        idx = random.randint(0, min(count - 1, 4))
        bbox = await pics.nth(idx).bounding_box()
        if bbox:
            await _bezier_mouse_move(
                page,
                bbox["x"] + bbox["width"] / 2,
                bbox["y"] + bbox["height"] / 2,
            )
            await natural_pause("reading")
            return

    await natural_pause("thinking")


async def _peek_messaging(page: Page) -> None:
    """Click the messaging icon, pause briefly, then dismiss."""
    log.debug("stealth.diversify", action="peek_messaging")
    msg_btn = page.locator(
        "a[href*='/messaging/'], "
        "button[aria-label*='Messaging'], "
        "li.msg-overlay-list-bubble"
    ).first
    try:
        if await msg_btn.is_visible(timeout=2000):
            bbox = await msg_btn.bounding_box()
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await _bezier_mouse_move(page, cx, cy)
                await asyncio.sleep(random.uniform(0.03, 0.1))
                await page.mouse.click(cx, cy)
            else:
                await msg_btn.click()
            await natural_pause("reading")
            await page.keyboard.press("Escape")
            await natural_pause("reading")
            return
    except Exception:
        pass

    await natural_pause("thinking")


async def _scroll_up_then_down(page: Page) -> None:
    log.debug("stealth.diversify", action="scroll_up_down")
    up_amount = random.randint(200, 500)
    await human_scroll(page, "up", amount=up_amount)
    await natural_pause("reading")
    down_amount = up_amount + random.randint(-100, 150)
    await human_scroll(page, "down", amount=max(100, down_amount))
    await natural_pause("reading")


async def _check_notifications(page: Page) -> None:
    log.debug("stealth.diversify", action="check_notifications")
    notif_link = page.locator("a[href*='/notifications/']").first
    try:
        if await notif_link.is_visible(timeout=2000):
            bbox = await notif_link.bounding_box()
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await _bezier_mouse_move(page, cx, cy)
                await asyncio.sleep(random.uniform(0.03, 0.1))
                await page.mouse.click(cx, cy)
            else:
                await notif_link.click()
            await natural_pause("thinking")
            await human_scroll(page, "down")
            await natural_pause("reading")
            await page.go_back()
            await natural_pause("reading")
            return
    except Exception:
        pass

    await page.goto(
        "https://www.linkedin.com/notifications/",
        wait_until="domcontentloaded",
    )
    await natural_pause("thinking")
    await human_scroll(page, "down")
    await natural_pause("reading")


async def _scroll_feed(page: Page) -> None:
    log.debug("stealth.diversify", action="scroll_feed")
    await page.goto(
        "https://www.linkedin.com/feed/",
        wait_until="domcontentloaded",
    )
    await randomize_scroll_pattern(page, "down")
    await natural_pause("reading")
