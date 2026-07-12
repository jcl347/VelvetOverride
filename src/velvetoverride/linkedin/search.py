"""LinkedIn job search — navigate, filter, and scrape job listings."""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

_DUP_RE = re.compile(r"^(.+?)\s*\1$")


def _clean_job_text(text: str) -> str:
    """Normalize scraped card text.

    LinkedIn cards often repeat the title (visible + screen-reader spans) and
    append badges like "with verification". Collapse whitespace, drop those
    badges, and de-duplicate an exactly-doubled string so we never store a
    garbled/duplicated job title.
    """
    if not text:
        return ""
    s = " ".join(text.split())
    for badge in (" with verification", " with Verification"):
        s = s.replace(badge, "")
    s = s.strip()
    m = _DUP_RE.match(s)
    if m and m.group(1).strip():
        s = m.group(1).strip()
    return s

from velvetoverride.browser.stealth import human_scroll, random_delay
from velvetoverride.tracking.models import JobListing
from velvetoverride.utils.logging import get_logger
from velvetoverride.utils.urls import extract_job_id, normalize_job_url

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


def build_search_url(config: Config, keyword: str | None = None,
                     location: str | None = None) -> str:
    """Build a LinkedIn jobs search URL from config filters.

    If ``keyword``/``location`` are given, those single values are used;
    otherwise falls back to joining the configured lists. LinkedIn's `location`
    param is a single place — multiple locations must be searched separately
    (the scraper loops over them), never comma-joined into one param.
    """
    search = config.search
    query = keyword if keyword is not None else " ".join(search.get("keywords", []))
    if location is not None:
        loc = location
    else:
        locs = search.get("locations", [])
        loc = locs[0] if locs else ""
    params: dict[str, str] = {
        "keywords": query,
        "location": loc,
    }

    if search.get("easy_apply_only", True):
        params["f_AL"] = "true"

    # Sort order: "date" (newest first) or "relevance" (LinkedIn default)
    if search.get("sort_by", "").lower() == "date":
        params["sortBy"] = "DD"

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
    """Search each configured keyword separately, then merge + de-duplicate.

    De-duplication across keywords is by canonical LinkedIn job ID (falling back
    to normalized URL), so the same posting surfaced by two role searches is only
    kept once.
    """
    keywords = config.search.get("keywords", [])
    if not keywords:
        keywords = [""]  # single empty query = location-only search

    max_pages = int(config.search.get("max_pages", max_pages))

    # Each location is a separate LinkedIn search (the `location` param is a
    # single place). Cross keywords x locations, merging + de-duping by job id.
    locations = config.search.get("locations", []) or [""]

    all_listings: list[JobListing] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for location in locations:
        for keyword in keywords:
            url = build_search_url(config, keyword=keyword, location=location)
            log.info("search.navigating", keyword=keyword, location=location, url=url)
            listings = await _scrape_one_search(page, config, url, max_pages)
            added = 0
            for listing in listings:
                if listing.job_id and listing.job_id in seen_ids:
                    continue
                if listing.url and listing.url in seen_urls:
                    continue
                if listing.job_id:
                    seen_ids.add(listing.job_id)
                if listing.url:
                    seen_urls.add(listing.url)
                all_listings.append(listing)
                added += 1
            log.info("search.keyword_done", keyword=keyword, location=location,
                     found=len(listings), new=added)

    log.info("search.all_keywords_complete", total_unique=len(all_listings))
    return all_listings


# Candidate card selectors, most-specific first. Only ONE is used per page so a
# job isn't counted twice (an <li> wrapper and its inner div both match).
_CARD_SELECTORS = (
    "li[data-occludable-job-id]",
    ".jobs-search-results__list-item",
    ".job-card-container",
)
CARD_SELECTOR = _CARD_SELECTORS[0]

# Steps the job list's own scroll container down one viewport at a time.
# LinkedIn virtualizes the list, so jumping straight to the bottom can skip
# rows; incremental scrolling forces each batch to render.
_SCROLL_LIST_JS = r"""
(sel) => {
  const cards = document.querySelectorAll(sel);
  if (!cards.length) {
    window.scrollBy(0, window.innerHeight * 0.9);
    return {count: 0, atBottom: (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 10)};
  }
  // Walk up from the last card to the nearest scrollable ancestor
  let el = cards[cards.length - 1].parentElement;
  while (el && el.scrollHeight <= el.clientHeight + 5) el = el.parentElement;

  let atBottom = false;
  if (el && el !== document.body && el !== document.documentElement) {
    el.scrollTop = Math.min(el.scrollTop + el.clientHeight * 0.9, el.scrollHeight);
    atBottom = (el.scrollTop + el.clientHeight) >= (el.scrollHeight - 10);
  } else {
    window.scrollBy(0, window.innerHeight * 0.9);
    atBottom = (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 10);
  }
  // Nudge the last card into view so virtualized rows render
  try { cards[cards.length - 1].scrollIntoView({block: 'end'}); } catch (e) {}
  return {count: document.querySelectorAll(sel).length, atBottom};
}
"""


# Resets the job-list scroll container back to the top (after pagination).
_SCROLL_TOP_JS = r"""
(sel) => {
  const cards = document.querySelectorAll(sel);
  if (!cards.length) { window.scrollTo(0, 0); return; }
  let el = cards[0].parentElement;
  while (el && el.scrollHeight <= el.clientHeight + 5) el = el.parentElement;
  if (el && el !== document.body && el !== document.documentElement) el.scrollTop = 0;
  else window.scrollTo(0, 0);
}
"""


async def _pick_card_selector(page: Page) -> str:
    """Choose the one selector that yields exactly one element per job card."""
    for sel in _CARD_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return sel
        except Exception:
            continue
    return _CARD_SELECTORS[-1]


async def _load_all_cards(page: Page, selector: str, max_scrolls: int = 30) -> int:
    """Scroll the job-list container to the bottom, loading every card."""
    last = -1
    count = 0
    stable = 0
    for _ in range(max_scrolls):
        try:
            res = await page.evaluate(_SCROLL_LIST_JS, selector)
        except Exception as e:
            log.debug("search.scroll_error", error=str(e)[:80])
            break
        count = res.get("count", 0)
        at_bottom = res.get("atBottom", False)
        await random_delay(0.4, 0.9)

        if count == last:
            stable += 1
        else:
            stable = 0
        last = count

        # Done only when we've reached the bottom AND no new cards appeared
        if at_bottom and stable >= 2:
            break

    log.info("search.cards_loaded", count=count, selector=selector)
    return count


async def _scrape_one_search(
    page: Page, config: Config, url: str, max_pages: int
) -> list[JobListing]:
    """Scrape all pages of a single search-results URL."""
    await page.goto(url, wait_until="domcontentloaded")
    await random_delay(2.0, 4.0)

    all_listings: list[JobListing] = []
    blacklist_companies = {c.lower() for c in config.search.get("blacklist_companies", [])}
    blacklist_keywords = [kw.lower() for kw in config.search.get("blacklist_keywords", [])]

    for page_num in range(max_pages):
        log.info("search.scraping_page", page=page_num + 1)

        # Scroll the (virtualized) job-list container to the bottom so every
        # card on this page is rendered before we read them.
        selector = await _pick_card_selector(page)
        await _load_all_cards(page, selector)

        cards = page.locator(selector)
        count = await cards.count()
        log.info("search.found_cards", count=count, page=page_num + 1)

        dropped = 0
        for i in range(count):
            card = cards.nth(i)
            try:
                listing = await _parse_job_card(card, page)
                if listing is None:
                    dropped += 1
                    continue

                # Company blacklist (JD-keyword blacklist is applied later, once
                # the full description has been fetched)
                if listing.company.lower() in blacklist_companies:
                    log.debug("search.blacklisted_company", company=listing.company)
                    continue

                all_listings.append(listing)
            except Exception as e:
                log.warning("search.parse_error", error=str(e), index=i)
        if dropped:
            log.debug("search.cards_dropped", dropped=dropped, of=count, page=page_num + 1)

        # Try to go to the next results page (LinkedIn has several pager shapes)
        next_button = page.locator(
            'button[aria-label="View next page"], '
            '.jobs-search-pagination__button--next, '
            'button[aria-label="Next"], '
            f'button[aria-label="Page {page_num + 2}"]'
        )
        try:
            if await next_button.count() > 0 and await next_button.first.is_enabled():
                # Remember the first card so we can confirm the page advanced
                first_id_before = ""
                try:
                    first_id_before = await cards.first.get_attribute("data-occludable-job-id") or ""
                except Exception:
                    pass
                await next_button.first.scroll_into_view_if_needed(timeout=4000)
                await next_button.first.click()
                await random_delay(2.0, 4.0)
                try:
                    await page.evaluate(_SCROLL_TOP_JS, selector)
                except Exception:
                    pass
                await random_delay(1.0, 2.0)
                # Verify the results actually changed; a no-op click means we're
                # at the last page (or the pager is stale) — stop rather than
                # re-scrape the same page.
                try:
                    first_id_after = await page.locator(selector).first.get_attribute(
                        "data-occludable-job-id") or ""
                    if first_id_before and first_id_after and first_id_before == first_id_after:
                        log.info("search.page_did_not_advance", page=page_num + 1)
                        break
                except Exception:
                    pass
            else:
                log.info("search.no_more_pages", page=page_num + 1)
                break
        except Exception as e:
            log.debug("search.pagination_end", error=str(e)[:80])
            break

    log.info("search.complete", total_listings=len(all_listings))
    return all_listings


async def _parse_job_card(card, page) -> JobListing | None:
    """Extract job metadata from a single job card — fast, no click.

    We deliberately do NOT click the card or read the description panel here:
    the full job description is fetched per-job (via ``get_job_description``)
    right before tailoring/applying. Skipping the click makes list scraping
    ~10x faster so we can cover many pages across many keywords.
    """
    try:
        # Extract from the card itself (cleaned — LinkedIn duplicates the title
        # text and appends "with verification" badges)
        title_el = card.locator(".job-card-list__title, .job-card-container__link, a.job-card-list__title--link")
        title = _clean_job_text(await title_el.first.text_content() or "") if await title_el.count() > 0 else ""

        company_el = card.locator(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle")
        company = _clean_job_text(await company_el.first.text_content() or "") if await company_el.count() > 0 else ""

        location_el = card.locator(".job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption")
        location = (await location_el.first.text_content() or "").strip() if await location_el.count() > 0 else ""

        # Get the job URL from the link
        link = card.locator("a[href*='/jobs/view/']")
        href = await link.first.get_attribute("href") if await link.count() > 0 else ""
        raw_url = href if href else ""
        if raw_url and not raw_url.startswith("http"):
            raw_url = "https://www.linkedin.com" + raw_url

        # Fall back to the card's job-id attributes if the link had none
        job_id = extract_job_id(raw_url) or ""
        if not job_id:
            for attr in ("data-job-id", "data-occludable-job-id"):
                data_id = await card.get_attribute(attr)
                if data_id and data_id.isdigit():
                    job_id = data_id
                    break

        # Canonicalize the URL for stable de-duplication
        job_url = normalize_job_url(raw_url) if raw_url else (
            f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else ""
        )

        # Description is fetched later, per job, right before applying.
        description = ""

        # A card with no id and no url is unusable
        if not title or not company or not (job_id or job_url):
            return None

        return JobListing(
            url=job_url,
            job_id=job_id,
            title=title,
            company=company,
            location=location,
            description=description,
        )
    except Exception as e:
        log.debug("search.card_parse_failed", error=str(e))
        return None


DESC_SELECTORS = (
    "#job-details, "
    ".jobs-description__content, .jobs-description-content, "
    ".jobs-box__html-content, "
    "article.jobs-description__container, "
    ".jobs-description"
)


# JS extractor: finds the "About the job" section (or known containers) and
# returns its text. Selector-independent, so it survives LinkedIn DOM changes.
_JD_EXTRACT_JS = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+\n/g, '\n').trim();
  // 1) Known description containers
  const sels = ['#job-details', '.jobs-description__content', '.jobs-box__html-content',
                'article.jobs-description__container', '.jobs-description'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && (el.innerText || '').trim().length > 120) return clean(el.innerText);
  }
  // 2) "About the job" heading -> nearest content container
  const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,span,div'));
  const h = heads.find(e => /about the job/i.test((e.textContent || '').trim())
                             && (e.textContent || '').trim().length < 40);
  if (h) {
    let c = h.parentElement;
    for (let i = 0; i < 4 && c; i++) {
      if ((c.innerText || '').trim().length > 200) return clean(c.innerText);
      c = c.parentElement;
    }
  }
  // 3) Largest text block on the job pane
  let best = '';
  for (const el of document.querySelectorAll('div, section, article')) {
    const t = (el.innerText || '').trim();
    if (t.length > best.length && t.length < 20000) best = t;
  }
  return clean(best);
}
"""


# Clicks any "see more" / "show more" toggle inside the job description area.
# Done in JS so it works regardless of LinkedIn's current button markup.
_EXPAND_JD_JS = r"""
() => {
  const isMore = (t) => /^(see|show|read)\s*more/i.test((t || '').trim());
  let clicked = 0;
  const btns = document.querySelectorAll('button, a[role="button"], span[role="button"]');
  for (const b of btns) {
    const label = (b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '');
    if (isMore(b.innerText) || /see more|show more/i.test(label)) {
      // Only click toggles near the description, not unrelated page buttons
      if (b.closest('.jobs-description, .jobs-box, #job-details, .jobs-details, main')) {
        try { b.click(); clicked++; } catch (e) {}
      }
    }
  }
  return clicked;
}
"""


async def _expand_job_description(page: Page) -> None:
    """Expand the truncated 'About the job' text so we read the full JD."""
    # 1) Playwright locators (most reliable when the button is standard)
    for sel in (
        'button.jobs-description__footer-button',
        'button[aria-label*="Show more"]',
        'button[aria-label*="see more"]',
        'button:has-text("See more")',
        'button:has-text("Show more")',
    ):
        try:
            btn = page.locator(sel)
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click(timeout=3000)
                await random_delay(0.3, 0.8)
                break
        except Exception:
            continue

    # 2) JS sweep as a fallback for non-standard markup
    try:
        clicked = await page.evaluate(_EXPAND_JD_JS)
        if clicked:
            log.debug("search.jd_expanded", buttons=clicked)
            await random_delay(0.3, 0.7)
    except Exception:
        pass


async def get_job_description(page: Page, job_url: str) -> str:
    """Navigate to a job listing and extract the full description robustly."""
    await page.goto(job_url, wait_until="domcontentloaded")
    await random_delay(1.5, 3.0)

    # Expand "See more" / "Show more" under "About the job" so the FULL
    # description is in the DOM (LinkedIn truncates it by default).
    await _expand_job_description(page)

    text = ""
    try:
        text = (await page.evaluate(_JD_EXTRACT_JS) or "").strip()
    except Exception as e:
        log.debug("search.jd_eval_error", error=str(e)[:80])

    if text and len(text) > 80:
        log.info("search.jd_fetched", chars=len(text), url=job_url)
        return text

    log.warning("search.jd_empty", url=job_url)
    return ""
