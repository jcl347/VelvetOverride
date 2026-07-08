"""Patchright browser engine — launches an undetected Chrome instance."""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING

from patchright.async_api import async_playwright, Browser, BrowserContext, Page

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

_COMMON_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
]

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => [4, 8][Math.floor(Math.random() * 2)]
});

Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const make = (name, filename, desc) => {
            const p = Object.create(Plugin.prototype);
            Object.defineProperties(p, {
                name: {get: () => name},
                filename: {get: () => filename},
                description: {get: () => desc},
                length: {get: () => 1},
            });
            return p;
        };
        return Object.assign(
            [
                make('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
                make('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
                make('Native Client', 'internal-nacl-plugin', ''),
            ],
            {length: 3}
        );
    }
});

if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) {
    window.chrome.runtime = {
        connect: function() {},
        sendMessage: function() {},
    };
} else {
    const orig = window.chrome.runtime.sendMessage;
    window.chrome.runtime.sendMessage = function() {
        try { return orig.apply(this, arguments); } catch(e) {}
    };
}
"""


class BrowserEngine:
    """Manages the Patchright browser lifecycle with anti-detection defaults."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def launch(self) -> Page:
        """Start the browser and return the main page."""
        bcfg = self._config.browser

        user_data_dir = Path(
            bcfg.get("user_data_dir", "browser_data")
        ).resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        viewport = bcfg.get("viewport")
        if viewport is None:
            viewport = random.choice(_COMMON_VIEWPORTS)
            log.debug("browser.random_viewport", viewport=viewport)

        timeout_ms = bcfg.get("launch_timeout_ms", 30_000)

        launch_kwargs: dict = {
            "channel": bcfg.get("channel", "chrome"),
            "headless": bcfg.get("headless", False),
            "user_data_dir": str(user_data_dir),
            "no_viewport": False,
            "viewport": viewport,
            "slow_mo": bcfg.get("slow_mo", 50),
            "timeout": timeout_ms,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }

        proxy_url = self._config.proxy_url
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}
            masked = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
            log.info("browser.proxy_configured", proxy=masked)

        log.info(
            "browser.launching",
            channel=launch_kwargs["channel"],
            headless=launch_kwargs["headless"],
            timeout_ms=timeout_ms,
        )

        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_kwargs
        )

        await self._inject_stealth_scripts()

        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

        log.info("browser.ready", viewport=viewport)
        return self._page

    async def _inject_stealth_scripts(self) -> None:
        """Register init scripts on the browser context for navigator hardening."""
        await self.context.add_init_script(_STEALTH_JS)

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._context

    async def new_page(self) -> Page:
        return await self.context.new_page()

    async def screenshot(self, path: str | Path) -> None:
        """Capture a screenshot of the current page."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=False)
        log.debug("browser.screenshot", path=str(path))

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                log.warning("browser.context_close_failed", exc_info=True)
            finally:
                self._context = None
                self._page = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                log.warning("browser.playwright_stop_failed", exc_info=True)
            finally:
                self._playwright = None
        log.info("browser.closed")
