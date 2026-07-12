"""Patchright browser engine — launches an undetected Chrome instance."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from patchright.async_api import async_playwright, Browser, BrowserContext, Page

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.utils.config import Config

log = get_logger(__name__)


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

        launch_kwargs: dict = {
            "channel": bcfg.get("channel", "chrome"),
            "headless": bcfg.get("headless", False),
            "user_data_dir": str(user_data_dir),
            "no_viewport": bcfg.get("viewport") is None,
            "slow_mo": bcfg.get("slow_mo", 50),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }

        proxy_url = self._config.proxy_url
        if proxy_url:
            # Playwright wants credentials as separate keys, NOT inline in the
            # server URL, or auth silently fails.
            import urllib.parse
            parsed = urllib.parse.urlparse(proxy_url)
            server = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port:
                server += f":{parsed.port}"
            proxy_cfg = {"server": server}
            if parsed.username:
                proxy_cfg["username"] = urllib.parse.unquote(parsed.username)
            if parsed.password:
                proxy_cfg["password"] = urllib.parse.unquote(parsed.password)
            launch_kwargs["proxy"] = proxy_cfg
            log.info("browser.proxy_configured", server=server, auth=bool(parsed.username))

        viewport = bcfg.get("viewport")
        if viewport:
            launch_kwargs["no_viewport"] = False
            launch_kwargs["viewport"] = viewport

        log.info(
            "browser.launching",
            channel=launch_kwargs["channel"],
            headless=launch_kwargs["headless"],
        )

        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_kwargs
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

        log.info("browser.ready")
        return self._page

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
        # Always stop playwright even if context.close() raises, or the Chrome
        # process + node driver leak.
        try:
            if self._context:
                await self._context.close()
        except Exception as e:
            log.warning("browser.context_close_error", error=str(e)[:100])
        finally:
            self._context = None
            self._page = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    log.warning("browser.playwright_stop_error", error=str(e)[:100])
                self._playwright = None
            log.info("browser.closed")
