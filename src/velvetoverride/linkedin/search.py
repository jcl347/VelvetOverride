"""LinkedIn job search — navigate, filter, and scrape job listings."""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

from velvetoverride.browser.stealth import human_scroll, random_delay
from velvetoverride.tracking.models import JobListing
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"

# LinkedIn URL parameter mappings
EXPERIENCE_LEVEL_MAP = {
    "internship": "1",
    "entry_level": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}

DATE_POSTED_MAP = {
    "any": "",
    "past_month": "r2592000",
    "past_week": "r604800",
    "past_24h": "r86400",
}

REMOTE_MAP = {
    "on_site": "1",
    "remote": "2",
    "hybrid": "3",
}

JOB_TYPE_MAP = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
}


def build_search_url(config: Config) -> str:
    """Build a LinkedIn jobs search URL from config filters."""
    search = config.search
    params: dict[str, str] = {
        "keywords": " ".join(search.get("keywords", [])),
        "location": ", ".join(search.get("locations", [])),
    }

    if search.get("easy_apply_only", True):
        params["f_AL"] = "true"

    # Experience levels
    levels = search.get("experience_levels", [])
    if levels:
        codes = [EXPERIENCE_LEVEL_MAP[lv] for lv in levels if lv in EXPERIENCE_LEVEL_MAP]
        if codes:
            params["f_E"] = ",".join(codes)

    # Date posted
    date_posted = search.get("date_posted", "")
    if date_posted and date_posted in DATE_POSTED_MAP and DATE_POSTED_MAP[date_posted]:
        params["f_TPR"] = DATE_POSTED_MAP[date_posted]

    # Remote
    remotes = search.get("remote", [])
    if remotes:
        codes = [REMOTE_MAP[r] for r in remotes if r in REMOTE_MAP]
        if codes:
            params["f_WT"] = ",".join(codes)

    # Job types
    job_types = search.get("job_types", [])
    if job_types:
        codes = [JOB_TYPE_MAP[jt] for jt in job_types if jt in JOB_TYPE_MAP]
        if codes:
            params["f_JT"] = ",".join(codes)

    return JOBS_SEARCH_URL + "?" + urllib.parse.urlencode(params)


async def scrape_job_listings(page: Page, config: Config, max_pages: int = 3) -> list[JobListing]:
    """Navigate to job search results and scrape listings.

    Returns a list of JobListing objects.
    """
    url = build_search_url(config)
    log.info("search.navigating", url=url)
    await page.goto(url, wait_until="domcontentloaded")
    await random_delay(2.0, 4.0)

    all_listings: list[JobListing] = []
    blacklist_companies = {c.lower() for c in config.search.get("blacklist_companies", [])}
    blacklist_keywords = [kw.lower() for kw in config.search.get("blacklist_keywords", [])]

    for page_num in range(max_pages):
        log.info("search.scraping_page", page=page_num + 1)

        # Scroll through the job list to load all results
        job_list = page.locator(".jobs-search-results-list")
        for _ in range(5):
            await human_scroll(page, "down", 300)
            await random_delay(0.5, 1.5)

        # Find all job cards
        cards = page.locator(".job-card-container, .jobs-search-results__list-item")
        count = await cards.count()
        log.info("search.found_cards", count=count)

        for i in range(count):
            card = cards.nth(i)
            try:
                listing = await _parse_job_card(card, page)
                if listing is None:
                    continue

                # Apply blacklist filters
                if listing.company.lower() in blacklist_companies:
                    log.debug("search.blacklisted_company", company=listing.company)
                    continue

                if any(kw in listing.description.lower() for kw in blacklist_keywords):
                    log.debug("search.blacklisted_keyword", title=listing.title)
                    continue

                all_listings.append(listing)
            except Exception as e:
                log.warning("search.parse_error", error=str(e), index=i)

        # Try to go to next page
        next_button = page.locator('button[aria-label="Next"]')
        if await next_button.count() > 0 and await next_button.is_enabled():
            await next_button.click()
            await random_delay(2.0, 5.0)
        else:
            break

    log.info("search.complete", total_listings=len(all_listings))
    return all_listings


async def _parse_job_card(card, page) -> JobListing | None:
    """Extract job details from a single job card element."""
    try:
        # Click the card to load job details in the side panel
        await card.click()
        await random_delay(1.0, 2.5)

        # Extract from the card itself
        title_el = card.locator(".job-card-list__title, .job-card-container__link")
        title = (await title_el.text_content() or "").strip()

        company_el = card.locator(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle")
        company = (await company_el.text_content() or "").strip()

        location_el = card.locator(".job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption")
        location = (await location_el.first.text_content() or "").strip() if await location_el.count() > 0 else ""

        # Get the job URL from the link
        link = card.locator("a[href*='/jobs/view/']")
        href = await link.get_attribute("href") if await link.count() > 0 else ""
        job_url = href.split("?")[0] if href else ""
        if job_url and not job_url.startswith("http"):
            job_url = "https://www.linkedin.com" + job_url

        # Extract job description from the detail panel
        description = ""
        desc_panel = page.locator(".jobs-description__content, .jobs-description-content")
        if await desc_panel.count() > 0:
            description = (await desc_panel.text_content() or "").strip()

        if not title or not company:
            return None

        return JobListing(
            url=job_url,
            title=title,
            company=company,
            location=location,
            description=description,
        )
    except Exception as e:
        log.debug("search.card_parse_failed", error=str(e))
        return None


async def get_job_description(page: Page, job_url: str) -> str:
    """Navigate to a specific job listing and extract the full description."""
    await page.goto(job_url, wait_until="domcontentloaded")
    await random_delay(1.5, 3.0)

    # Click "Show more" if the description is truncated
    show_more = page.locator('button[aria-label*="Show more"]')
    if await show_more.count() > 0:
        await show_more.click()
        await random_delay(0.5, 1.0)

    desc_el = page.locator(".jobs-description__content, .jobs-description-content")
    if await desc_el.count() > 0:
        return (await desc_el.text_content() or "").strip()

    return ""
