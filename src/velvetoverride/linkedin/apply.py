"""Application flow controller — walks through Easy Apply multi-step forms."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from velvetoverride.browser.stealth import random_delay
from velvetoverride.linkedin.fields import FormField, detect_form_fields, _get_field_label
from velvetoverride.tracking.models import (
    ApplicationRecord,
    ApplicationStatus,
    FieldType,
    QuestionRecord,
)
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Page
    from velvetoverride.agent.field_solver import FieldSolver
    from velvetoverride.tracking.database import TrackingDB
    from velvetoverride.tracking.models import JobListing
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

# Easy Apply navigation. Kept broad — "No Next/Submit button found" was the most
# common failure, caused by LinkedIn's varying button labels/aria-labels.
SUBMIT_SELECTOR = (
    'button[aria-label*="Submit application"], '
    'button[aria-label*="Submit"], '
    'button:has-text("Submit application"), '
    'button:has-text("Submit"), '
    'button:has-text("Send application"), '
    'button[data-control-name="submit_unify"]'
)

_RESUME_EXTS = {".pdf", ".doc", ".docx"}

# Degree LEVEL -> phrasings that appear in ATS dropdown options. Ordered
# most-specific first (doctorate before master) so "graduate" collisions don't
# misfire. Option synonyms deliberately avoid bare "graduate" (matches
# "undergraduate"). Used to map a degree answer like "Master's Degree" onto an
# option worded differently, e.g. "Master of Science".
_DEGREE_LEVELS = [
    ("doctor", ("doctor", "phd", "ph.d", "ph. d", "doctorate", "d.phil", "dphil")),
    ("master", ("master", "msc", "m.s", "m.sc", "m.eng", "mba", "postgraduate", "post-graduate")),
    ("bachelor", ("bachelor", "undergraduate", "bsc", "b.s", "b.sc", "b.eng", "b.a.")),
    ("associate", ("associate", "a.a.", "a.s.")),
    ("high school", ("high school", "secondary school", "ged", "diploma", "a-level", "gcse")),
]


def _match_degree_option(value: str, options: list[str]) -> str | None:
    """Best degree-dropdown option for a degree answer, matched on the degree
    LEVEL keyword (options are often worded differently: "Master of Science" for
    "Master's Degree"). Returns the option text, or None if the answer is not a
    recognizable degree level or no option mentions that level.
    """
    vl = (value or "").lower()
    level_syns: tuple[str, ...] | None = None
    for level, syns in _DEGREE_LEVELS:
        if level in vl or any(s in vl for s in syns):
            level_syns = (level,) + syns
            break
    if not level_syns:
        return None
    matches = [opt for opt in options if opt and any(s in opt.lower() for s in level_syns)]
    if not matches:
        return None
    # Prefer the shortest match (e.g. "Master's" over "Master's or higher, plus
    # 5 years") so we pick the plain level, not a compound requirement.
    return min(matches, key=len)


_CONSENT_LABEL_KEYS = (
    "consent", "authoriz", "i agree", "acknowledg", "certify", "terms",
    "i confirm", "declaration", "i understand", "background check",
)
_AFFIRM_OPTION_WORDS = (
    "i agree", "agree", "i consent", "consent", "i accept", "accept",
    "acknowledge", "i certify", "certify", "confirm", "i do", "yes",
)


def _is_negative_option(opt: str) -> bool:
    """True if a dropdown option expresses REFUSAL/negation of agreement."""
    ol = (opt or "").strip().lower()
    return ol.startswith((
        "no", "i do not", "i don't", "i decline", "decline", "disagree",
        "i disagree", "i refuse", "not ",
    ))


def _match_consent_option(value: str, options: list[str], label: str) -> str | None:
    """For a consent / authorization / agreement dropdown answered affirmatively
    (value "Yes"), pick the option that expresses agreement. ATS forms word these
    "I agree" / "I acknowledge", not "Yes", so an exact match misses. Never picks
    a refusal option, and returns None unless the field is clearly a consent
    question answered affirmatively.
    """
    vl = (value or "").lower()
    if not any(a in vl for a in ("yes", "true", "agree", "consent", "accept",
                                 "acknowledge", "certify", "confirm")):
        return None
    ll = (label or "").lower()
    if not any(k in ll for k in _CONSENT_LABEL_KEYS):
        return None
    affirmative = [
        opt for opt in options
        if opt and opt.strip() and not _is_negative_option(opt)
        and any(a in opt.lower() for a in _AFFIRM_OPTION_WORDS)
    ]
    if not affirmative:
        return None
    return min(affirmative, key=len)


def _match_clearance_option(value: str, options: list[str], label: str) -> str | None:
    """A "what LEVEL of security clearance do you have?" dropdown answered "No"
    (i.e. no clearance) must select the "None" option, not fail — the answer is a
    level, not Yes/No. Returns the none-type option or None (never a real level).
    """
    if "clearance" not in (label or "").lower():
        return None
    if (value or "").strip().lower() not in ("no", "none", "n/a", "na", "false", ""):
        return None
    for opt in options:
        ol = (opt or "").lower()
        if any(k in ol for k in (
            "none", "no clearance", "no active", "not applicable", "n/a",
            "do not have", "no security clearance", "unclassified",
        )):
            return opt
    return None


def _match_year_range_option(value: str, options: list[str]) -> str | None:
    """For a "how many years ...?" dropdown whose options are year RANGES
    ("0-1 years", "3-5 years", "5-7", "7+ years", "less than 1"), pick the option
    whose range contains the numeric answer. Returns the option, or None if the
    value isn't a plain number or no range matches. Prefers a bounded range over
    an open-ended "N+" so a boundary value lands in the tighter bucket."""
    m = re.fullmatch(r"\s*(\d+)\s*", str(value or ""))
    if not m:
        return None
    n = int(m.group(1))
    plus_match = None
    for opt in options:
        ol = (opt or "").lower()
        if not re.search(r"\d", ol):
            continue
        rng = re.search(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)", ol)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo <= n <= hi:
                return opt
            continue
        less = re.search(r"(?:less than|under|fewer than|below|<)\s*(\d+)", ol)
        if less:
            if n < int(less.group(1)):
                return opt
            continue
        plus = re.search(r"(\d+)\s*\+", ol) or re.search(
            r"(?:more than|at least|over|>=?)\s*(\d+)", ol) or re.search(
            r"(\d+)\s*(?:or more|or greater|and above|and up)", ol)
        if plus:
            if n >= int(plus.group(1)) and plus_match is None:
                plus_match = opt  # remember but prefer a bounded range first
            continue
        exact = re.search(r"\b(\d+)\b", ol)
        if exact and n == int(exact.group(1)):
            return opt
    return plus_match

NEXT_SELECTOR = (
    'button[aria-label*="Continue to next step"], '
    'button[aria-label*="Review your application"], '
    'button[aria-label*="Continue"], '
    'button[aria-label*="Next"], '
    'button[aria-label*="Review"], '
    'button:has-text("Next"), '
    'button:has-text("Continue"), '
    'button:has-text("Review"), '
    'button:has-text("Save and continue"), '
    'button[data-control-name="continue_unify"]'
)


class ApplicationFlow:
    """Manages the end-to-end Easy Apply application process for a single job."""

    def __init__(
        self,
        page: Page,
        config: Config,
        field_solver: FieldSolver,
        db: TrackingDB,
    ) -> None:
        self._page = page
        self._config = config
        self._solver = field_solver
        self._db = db
        self._run_id: int | None = None  # set by the orchestrator for tracking
        self._questions: list[QuestionRecord] = []
        self._screenshot_count = 0
        self._screenshot_paths: list[str] = []

    async def apply_to_job(
        self,
        listing: JobListing,
        resume_path: str | None = None,
    ) -> ApplicationRecord:
        """Execute the full Easy Apply flow for a single job listing.

        Returns an ApplicationRecord with the outcome.
        """
        dry_run = self._config.bot.get("dry_run", True)
        self._questions = []
        self._screenshot_count = 0
        self._screenshot_paths = []
        self._resume_path = resume_path

        log.info(
            "apply.starting",
            title=listing.title,
            company=listing.company,
            dry_run=dry_run,
        )

        try:
            # Navigate to job and click Easy Apply
            await self._page.goto(listing.url, wait_until="domcontentloaded")
            await random_delay(1.5, 3.0)

            # Click the Easy Apply button
            easy_apply_btn = self._page.locator(
                'button.jobs-apply-button, '
                'button:has-text("Easy Apply"), '
                'button[aria-label*="Easy Apply"]'
            )
            # Distinguish Easy Apply from external "Apply" (opens company site)
            is_easy = await self._is_easy_apply()
            if await easy_apply_btn.count() == 0 or not is_easy:
                if self._external_cfg()["enabled"]:
                    log.info("apply.external_flow", url=listing.url)
                    return await self._apply_external(listing, resume_path)
                log.warning("apply.no_easy_apply_button", url=listing.url)
                return self._make_record(listing, ApplicationStatus.SKIPPED, "No Easy Apply button found")

            # Prefer the specific apply button by its accessible name so we never
            # click an unrelated "Easy Apply" chip. LinkedIn now uses obfuscated
            # CSS classes, so match on aria-label/role, not class.
            apply_btn = self._page.locator(
                'button[aria-label*="Easy Apply" i], button.jobs-apply-button'
            )
            if await apply_btn.count() == 0:
                apply_btn = easy_apply_btn

            # Clicking once often no-ops: the page fires domcontentloaded before
            # React attaches the button handler (hydration race). Retry the click
            # and WAIT for the modal each time until it actually opens.
            modal_selector = (
                'dialog:has(header#dialog-header), '
                'dialog[open], dialog, '
                'div[data-test-modal-id="easy-apply-modal"], '
                '.jobs-easy-apply-modal, '
                'div.artdeco-modal[role="dialog"], '
                'div[role="dialog"]'
            )
            modal_opened = False
            for attempt in range(4):
                try:
                    await apply_btn.first.scroll_into_view_if_needed(timeout=3000)
                    await apply_btn.first.click(timeout=5000)
                except Exception as e:
                    log.warning("apply.easy_apply_click_failed",
                                attempt=attempt + 1, error=str(e)[:80])
                try:
                    await self._page.wait_for_selector(
                        modal_selector, state="visible", timeout=4000)
                    modal_opened = True
                    break
                except Exception:
                    await random_delay(1.0, 2.0)
            if not modal_opened:
                log.warning("apply.modal_wait_timeout", company=listing.company,
                            title=listing.title, attempts=4)
            await random_delay(1.0, 2.0)

            # Walk through multi-step form
            max_steps = int(self._config.bot.get("max_form_steps", 20))
            max_stuck = int(self._config.bot.get("max_stuck_retries", 4))
            last_signature = None
            stuck_count = 0
            last_errors: list[str] = []
            self._resume_uploaded_this_app = False  # reset per application
            self._checkbox_recovered: set[str] = set()  # steps we've tried the
            # consent-checkbox recovery on (once each), reset per application

            for step in range(max_steps):
                log.info("apply.form_step", step=step + 1, title=listing.title)

                # Capture screenshot if enabled
                await self._maybe_screenshot(listing)

                # Detect and fill all fields on this step — scoped to the Easy
                # Apply modal so we don't touch page chrome ("Set alert" etc.)
                modal = await self._easy_apply_modal()
                if modal is None:
                    # The modal can briefly vanish during a slow step transition,
                    # or close back to the job page (seen on Tiger Analytics /
                    # ConsumerAffairs). Before giving up: (1) re-poll a few times
                    # in case it is still re-rendering, then (2) if the Easy Apply
                    # button is visible again, RE-OPEN it once and re-poll. Only
                    # abort if it truly stays gone — never scan the full page.
                    modal = await self._await_modal(retries=4, wait=1.0)
                    if modal is None and await self._reopen_easy_apply():
                        modal = await self._await_modal(retries=4, wait=1.0)
                    if modal is not None:
                        log.info("apply.modal_recovered", step=step + 1,
                                 company=listing.company)
                if modal is None:
                    # SAFETY: detect_form_fields treats scope=None as "scan the
                    # whole page". On LinkedIn that means the bot would start
                    # clicking the user's OWN account UI — the messaging drawer's
                    # conversation checkboxes, etc. Never do that: abort instead.
                    if await self._submission_confirmed():
                        log.info("apply.submitted", company=listing.company, title=listing.title)
                        return self._make_record(listing, ApplicationStatus.APPLIED)
                    diag = await self._dialog_diagnostics()
                    try:
                        shot = str(Path("data") / "modal_missing_debug.png")
                        await self._page.screenshot(path=shot, full_page=False)
                        log.error("apply.modal_missing_screenshot", path=shot)
                    except Exception:
                        pass
                    log.error("apply.modal_missing", step=step + 1, company=listing.company,
                              title=listing.title, dialogs=diag,
                              msg="Easy Apply modal not found — aborting rather than scanning the page")
                    await self._dismiss_modal()
                    return self._make_record(
                        listing, ApplicationStatus.FAILED,
                        f"Easy Apply modal not found at step {step + 1} — aborted "
                        "(refused to scan the full page)",
                    )
                fields = await detect_form_fields(self._page, scope=modal)
                if not fields:
                    # The modal may still be rendering its content (a 0-field step
                    # early on usually means it hasn't loaded yet). Wait once and
                    # re-detect before treating the step as empty — a premature
                    # advance is what drops us out of a slow-loading modal.
                    await random_delay(1.3, 2.2)
                    modal = await self._easy_apply_modal() or modal
                    fields = await detect_form_fields(self._page, scope=modal)
                await self._diag_dump_structures(modal)
                # If this is a résumé step with an "Upload resume" BUTTON (no file
                # input in the DOM until clicked), push our tailored PDF via the
                # file chooser so it's used instead of a stored résumé.
                if not any(f.field_type == FieldType.FILE_UPLOAD for f in fields):
                    await self._ensure_resume_uploaded(modal)
                await self._fill_fields(fields, listing)
                await random_delay(0.8, 1.8)

                # Pull back any inline (red) validation errors and try to fix them
                errors = await self._fix_validation_errors(fields, listing, modal)
                if errors:
                    last_errors = errors
                    log.warning("apply.form_errors", count=len(errors), first=errors[0][:100])
                    # Give the fix a chance to apply, then re-read
                    await random_delay(0.5, 1.0)
                    errors = await self._fix_validation_errors(fields, listing, modal)

                # Capture the step AFTER answering, so the actual selections
                # (radios ticked, dropdowns set) are visible for review — only
                # when this step had fields worth showing.
                if fields:
                    await self._maybe_screenshot(listing, suffix="filled")

                await random_delay(0.6, 1.2)

                # Detect a form that isn't advancing. The signature includes a
                # per-step marker (progress % + section heading) so that ADVANCING
                # to a genuinely different step resets the stuck counter — even
                # when several steps in a row have no fillable fields (cover-letter
                # upload, work-experience / education cards). Without it, those
                # empty steps all share the signature ((), (), False) and the form
                # falsely "gives up" while it is actually progressing.
                signature = (
                    await self._step_marker(modal),
                    tuple(f.label for f in fields),
                    tuple(errors),
                    await self._is_review_step(),
                )
                if signature == last_signature:
                    stuck_count += 1
                    log.warning(
                        "apply.form_stuck", step=step + 1, attempts=stuck_count,
                        errors=errors[:2] if errors else [],
                    )
                    # One-time recovery: a stuck step is frequently blocked by a
                    # REQUIRED consent/acknowledgement checkbox that came through
                    # unlabeled (e.g. rendered inside a shadow DOM, so the label
                    # is unreadable and it stays unticked). Tick the non-marketing
                    # checkbox(es) and retry before giving up. Only fires on an
                    # already-stuck form, so it never touches optional opt-ins on
                    # forms that submit fine.
                    marker = signature[0]
                    if marker not in self._checkbox_recovered:
                        self._checkbox_recovered.add(marker)
                        ticked = await self._tick_unchecked_checkboxes(modal)
                        if ticked:
                            log.info("apply.stuck_recovery_checkbox",
                                     ticked=ticked[:4], step=step + 1)
                            stuck_count = 0
                            last_signature = None
                            await random_delay(0.4, 1.0)
                            continue
                    if stuck_count >= max_stuck:
                        reason = (
                            f"Stuck on form step {step + 1}: {last_errors[0]}"
                            if last_errors else
                            f"Stuck on form step {step + 1} (form not advancing)"
                        )
                        diag = await self._stuck_diagnostics(fields, modal, listing)
                        log.error("apply.form_stuck_giveup", reason=reason, diag=diag)
                        await self._dismiss_modal()
                        return self._make_record(listing, ApplicationStatus.FAILED, reason)
                else:
                    stuck_count = 0
                last_signature = signature

                # Check if this is the final step (Submit button) or Next
                if await self._is_review_step():
                    if dry_run:
                        log.info("apply.dry_run_skip", company=listing.company)
                        await self._dismiss_modal()
                        return self._make_record(listing, ApplicationStatus.DRY_RUN, "Dry run — not submitted")

                    await self._click_submit()
                    await random_delay(2.0, 4.0)
                    # VERIFY the submission actually went through — don't record
                    # APPLIED just because we clicked. If no confirmation appears,
                    # the submit was likely blocked by a validation error.
                    if await self._submission_confirmed():
                        log.info("apply.submitted", company=listing.company, title=listing.title)
                        await self._dismiss_modal()
                        return self._make_record(listing, ApplicationStatus.APPLIED)
                    # Not confirmed — read any error and keep walking the form
                    errs2 = await self._fix_validation_errors(fields, listing, modal)
                    if errs2:
                        last_errors = errs2
                    log.warning("apply.submit_unconfirmed", company=listing.company,
                                errors=errs2[:2] if errs2 else [])
                    # fall through: loop will re-detect and try again / stuck-detect

                elif await self._has_next_button():
                    await self._click_next()
                    await random_delay(1.5, 3.0)
                else:
                    # Selector-based Next/Submit not found. The footer button may
                    # just be lazy/renamed — wait briefly and fall back to the
                    # modal's PRIMARY action button before giving up.
                    await random_delay(1.2, 2.2)
                    kind, btn = await self._primary_action()
                    if kind == "submit" and not dry_run:
                        await self._scroll_and_click(btn)
                        await random_delay(2.0, 4.0)
                        if await self._submission_confirmed():
                            log.info("apply.submitted", company=listing.company, title=listing.title, via="primary")
                            await self._dismiss_modal()
                            return self._make_record(listing, ApplicationStatus.APPLIED)
                        last_errors = await self._fix_validation_errors(fields, listing, modal) or last_errors
                    elif kind == "submit" and dry_run:
                        await self._dismiss_modal()
                        return self._make_record(listing, ApplicationStatus.DRY_RUN, "Dry run — not submitted")
                    elif kind == "next":
                        log.info("apply.next", via="primary", step=step + 1)
                        await self._scroll_and_click(btn)
                        await random_delay(1.5, 3.0)
                    else:
                        # DYNAMIC fallback: resolve the button via ChatGPT from
                        # the modal's actual visible labels (handles late-rendered
                        # / renamed buttons the selectors and class-fallback miss).
                        action, label = await self._resolve_nav_button_llm(
                            listing, allow_submit=not dry_run)
                        if action == "submit" and not dry_run:
                            await random_delay(2.0, 4.0)
                            if await self._submission_confirmed():
                                log.info("apply.submitted", company=listing.company,
                                         title=listing.title, via="dynamic")
                                await self._dismiss_modal()
                                return self._make_record(listing, ApplicationStatus.APPLIED)
                            last_errors = await self._fix_validation_errors(fields, listing, modal) or last_errors
                        elif action == "submit" and dry_run:
                            await self._dismiss_modal()
                            return self._make_record(listing, ApplicationStatus.DRY_RUN, "Dry run — not submitted")
                        elif action == "next":
                            await random_delay(1.5, 3.0)
                        else:
                            # Truly no navigation — even ChatGPT found no button.
                            labels = await self._collect_button_labels(self._page)
                            reason = (
                                f"No navigation available: {last_errors[0]}"
                                if last_errors else "No Next/Submit button found"
                            )
                            log.warning("apply.no_navigation", step=step, reason=reason, buttons=labels[:10])
                            await self._dismiss_modal()
                            return self._make_record(listing, ApplicationStatus.FAILED, reason)

            reason = (
                f"Max form steps exceeded; last error: {last_errors[0]}"
                if last_errors else "Max form steps exceeded"
            )
            log.warning("apply.max_steps_exceeded", title=listing.title)
            await self._dismiss_modal()
            return self._make_record(listing, ApplicationStatus.FAILED, reason)

        except Exception as e:
            log.error("apply.error", error=str(e), title=listing.title)
            await self._dismiss_modal()
            return self._make_record(listing, ApplicationStatus.FAILED, str(e))

    async def _await_modal(self, retries: int = 4, wait: float = 1.0):
        """Poll for the Easy Apply modal a few times with short waits, to ride
        out a transient absence while it re-renders between steps. Returns the
        modal locator or None if it never (re)appears."""
        for _ in range(max(1, retries)):
            m = await self._easy_apply_modal()
            if m is not None:
                return m
            await random_delay(wait, wait + 0.6)
        return None

    async def _reopen_easy_apply(self) -> bool:
        """If the Easy Apply modal closed back to the job page, click the
        (re-)visible Easy Apply button to reopen the application. LinkedIn
        resumes saved progress, so this recovers an unexpectedly-dismissed modal
        rather than losing the application. Returns True if a button was clicked."""
        try:
            btn = self._page.locator(
                'button:has-text("Easy Apply"), '
                'button[aria-label*="Easy Apply" i]'
            )
            if await btn.count() > 0 and await btn.first.is_visible():
                await self._scroll_and_click(btn.first)
                await random_delay(1.2, 2.4)
                log.info("apply.reopened_easy_apply")
                return True
        except Exception as e:
            log.debug("apply.reopen_failed", error=str(e)[:80])
        return False

    async def _easy_apply_modal(self):
        """Return a locator for the Easy Apply modal container, or None.

        LinkedIn now renders Easy Apply as a native <dialog> with a
        <header id="dialog-header"> (obfuscated CSS classes, no role="dialog"
        and no data-test-modal-id). Match that first, then the legacy markup,
        then any dialog that actually contains an application form — so a future
        markup change doesn't blind us, while unrelated dialogs are skipped.
        """
        specific = (
            'dialog:has(header#dialog-header)',
            'dialog:has-text("Apply to")',
            'dialog[open]',
            'div[data-test-modal-id="easy-apply-modal"]',
            '.jobs-easy-apply-modal',
            'div[role="dialog"][aria-label*="Easy Apply" i]',
            'div[role="dialog"].artdeco-modal',
        )
        for sel in specific:
            try:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        # Fallback: a visible dialog that contains an application form. Scoped to
        # form-bearing dialogs so we never grab e.g. a cookie/notification dialog.
        try:
            dlg = self._page.locator(
                'dialog:has(input), dialog:has(select), '
                'div[role="dialog"]:has(input), '
                'div[role="dialog"]:has(select), '
                'div[role="dialog"]:has(button:has-text("Submit application")), '
                'div[role="dialog"]:has(button:has-text("Review")), '
                'div[role="dialog"]:has(button[aria-label*="next" i])'
            )
            if await dlg.count() > 0:
                return dlg.first
        except Exception:
            pass
        return None

    # Marketing / promotional opt-ins we must NOT auto-tick even during recovery.
    _MARKETING_KW = (
        "marketing", "promotional", "promotion", "newsletter", "subscribe",
        "receive offer", "receive updates", "receive communication",
        "receive email", "receive text", "receive sms", "sms updates",
        "opt in to", "opt-in to", "third part", "partner offer",
        "share my information", "share my data", "keep me informed",
    )

    async def _tick_unchecked_checkboxes(self, modal) -> list[str]:
        """Recovery for a stuck form: tick unchecked checkbox(es) that look like a
        required consent/acknowledgement gate. A stuck (non-advancing) form is
        typically blocked by one, and such gates sometimes render inside a shadow
        DOM so their label is unreadable ("unknown_field") and they stay unticked.
        Marketing/promotional opt-ins are skipped. Returns the labels ticked.
        """
        ticked: list[str] = []
        try:
            cbs = modal.locator('input[type="checkbox"]')
            n = await cbs.count()
        except Exception:
            return ticked
        for i in range(n):
            cb = cbs.nth(i)
            try:
                if await cb.is_checked():
                    continue
            except Exception:
                continue
            label = ""
            try:
                label = (await _get_field_label(cb, self._page) or "").lower()
            except Exception:
                pass
            if any(k in label for k in self._MARKETING_KW):
                log.info("apply.stuck_recovery_skip_marketing", label=label[:50])
                continue
            try:
                await self._click_choice_input(cb, desired=True)
                if await cb.is_checked():
                    ticked.append(label or "unlabeled")
            except Exception as e:
                log.debug("apply.stuck_recovery_tick_failed", error=str(e)[:60])
        return ticked

    async def _stuck_diagnostics(self, fields, modal, listing=None) -> str:
        """Capture a stuck form step — a screenshot plus each field's
        label/type/required/value/options and any visible inline error — so we
        can see which required field is unsatisfied (why Next won't advance).
        Screenshot filename is per-company so a sampling run keeps them all."""
        try:
            comp = "".join(ch for ch in (getattr(listing, "company", "") or "form")
                           if ch.isalnum() or ch in " _-")[:40].strip().replace(" ", "_")
            debug_dir = Path("data") / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            shot = str(debug_dir / f"stuck_{comp or 'form'}.png")
            await self._page.screenshot(path=shot, full_page=False)
        except Exception:
            shot = "(screenshot failed)"
        rows = []
        for f in fields:
            rows.append({
                "label": (f.label or "")[:46],
                "type": getattr(f.field_type, "value", str(f.field_type)),
                "req": bool(getattr(f, "required", False)),
                "val": (f.current_value or "")[:24],
                "opts": (f.options or [])[:5],
            })
        errtext = []
        try:
            raw = await modal.locator(
                '[role="alert"], [class*="error" i], [class*="artdeco-inline-feedback" i]'
            ).all_inner_texts()
            errtext = [t.strip() for t in raw if t.strip()][:6]
        except Exception:
            pass
        import json as _json
        return _json.dumps({"screenshot": shot, "fieldCount": len(fields),
                            "fields": rows, "errorsVisible": errtext})[:1900]

    async def _dialog_diagnostics(self) -> str:
        """Summarize the apply UI on the page — dialogs AND apply buttons plus the
        current URL — to diagnose why the Easy Apply modal wasn't reached
        (markup change vs. wrong/non-easy-apply button vs. navigation)."""
        try:
            info = await self._page.evaluate(
                "() => {"
                " const q = s => Array.from(document.querySelectorAll(s));"
                " const dialogs = q('[role=\"dialog\"], .artdeco-modal, [data-test-modal-id]')"
                "   .slice(0,5).map(n => ({modalId:n.getAttribute('data-test-modal-id'),"
                "     aria:n.getAttribute('aria-label'), cls:(n.className||'').toString().slice(0,60)}));"
                " const btns = q('button').filter(b => /apply/i.test((b.innerText||'')+"
                "   (b.getAttribute('aria-label')||''))).slice(0,6).map(b => ({"
                "     txt:(b.innerText||'').trim().slice(0,30),"
                "     aria:(b.getAttribute('aria-label')||'').slice(0,40),"
                "     cls:(b.className||'').toString().slice(0,50),"
                "     vis:!!(b.offsetWidth||b.offsetHeight)}));"
                " return {url:location.href.slice(0,90), dialogs, applyButtons:btns,"
                "   pages:window.length};"
                "}"
            )
            # Is the modal hiding in a child frame?
            frames = []
            for f in self._page.frames:
                try:
                    cnt = await f.locator('[role="dialog"], .artdeco-modal, [data-test-modal-id]').count()
                    if cnt > 0:
                        frames.append({"url": (f.url or "")[:60], "dialogs": cnt})
                except Exception:
                    pass
            info["framesWithDialog"] = frames
            info["frameCount"] = len(self._page.frames)
            # If the modal is visually open (obfuscated markup), find it by its
            # "Apply to …" heading and report the ancestor chain's identifying
            # attributes so we can build a correct selector.
            try:
                chain = await self._page.evaluate(
                    "() => {"
                    " const h = Array.from(document.querySelectorAll('h1,h2,h3,[role=heading]'))"
                    "   .find(e => /^apply to /i.test((e.innerText||'').trim()));"
                    " if (!h) return null;"
                    " let el = h; const out = [];"
                    " for (let i=0;i<9 && el;i++){"
                    "   out.push({tag:el.tagName, id:el.id||null, role:el.getAttribute('role'),"
                    "     aria:el.getAttribute('aria-label'),"
                    "     data:Array.from(el.attributes||[]).filter(a=>a.name.startsWith('data-'))"
                    "       .map(a=>a.name).slice(0,5),"
                    "     cls:(el.className||'').toString().slice(0,50)});"
                    "   el = el.parentElement;"
                    " } return out;"
                    "}"
                )
                info["applyToChain"] = chain
            except Exception:
                pass
            import json as _json
            return _json.dumps(info)[:1600]
        except Exception as e:
            return f"diag-failed: {str(e)[:80]}"

    async def _fix_validation_errors(self, fields, listing, modal) -> list[str]:
        """Read inline (red) validation errors and try to correct the field.

        Handles the common "please enter a whole number" / decimal / format
        errors on numeric fields by re-entering a cleaned value. Returns the
        list of error messages seen (so the caller can detect a stuck form).
        """
        seen: list[str] = []
        scope = modal if modal is not None else self._page
        try:
            err_locator = scope.locator(
                '.artdeco-inline-feedback--error, '
                '.fb-dash-form-element__error-text, '
                '[data-test-form-element-error-messages], '
                '.artdeco-inline-feedback__message'
            )
            err_count = await err_locator.count()
        except Exception:
            return seen
        if not err_count:
            return seen

        for i in range(min(err_count, 12)):
            try:
                el = err_locator.nth(i)
                if not await el.is_visible():
                    continue
                msg = " ".join((await el.text_content() or "").split()).strip()
                if not msg:
                    continue
                if msg not in seen:
                    seen.append(msg[:200])
                log.info("apply.validation_error", message=msg[:120])
                low = msg.lower()

                # Find the offending input near this error message
                container = el.locator("xpath=ancestor::*[self::div or self::fieldset][1]")
                target = container.locator("input, textarea, select").first
                if await target.count() == 0:
                    continue

                # Mechanical fix: numeric formatting errors.
                # NOTE: "enter a decimal number" means the field WANTS a decimal,
                # so it must not be rounded to a whole number.
                wants_whole = ("whole number" in low or "integer" in low) and "decimal" not in low
                if any(k in low for k in ("whole number", "integer", "decimal", "number", "numeric", "digits")):
                    current = await target.input_value()
                    cleaned = self._clean_numeric(current, whole=wants_whole)
                    if cleaned and cleaned != current:
                        await target.fill("")
                        await target.fill(cleaned)
                        log.info("apply.validation_fixed", **{"from": current, "to": cleaned})
                        await random_delay(0.3, 0.8)

                # Did the mechanical fix actually clear the error? A wrong fix
                # must NOT short-circuit the LLM fallback.
                try:
                    still_failing = await el.is_visible()
                except Exception:
                    still_failing = True

                # LLM fallback: hand the error text to the model for a value
                # that satisfies it (dates, lengths, formats, odd constraints).
                if still_failing:
                    llm = getattr(self._solver, "_llm", None)
                    if llm is not None:
                        try:
                            current = await target.input_value()
                        except Exception:
                            current = ""
                        label = await self._label_for(container)
                        try:
                            corrected = llm.fix_field_value(
                                question=label or msg,
                                error_message=msg,
                                current_value=current,
                                field_type=await self._input_kind(target),
                                job_title=listing.title,
                                company=listing.company,
                                profile_summary=self._solver._build_profile_summary(),
                            )
                        except Exception as e:
                            log.warning("apply.llm_fix_failed", error=str(e)[:80])
                            corrected = ""
                        if corrected and corrected != current:
                            await target.fill("")
                            await target.fill(corrected)
                            log.info("apply.validation_fixed_llm", label=(label or "")[:40],
                                     **{"from": current[:20], "to": corrected[:30]})
                            await random_delay(0.3, 0.8)
            except Exception as e:
                log.debug("apply.validation_fix_failed", error=str(e)[:80])

        return seen

    @staticmethod
    async def _label_for(container) -> str:
        """Best-effort question text for the field inside an error container."""
        try:
            lbl = container.locator("label")
            if await lbl.count() > 0:
                text = " ".join((await lbl.first.text_content() or "").split())
                if text:
                    return text[:150]
        except Exception:
            pass
        try:
            text = " ".join((await container.inner_text(timeout=2000) or "").split())
            return text[:150]
        except Exception:
            return ""

    @staticmethod
    async def _input_kind(target) -> str:
        try:
            tag = await target.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("textarea", "select"):
                return tag
            return (await target.get_attribute("type")) or "text"
        except Exception:
            return "text"

    @staticmethod
    def _clean_numeric(value: str, whole: bool) -> str:
        """Strip a value down to a valid number (optionally an integer)."""
        import re as _re
        if value is None:
            return ""
        m = _re.findall(r"\d+(?:\.\d+)?", value.replace(",", ""))
        if not m:
            return ""
        num = m[0]
        if whole and "." in num:
            num = num.split(".")[0]
        return num

    def _external_cfg(self) -> dict:
        """Merged external-apply config (new `external_apply:` section wins,
        with backward-compat for the old `bot.external_apply` keys)."""
        ext = self._config.settings.get("external_apply", {}) or {}
        bot = self._config.bot
        return {
            "enabled": ext.get("enabled", bot.get("external_apply", True)),
            "submit": ext.get("submit", False),
            "max_pages": int(ext.get("max_pages", bot.get("external_max_pages", 8))),
        }

    async def _submission_confirmed(self) -> bool:
        """Detect that an Easy Apply submission ACTUALLY completed.

        Requires a POSITIVE confirmation signal — LinkedIn's post-apply modal
        ("Your application was sent to …"). Two things this must NOT do, because
        they caused jobs to be recorded as applied without submitting:
          - Match the bare word "applied": it appears all over a jobs page
            ("28 applicants", "Applied 2h ago"), so it is never proof.
          - Treat the form's ABSENCE as success: the modal may simply have failed
            to open, or the page navigated — neither means we submitted.
        A false negative here just makes the job retryable; a false positive
        fabricates an application, so we err toward requiring explicit proof.
        """
        confirm_phrases = (
            "your application was sent",
            "application was sent to",
            "your application has been sent",
            "your application has been submitted",
            "application submitted successfully",
        )
        # During the submit transition LinkedIn briefly shows TWO dialogs (the
        # closing form + the opening "application sent" confirmation), so check
        # EVERY dialog, not just .first — and retry briefly, since the confirm
        # dialog animates in and can auto-dismiss. Missing it records a genuine
        # submission as FAILED, which then re-applies (a duplicate) next run.
        async def _seen() -> bool:
            try:
                dialogs = self._page.locator('dialog, div[role="dialog"], .artdeco-modal')
                n = await dialogs.count()
            except Exception:
                n = 0
            for i in range(min(n, 5)):
                try:
                    t = (await dialogs.nth(i).inner_text(timeout=1500) or "").lower()
                except Exception:
                    continue
                if any(p in t for p in confirm_phrases):
                    return True
            # Fall back to the whole page (confirmation can render inline).
            try:
                body = (await self._page.locator("body").inner_text(timeout=2000) or "").lower()
                return any(p in body for p in confirm_phrases)
            except Exception:
                return False

        for attempt in range(3):
            if await _seen():
                return True
            if attempt < 2:
                await random_delay(0.8, 1.5)
        return False

    async def _is_easy_apply(self) -> bool:
        """True if the current job uses in-app Easy Apply (not an external site)."""
        btn = self._page.locator(
            'button:has-text("Easy Apply"), button[aria-label*="Easy Apply"]'
        )
        try:
            return await btn.count() > 0
        except Exception:
            return False

    # ── Dynamic external application (beyond Easy Apply) ──

    async def _apply_external(
        self, listing: JobListing, resume_path: str | None
    ) -> ApplicationRecord:
        """Discover and navigate a company-website application dynamically.

        Clicks the external "Apply" button (which usually opens the employer's ATS
        in a new tab), then runs an LLM-guided loop: detect fields → answer them
        via the hybrid solver → upload the resume → pick the button that advances
        the form → repeat, until it submits, completes, or gets stuck.
        """
        ext_cfg = self._external_cfg()
        dry_run = self._config.bot.get("dry_run", True)
        # External submit is gated by BOTH not-dry-run AND external_apply.submit,
        # so external forms can be filled-but-not-submitted even in live mode.
        allow_external_submit = (not dry_run) and ext_cfg["submit"]
        max_pages = ext_cfg["max_pages"]
        llm = getattr(self._solver, "_llm", None)

        # Find and click the external apply button, capturing any new tab
        apply_btn = self._page.locator(
            'button.jobs-apply-button, '
            'button[aria-label*="Apply"], '
            'a[aria-label*="Apply"]'
        )
        if await apply_btn.count() == 0:
            log.warning("apply.external_no_button", url=listing.url)
            return self._make_record(listing, ApplicationStatus.SKIPPED, "No apply button found")

        context = self._page.context
        ext_page = self._page
        opened_tab = None  # a new tab we open (so we can close it afterwards)
        try:
            async with context.expect_page(timeout=8000) as new_page_info:
                await apply_btn.first.click()
            ext_page = await new_page_info.value
            opened_tab = ext_page
            await ext_page.wait_for_load_state("domcontentloaded", timeout=20000)
            log.info("apply.external_new_tab", url=ext_page.url)
        except Exception:
            # No new tab — the apply may navigate in place or open an in-page panel
            await random_delay(2.0, 4.0)
            pages = context.pages
            ext_page = pages[-1] if pages else self._page
            if ext_page is not self._page:
                opened_tab = ext_page
            log.info("apply.external_same_tab", url=ext_page.url)

        orig_page = self._page
        self._page = ext_page  # reuse _fill_fields / _set_field_value on the ATS page
        try:
            last_signature = None
            for page_i in range(max_pages):
                try:
                    await ext_page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await random_delay(1.5, 3.0)
                await self._maybe_screenshot(listing)

                # Completion check
                if await self._external_success(ext_page):
                    log.info("apply.external_submitted", company=listing.company, url=ext_page.url)
                    status = ApplicationStatus.DRY_RUN if dry_run else ApplicationStatus.APPLIED
                    return self._make_record(
                        listing, status,
                        f"External application ({'filled, not submitted' if dry_run else 'submitted'}) at {ext_page.url}",
                    )

                # Fill everything we can on this page — scoped to the primary
                # application form so we don't touch unrelated page inputs
                # (search boxes, newsletter signups, cookie banners).
                form_scope = await self._external_form_scope(ext_page)
                fields = await detect_form_fields(ext_page, scope=form_scope)
                await self._fill_fields(fields, listing)
                await random_delay(1.0, 2.0)

                # Decide which button advances the application
                buttons = await self._collect_button_labels(ext_page)
                page_summary = await self._page_text_summary(ext_page)
                choice = ""
                if llm and buttons:
                    choice = llm.choose_next_action(
                        buttons, listing.title, listing.company,
                        page_summary=page_summary, allow_submit=allow_external_submit,
                    )
                if not choice:
                    choice = self._heuristic_next_button(buttons, allow_submit=allow_external_submit)

                # Only a real terminal action counts as "submit". A bare "Apply"
                # on an ATS landing page STARTS the form and must be clickable
                # even when we won't submit, so it is NOT treated as final submit.
                is_submit = choice and any(
                    k in choice.lower() for k in ("submit", "send application", "finish")
                )

                # If we're not allowed to submit (dry-run OR external_apply.submit
                # is false), stop before the final submit and record that we filled it.
                if is_submit and not allow_external_submit:
                    reason = "dry run" if dry_run else "external_apply.submit is false"
                    log.info("apply.external_fill_only", company=listing.company, button=choice, reason=reason)
                    status = ApplicationStatus.DRY_RUN if dry_run else ApplicationStatus.NEEDS_REVIEW
                    return self._make_record(
                        listing, status,
                        f"External application filled, stopped before '{choice}' ({reason})",
                    )

                if not choice:
                    # Stuck: no field progress and nothing to click (often a login/account wall)
                    # NOT submitted → FAILED (honest + retryable), never NEEDS_REVIEW,
                    # which counts as applied and would block a real future attempt.
                    reason = "External flow stuck (login/account wall or unknown form) — NOT submitted"
                    log.warning("apply.external_stuck", company=listing.company, url=ext_page.url, buttons=buttons[:6])
                    return self._make_record(listing, ApplicationStatus.FAILED, reason)

                # Detect a no-progress loop (same URL + same fields + same buttons)
                signature = (ext_page.url, len(fields), tuple(buttons))
                if signature == last_signature:
                    log.warning("apply.external_no_progress", company=listing.company, url=ext_page.url)
                    return self._make_record(
                        listing, ApplicationStatus.FAILED,
                        "External flow made no progress — NOT submitted (may need manual completion)",
                    )
                last_signature = signature

                log.info("apply.external_click", button=choice, page=page_i + 1)
                await self._click_button_by_text(ext_page, choice)
                await random_delay(2.0, 4.0)

            log.warning("apply.external_max_pages", company=listing.company)
            return self._make_record(
                listing, ApplicationStatus.FAILED,
                "External application exceeded max pages — NOT submitted (may need manual completion)",
            )
        except Exception as e:
            log.error("apply.external_error", error=str(e), company=listing.company)
            return self._make_record(listing, ApplicationStatus.FAILED, f"External flow error: {e}")
        finally:
            self._page = orig_page
            # Close the tab we opened so tabs don't leak and a stale tab isn't
            # mistakenly picked up on the next job.
            if opened_tab is not None and opened_tab is not orig_page:
                try:
                    await opened_tab.close()
                except Exception:
                    pass

    async def _external_form_scope(self, page):
        """Locator for the primary application form on an external ATS page.

        Prefers a <form> containing a file input or the most inputs; falls back
        to None (whole page) if nothing obvious is found.
        """
        try:
            forms = page.locator("form")
            n = await forms.count()
            if n == 0:
                return None
            best, best_score = None, -1
            for i in range(min(n, 8)):
                f = forms.nth(i)
                try:
                    inputs = await f.locator("input, select, textarea").count()
                    has_file = await f.locator('input[type="file"]').count()
                    score = inputs + (50 if has_file else 0)
                    if score > best_score:
                        best, best_score = f, score
                except Exception:
                    continue
            # Only scope if the form actually has fields worth filling
            return best if best_score >= 2 else None
        except Exception:
            return None

    async def _collect_button_labels(self, page) -> list[str]:
        """Collect visible button / submit-input / link-button labels on a page."""
        labels: list[str] = []
        try:
            btns = page.locator(
                'button, input[type="submit"], input[type="button"], [role="button"], a.btn, a[class*="button"]'
            )
            count = min(await btns.count(), 40)
            for i in range(count):
                el = btns.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    text = (await el.text_content() or "").strip()
                    if not text:
                        text = (await el.get_attribute("value") or "").strip()
                    if not text:
                        text = (await el.get_attribute("aria-label") or "").strip()
                    text = " ".join(text.split())
                    if text and 1 <= len(text) <= 60 and text not in labels:
                        labels.append(text)
                except Exception:
                    continue
        except Exception:
            pass
        return labels

    @staticmethod
    def _heuristic_next_button(buttons: list[str], allow_submit: bool) -> str:
        """Fallback button choice when the LLM abstains."""
        # "review" advances toward submit; keep it ahead of generic "next".
        forward = ["review your application", "review", "next", "continue",
                   "save and continue", "proceed", "start application"]
        submit = ["submit application", "submit", "send application", "finish"]
        # Never click destructive / dismissive buttons.
        # (Do NOT put bare "save" here — it would eat "save and continue".)
        avoid = ["cancel", "back", "previous", "sign out", "save draft", "log out",
                 "dismiss", "discard", "not now", "no thanks", "delete",
                 "remove", "withdraw", "report"]
        lowered = [(b, b.lower()) for b in buttons if not any(a in b.lower() for a in avoid)]
        for kw in forward:
            for orig, low in lowered:
                if kw in low:
                    return orig
        if allow_submit:
            for kw in submit:
                for orig, low in lowered:
                    if kw in low:
                        return orig
        return ""

    async def _click_button_by_text(self, page, text: str) -> None:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if await btn.count() > 0:
                await btn.first.click()
                return
        except Exception:
            pass
        try:
            btn = page.locator(f':is(button, a, [role="button"]):has-text("{text}")')
            if await btn.count() > 0:
                await btn.first.click()
        except Exception as e:
            log.warning("apply.external_click_failed", text=text, error=str(e))

    async def _external_success(self, page) -> bool:
        """Heuristically detect a submitted/confirmed application."""
        try:
            body = (await page.locator("body").inner_text(timeout=4000) or "").lower()
        except Exception:
            return False
        markers = [
            "thank you for applying", "application submitted", "application received",
            "we have received your application", "successfully submitted",
            "thanks for applying", "your application has been", "application complete",
        ]
        return any(m in body for m in markers)

    async def _page_text_summary(self, page) -> str:
        try:
            heading = await page.locator("h1, h2").first.text_content(timeout=2000)
            return (heading or "").strip()
        except Exception:
            return ""

    async def _ensure_resume_uploaded(self, scope) -> bool:
        """Upload our tailored PDF via the "Upload resume" BUTTON.

        Some résumé steps have no ``input[type=file]`` in the DOM — an "Upload
        resume" button triggers a file chooser instead — so the bot would
        otherwise submit with a stored/previous résumé. We intercept that chooser
        with Playwright and push the tailored PDF. Once per application.
        """
        if getattr(self, "_resume_uploaded_this_app", False):
            return False
        resume_path = getattr(self, "_resume_path", None)
        if not resume_path or not Path(resume_path).exists():
            return False
        if Path(resume_path).suffix.lower() not in _RESUME_EXTS:
            return False
        try:
            btn = scope.locator(
                'button:has-text("Upload resume"), label:has-text("Upload resume"), '
                '[role="button"]:has-text("Upload resume")'
            )
            if await btn.count() == 0 or not await btn.first.is_visible():
                return False
        except Exception:
            return False
        try:
            async with self._page.expect_file_chooser(timeout=6000) as fc_info:
                await btn.first.click()
            chooser = await fc_info.value
            await chooser.set_files(resume_path)
            self._resume_uploaded_this_app = True
            await random_delay(1.5, 3.0)
            log.info("apply.resume_uploaded", path=resume_path, via="upload_button")
            return True
        except Exception as e:
            log.warning("apply.resume_upload_button_failed", error=str(e)[:120])
            return False

    async def _step_marker(self, modal) -> str:
        """A per-step identity marker for the stuck detector: the modal's progress
        indicator (aria-valuenow / "X/Y pages") plus its section heading. Two
        genuinely different steps that both have no fillable fields still get
        different markers, so advancing resets the stuck counter; a step that
        truly repeats keeps the same marker and is still caught.
        """
        try:
            return await modal.evaluate(
                r"""el => {
                  const norm = s => (s||'').replace(/\s+/g,' ').trim();
                  let prog = '';
                  const pb = el.querySelector('progress,[role=progressbar]');
                  if (pb) prog = pb.getAttribute('aria-valuenow') || pb.value || '';
                  if (!prog) { const m = norm(el.innerText).match(/(\d+)\s*\/\s*(\d+)\s*(pages|steps)?/i); if (m) prog = m[0]; }
                  const h = el.querySelector('h1,h2,h3,h4,[role=heading]');
                  const head = h ? norm(h.innerText).slice(0,60) : '';
                  return (prog + '|' + head).slice(0, 90);
                }"""
            ) or ""
        except Exception:
            return ""

    async def _diag_dump_structures(self, modal) -> None:
        """DIAGNOSTIC (set VELVET_DIAG=1, dry-run): dump résumé-step and
        structured-card markup so the upload / card-fill flows can be built from
        real DOM. Also clicks ONE "Edit" (once per run) to reveal the card
        sub-form. No-op unless the flag is set.
        """
        if not os.environ.get("VELVET_DIAG"):
            return
        page = self._page
        try:
            static = await page.evaluate(
                r"""() => {
                  const norm = s => (s||'').replace(/\s+/g,' ').trim();
                  const dlg = document.querySelector('div[role=dialog], dialog') || document.body;
                  const out = {buttons: [], file_inputs: [], resume_html: null, cards: []};
                  dlg.querySelectorAll('button,[role=button],label').forEach(b => {
                    const t = norm(b.innerText); const r = b.getBoundingClientRect();
                    if (t && t.length < 42 && r.width > 0)
                      out.buttons.push(t + ' <' + b.tagName + (b.getAttribute('for') ? ' for=' + b.getAttribute('for') : '') + '>');
                  });
                  document.querySelectorAll('input[type=file]').forEach(f =>
                    out.file_inputs.push({id: f.id, name: f.name, accept: f.accept}));
                  const rh = Array.from(dlg.querySelectorAll('*')).find(e =>
                    /upload resume|be sure to include|tailor resume/i.test(norm(e.innerText))
                    && norm(e.innerText).length < 700 && e.querySelectorAll('*').length < 70);
                  if (rh) out.resume_html = rh.outerHTML.replace(/\s+/g,' ').slice(0, 1500);
                  dlg.querySelectorAll('button,[role=button]').forEach(b => {
                    if (norm(b.innerText) === 'Edit') {
                      let card = b;
                      for (let i = 0; i < 6 && card; i++) { card = card.parentElement;
                        if (card && norm(card.innerText).length > 40) break; }
                      if (card) out.cards.push(norm(card.innerText).slice(0, 240));
                    }
                  });
                  out.checkboxes = [];
                  dlg.querySelectorAll('input[type=checkbox], [role=checkbox]').forEach(cb => {
                    const p1 = cb.parentElement, p2 = p1 && p1.parentElement;
                    out.checkboxes.push({
                      tag: cb.tagName, type: cb.getAttribute('type'), id: cb.id,
                      name: cb.getAttribute('name'), aria: cb.getAttribute('aria-label'),
                      labelledby: cb.getAttribute('aria-labelledby'),
                      required: cb.required || cb.getAttribute('aria-required'),
                      self: (cb.outerHTML||'').replace(/\s+/g,' ').slice(0,180),
                      p1_text: p1 ? norm(p1.innerText).slice(0,140) : null,
                      p2_text: p2 ? norm(p2.innerText).slice(0,180) : null,
                      p1_html: p1 ? (p1.outerHTML||'').replace(/\s+/g,' ').slice(0,420) : null,
                    });
                  });
                  out.checkboxes = out.checkboxes.slice(0, 8);
                  out.buttons = [...new Set(out.buttons)].slice(0, 18);
                  out.cards = out.cards.slice(0, 8);
                  return JSON.stringify(out);
                }"""
            )
            log.info("diag.static", dump=static)
            # Click a CARD "Edit" once (not the contact-info edit) to reveal the
            # card sub-form. Mark the right one so Playwright clicks exactly it.
            if not getattr(self, "_diag_edited", False):
                marked = await page.evaluate(
                    r"""() => {
                      const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
                      const dlg = document.querySelector('div[role=dialog], dialog') || document.body;
                      const kw = ['dates of employment','dates attended','discipline',
                                  'your title','field of study','school'];
                      const edits = [...dlg.querySelectorAll('button,[role=button]')]
                        .filter(b => norm(b.innerText) === 'edit');
                      for (const e of edits) {
                        let c = e;
                        for (let j = 0; j < 6 && c; j++) { c = c.parentElement;
                          if (c && kw.some(k => norm(c.innerText).includes(k))) {
                            e.setAttribute('data-diag-edit','1'); return true; } }
                      }
                      return false;
                    }"""
                )
                if marked:
                    self._diag_edited = True
                    try:
                        await modal.locator('[data-diag-edit="1"]').first.click(timeout=4000)
                        await random_delay(1.2, 2.0)
                        sub = await page.evaluate(
                            r"""() => {
                              const norm = s => (s||'').replace(/\s+/g,' ').trim();
                              const dlg = document.querySelector('div[role=dialog], dialog') || document.body;
                              const fields = [];
                              dlg.querySelectorAll('input,select,textarea').forEach(el => {
                                const r = el.getBoundingClientRect();
                                let lbl = '';
                                if (el.id) { const l = document.querySelector('label[for="'+el.id+'"]'); if (l) lbl = norm(l.innerText); }
                                fields.push({tag: el.tagName, type: el.type||'', id: el.id, name: el.name,
                                  vis: (r.width>0&&r.height>0), label: lbl.slice(0,40),
                                  aria: el.getAttribute('aria-label'), ph: el.getAttribute('placeholder')});
                              });
                              const btns = []; dlg.querySelectorAll('button,[role=button]').forEach(b => {
                                const t = norm(b.innerText); if (t && t.length < 30) btns.push(t); });
                              return JSON.stringify({fields: fields.slice(0,22), btns: [...new Set(btns)].slice(0,14)});
                            }"""
                        )
                        log.info("diag.edit_subform", dump=sub)
                    except Exception as e:
                        log.warning("diag.edit_click_failed", error=str(e)[:120])
        except Exception as e:
            log.warning("diag.structures_failed", error=str(e)[:120])

    async def _fill_fields(self, fields: list[FormField], listing: JobListing) -> None:
        """Fill all detected fields using the hybrid solver."""
        for field in fields:
            # Resume / file upload — use the tailored resume path directly
            if field.field_type == FieldType.FILE_UPLOAD:
                resume_path = getattr(self, "_resume_path", None)
                ext = Path(resume_path).suffix.lower() if resume_path else ""
                # NEVER upload a non-resume file: LinkedIn/ATS reject anything
                # but PDF/DOC/DOCX, which would silently block submission while
                # we log "uploaded". Flag it for review instead.
                if resume_path and Path(resume_path).exists() and ext in _RESUME_EXTS:
                    try:
                        await field.locator.set_input_files(resume_path)
                        log.info("apply.resume_uploaded", path=resume_path, label=field.label)
                        self._questions.append(QuestionRecord(
                            question_text=field.label or "Resume upload",
                            field_type="file_upload",
                            answer_given=Path(resume_path).name,
                            answer_source="profile",
                            needs_review=False,
                        ))
                    except Exception as e:
                        log.warning("apply.resume_upload_failed", error=str(e), label=field.label)
                else:
                    log.warning(
                        "apply.resume_not_uploadable",
                        path=resume_path, ext=ext,
                        msg="no valid PDF/DOC resume to upload — flagging for review",
                    )
                    self._questions.append(QuestionRecord(
                        question_text=field.label or "Resume upload",
                        field_type="file_upload",
                        answer_given=f"MISSING/INVALID RESUME ({ext or 'none'})",
                        answer_source="skip",
                        needs_review=True,
                    ))
                continue

            # SAFETY: a control with no label is not a real application question.
            # Answering it blindly (the LLM returns "Yes" for an empty prompt) and
            # clicking it can toggle unrelated UI. Skip it.
            if not (field.label or "").strip():
                log.warning("apply.unlabeled_field_skipped",
                            field_type=field.field_type.value)
                continue

            # Skip if already has a value (pre-filled)
            if field.current_value:
                log.debug("apply.field_prefilled", label=field.label, value=field.current_value[:30])
                continue

            try:
                answer, source, needs_review = await self._solver.solve(
                    field, listing.description, listing.title, listing.company
                )

                if answer is None:
                    log.warning("apply.no_answer", label=field.label)
                    continue

                await self._set_field_value(field, answer)

                # READ-BACK VERIFICATION: re-read the live DOM value and confirm
                # it matches what we intended. A mismatch (e.g. a radio left at
                # its default) is flagged for review instead of being silently
                # recorded as the intended answer.
                verified, actual = await self._readback_field(field, answer)
                if verified is False:
                    needs_review = True
                    log.warning("apply.field_unverified", label=field.label[:50],
                                intended=str(answer)[:30], actual=str(actual)[:30])
                elif verified is True:
                    log.debug("apply.field_verified", label=field.label[:50],
                              value=str(actual)[:30])

                self._questions.append(QuestionRecord(
                    question_text=field.label,
                    field_type=field.field_type.value,
                    answer_given=str(answer),
                    answer_source=source,
                    needs_review=needs_review,
                ))
            except Exception as e:
                log.warning("apply.field_error", label=field.label, error=str(e))

    async def _set_field_value(self, field: FormField, value: str) -> None:
        """Set the value of a form field based on its type."""
        match field.field_type:
            case FieldType.TEXT | FieldType.NUMERIC:
                await field.locator.fill("")
                if self._is_location_field(field.label):
                    # Type so the autocomplete fires, then pick the suggestion
                    await field.locator.type(str(value), delay=40)
                    await self._pick_typeahead(field, str(value))
                else:
                    await field.locator.fill(str(value))

            case FieldType.TEXTAREA:
                await field.locator.fill("")
                await field.locator.fill(str(value))

            case FieldType.DROPDOWN:
                await self._select_dropdown(field, str(value))

            case FieldType.RADIO:
                # Find and click the radio option matching the value
                await self._select_radio_option(field, str(value))

            case FieldType.CHECKBOX:
                should_check = str(value).lower() in ("true", "yes", "1")
                try:
                    is_checked = await field.locator.is_checked()
                except Exception:
                    is_checked = False
                if should_check != is_checked:
                    # Click the visible label proxy — the native checkbox is
                    # visually hidden and not directly clickable.
                    await self._click_choice_input(field.locator, desired=should_check)

            case FieldType.FILE_UPLOAD:
                if value and Path(value).exists():
                    await field.locator.set_input_files(value)
                    log.info("apply.resume_uploaded", path=value)

        await random_delay(0.5, 1.5)

    @staticmethod
    def _is_location_field(label: str) -> bool:
        low = (label or "").lower()
        return any(k in low for k in ("city", "location", "address", "town", "where are you"))

    async def _pick_typeahead(self, field: FormField, value: str) -> None:
        """After typing into a location field, click the matching suggestion.

        LinkedIn and many ATS location inputs pop up an autocomplete list; the
        value isn't accepted until you click a suggestion.
        """
        await random_delay(0.8, 1.5)
        option_selectors = (
            '.search-typeahead-v2__hit, '
            '.basic-typeahead__selectable, '
            '[role="option"], '
            'ul[role="listbox"] li, '
            '.jobs-search-box__typeahead-suggestion'
        )
        try:
            options = self._page.locator(option_selectors)
            count = await options.count()
            if count == 0:
                # No popup — the plain typed value is fine
                return
            value_l = value.lower()
            # Prefer an option that matches the value; else take the first
            best = None
            for i in range(min(count, 8)):
                opt = options.nth(i)
                if not await opt.is_visible():
                    continue
                text = (await opt.text_content() or "").strip().lower()
                if value_l in text or text.startswith(value_l.split(",")[0]):
                    best = opt
                    break
            if best is None:
                best = options.first
            if await best.is_visible():
                await best.click()
                log.info("apply.typeahead_selected", label=field.label, value=value)
        except Exception as e:
            log.debug("apply.typeahead_failed", label=field.label, error=str(e)[:80])

    async def _select_dropdown(self, field: FormField, value: str) -> None:
        """Select a dropdown option robustly without hanging on a mismatch."""
        value_l = value.lower().strip()
        # 1) Exact label (short timeout so a miss fails fast, not 30s)
        try:
            await field.locator.select_option(label=value, timeout=3000)
            return
        except Exception:
            pass
        # 2) Fuzzy match against the actual option texts
        try:
            options = field.locator.locator("option")
            option_texts: list[str] = []
            best = None
            for i in range(await options.count()):
                opt = options.nth(i)
                text = (await opt.text_content() or "").strip()
                tl = text.lower()
                if not tl:
                    continue
                option_texts.append(text)
                # Exact, or a substantial substring either way (len>=3 so "no"
                # doesn't match "None", "male" doesn't match "female", etc.).
                if best is None and (value_l == tl or (len(value_l) >= 3 and value_l in tl) or
                                     (len(tl) >= 3 and tl in value_l)):
                    best = text
            if best is not None:
                await field.locator.select_option(label=best, timeout=3000)
                return
            # 2.5) Degree-level questions: "Master's Degree" rarely matches a
            #      dropdown option verbatim (options say "Master of Science",
            #      "Graduate degree", ...). Match on the shared degree KEYWORD.
            deg = _match_degree_option(value, option_texts)
            if deg is not None:
                await field.locator.select_option(label=deg, timeout=3000)
                log.info("apply.dropdown_degree_matched", label=field.label, chose=deg)
                return
            # 2.6) Consent/authorization dropdowns answered "Yes" whose options are
            #      worded "I agree" / "I acknowledge" (never a refusal option).
            con = _match_consent_option(value, option_texts, field.label)
            if con is not None:
                await field.locator.select_option(label=con, timeout=3000)
                log.info("apply.dropdown_consent_matched", label=field.label[:50], chose=con)
                return
            # 2.7) "What LEVEL of security clearance?" answered "No" -> pick "None".
            clr = _match_clearance_option(value, option_texts, field.label)
            if clr is not None:
                await field.locator.select_option(label=clr, timeout=3000)
                log.info("apply.dropdown_clearance_matched", label=field.label[:50], chose=clr)
                return
            # 2.8) "How many years ...?" dropdown whose options are year RANGES
            #      ("0-1", "3-5 years", "7+") -> pick the range containing our
            #      number, so a numeric answer like "5" isn't left unselected.
            yr = _match_year_range_option(value, option_texts)
            if yr is not None:
                await field.locator.select_option(label=yr, timeout=3000)
                log.info("apply.dropdown_year_range_matched", label=field.label[:50], chose=yr)
                return
        except Exception:
            pass
        # 3) No confident match — do NOT pick an arbitrary option. Selecting a
        #    real value (index=1) could submit a wrong answer or disclose an EEO
        #    demographic the user withheld. Leave it unselected.
        log.warning("apply.dropdown_unresolved", label=field.label, value=value)

    async def _click_choice_input(self, inp, desired: bool | None = None) -> bool:
        """Select a radio/checkbox by its VISIBLE proxy.

        LinkedIn hides the native <input> and renders a styled <label> as the
        clickable element, so clicking the input itself is not actionable. Try
        label[for=id], then a wrapping <label>, then a force click. ``desired``
        is the target checked-state for a checkbox (None for radios, which are
        always selected). The last-resort uses .check()/.uncheck() to match the
        desired state — never blindly check (which would flip an intended
        uncheck the wrong way).
        """
        try:
            iid = await inp.get_attribute("id")
            if iid and '"' not in iid and "\\" not in iid:
                lbl = self._page.locator(f'label[for="{iid}"]')
                if await lbl.count() > 0 and await lbl.first.is_visible():
                    await lbl.first.click()
                    return True
        except Exception:
            pass
        try:
            wrap = inp.locator("xpath=ancestor::label[1]")
            if await wrap.count() > 0 and await wrap.first.is_visible():
                await wrap.first.click()
                return True
        except Exception:
            pass
        try:
            await inp.click(force=True, timeout=3000)
            return True
        except Exception:
            pass
        try:
            if desired is False:
                await inp.uncheck(force=True, timeout=3000)
            else:
                await inp.check(force=True, timeout=3000)
            return True
        except Exception:
            return False

    async def _select_radio_option(self, field: FormField, value: str) -> None:
        """Select a radio button option by matching the value to option labels."""
        value_lower = value.lower()

        # Find all radio buttons in the same group
        name = await field.locator.get_attribute("name")
        if name:
            radios = self._page.locator(f'input[type="radio"][name="{name}"]')
        else:
            # Fallback: look in parent fieldset
            radios = field.locator.locator("xpath=ancestor::fieldset").locator('input[type="radio"]')

        for i in range(await radios.count()):
            radio = radios.nth(i)
            otext = (await self._radio_visible_text(radio)).strip().lower()
            if self._option_matches_value(value_lower, otext):
                await self._click_choice_input(radio)
                await self._verify_radio(field, value, radios)
                return

        # DECLINE intent (EEO / voluntary self-identification): the desired value
        # is a "prefer not to say" phrase that rarely matches the option text
        # verbatim ("I prefer not to specify"). Click the option that actually
        # declines, matched by keyword — and NEVER a real demographic. If there
        # is no decline option, leave the group UNSELECTED (better to skip than
        # disclose race/gender/veteran/disability the user withheld).
        _decline_markers = (
            "prefer not", "decline", "not to say", "not to specify",
            "not to answer", "not to disclose", "do not wish", "don't wish",
            "choose not", "rather not", "not specified", "self-identif",
            "i do not want to answer", "wish not to",
        )
        if any(m in value_lower for m in (
            "prefer not", "decline", "not to say", "not to specify",
            "not to answer", "self-identif", "not to disclose", "do not wish",
        )):
            for i in range(await radios.count()):
                r = radios.nth(i)
                try:
                    txt = ""
                    rid = await r.get_attribute("id")
                    if rid and '"' not in rid and "\\" not in rid:
                        lab = self._page.locator(f'label[for="{rid}"]')
                        if await lab.count() > 0:
                            txt = (await lab.first.text_content() or "").strip().lower()
                    if not txt:
                        wrap = r.locator("xpath=ancestor::label[1]")
                        if await wrap.count() > 0:
                            txt = (await wrap.first.text_content() or "").strip().lower()
                    if not txt:
                        txt = (await r.get_attribute("value") or "").lower()
                    if any(m in txt for m in _decline_markers):
                        await self._click_choice_input(r)
                        log.info("apply.eeo_declined", label=field.label[:50], chose=txt[:30])
                        return
                except Exception:
                    continue
            log.warning("apply.eeo_no_decline_option", label=field.label[:60])
            return  # leave unselected — never select a real demographic

        # Fallback: if affirmative, find the option whose label actually reads
        # "yes" — never assume the FIRST radio is Yes (a group may render No
        # first, which would flip the answer). If there's no yes option, leave
        # it unselected rather than guessing wrong.
        if value_lower in ("yes", "true"):
            for i in range(await radios.count()):
                r = radios.nth(i)
                try:
                    txt = (await self._radio_visible_text(r)).strip().lower()
                    if txt == "yes" or txt.startswith("yes"):
                        await self._click_choice_input(r)
                        await self._verify_radio(field, value, radios)
                        return
                except Exception:
                    continue
            log.warning("apply.radio_no_yes_option", label=field.label)

        # Symmetric fallback for a NEGATIVE answer: click the option whose visible
        # text reads "no". Prevents leaving the group at its default (often "Yes")
        # when the primary match missed.
        if value_lower in ("no", "false"):
            for i in range(await radios.count()):
                r = radios.nth(i)
                try:
                    txt = (await self._radio_visible_text(r)).strip().lower()
                    if txt == "no" or txt.startswith("no,") or txt.startswith("no "):
                        await self._click_choice_input(r)
                        await self._verify_radio(field, value, radios)
                        return
                except Exception:
                    continue
            log.warning("apply.radio_no_no_option", label=field.label)

    async def _radio_visible_text(self, radio) -> str:
        """The visible option text for a radio, robust to LinkedIn rendering the
        text in a sibling span rather than the label[for] (which then reads empty
        — the bug that flipped sponsorship to Yes). Order: label[for] -> wrapping
        <label> -> nearby sibling/parent visible text -> value attribute."""
        try:
            rid = await radio.get_attribute("id")
            if rid and '"' not in rid and "\\" not in rid:
                lab = self._page.locator(f'label[for="{rid}"]')
                if await lab.count() > 0:
                    t = (await lab.first.text_content() or "").strip()
                    if t:
                        return t
        except Exception:
            pass
        try:
            wrap = radio.locator("xpath=ancestor::label[1]")
            if await wrap.count() > 0:
                t = (await wrap.first.text_content() or "").strip()
                if t:
                    return t
        except Exception:
            pass
        try:
            t = await radio.evaluate(
                """e => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    // Following siblings that hold THIS option's text — stop
                    // before reaching another radio (its text is not ours).
                    let sib = e.nextElementSibling;
                    while (sib) {
                        if (sib.matches && (sib.matches('input[type=radio]') ||
                            (sib.querySelector && sib.querySelector('input[type=radio]')))) break;
                        const t = norm(sib.innerText || sib.textContent || '');
                        if (t && t.length <= 48) return t;
                        sib = sib.nextElementSibling;
                    }
                    // Climb, but never into a container holding MORE than one radio
                    // (that is the whole group — its text is every option joined).
                    let p = e.parentElement;
                    for (let i = 0; i < 3 && p; i++) {
                        if (p.querySelectorAll('input[type=radio]').length > 1) break;
                        const t = norm(p.innerText || p.textContent || '');
                        if (t && t.length <= 48) return t;
                        p = p.parentElement;
                    }
                    return '';
                }"""
            )
            if t and t.strip():
                return t.strip()
        except Exception:
            pass
        return (await radio.get_attribute("value") or "").strip()

    @staticmethod
    def _option_matches_value(value_lower: str, otext: str) -> bool:
        """Precise option<->value match: exact, then a "value, …"/"value …" prefix
        (verbose options like "No, I will not require sponsorship"), then the value
        as a WHOLE WORD — so "No" never matches "Not applicable", and an empty
        option never matches anything."""
        if not otext or not value_lower:
            return False
        if otext == value_lower:
            return True
        if otext.startswith(value_lower + ",") or otext.startswith(value_lower + " "):
            return True
        if re.search(r"\b" + re.escape(value_lower) + r"\b", otext):
            return True
        if len(otext) >= 3 and otext in value_lower:
            return True
        return False

    async def _verify_radio(self, field, intended: str, radios) -> None:
        """After selecting, log which option actually ended up checked vs what we
        intended — so a mis-selection (e.g. sponsorship left at the default "Yes")
        is visible instead of silently recorded as the intended value."""
        try:
            checked = None
            opts = []
            for i in range(await radios.count()):
                r = radios.nth(i)
                txt = " ".join((await self._radio_visible_text(r)).split())[:26]
                opts.append(txt or "?")
                try:
                    if await r.is_checked():
                        checked = txt or "?"
                except Exception:
                    pass
            ok = bool(checked) and intended.strip().lower() in (checked or "").lower()
            (log.info if ok else log.warning)(
                "apply.radio_result", label=field.label[:48], intended=intended,
                checked=checked, options=opts[:6],
            )
        except Exception:
            pass

    async def _readback_field(self, field: FormField, intended: str):
        """Re-read a field's ACTUAL value from the live DOM after filling and
        check it matches the intended answer. Catches selections that silently
        didn't take — a radio left at its default, a dropdown that never set, a
        text box that didn't accept the value. Returns:
          True  -> verified (actual matches intended)
          False -> MISMATCH (flag for review)
          None  -> unverifiable (couldn't read the control; don't flag)
        and the actual value read, as (status, actual_str).
        """
        ft = field.field_type
        want = str(intended).strip().lower()
        # Nothing intended (e.g. an optional field deliberately left blank) — an
        # empty actual value matches that intent, so don't flag it.
        if not want:
            return (None, "(no value intended)")
        # LinkedIn's résumé-method widget ("Upload resume / Tailor resume with AI")
        # is fulfilled by the separate upload flow (verified via resume_uploaded),
        # not by a checked radio — so skip read-back to avoid a false "none
        # selected" flag.
        label_l = (field.label or "").lower()
        if "upload resume" in label_l and ("tailor" in label_l or "with ai" in label_l):
            return (None, "resume-widget")
        try:
            if ft == FieldType.CHECKBOX:
                target = want in ("yes", "true", "1")
                actual = await field.locator.is_checked()
                return (actual == target, "checked" if actual else "unchecked")

            if ft == FieldType.RADIO:
                name = await field.locator.get_attribute("name")
                radios = (
                    self._page.locator(f'input[type="radio"][name="{name}"]')
                    if name else
                    field.locator.locator("xpath=ancestor::fieldset").locator('input[type="radio"]')
                )
                checked_txt = None
                for i in range(await radios.count()):
                    r = radios.nth(i)
                    try:
                        if await r.is_checked():
                            checked_txt = (await self._radio_visible_text(r)).strip()
                            break
                    except Exception:
                        continue
                if not checked_txt:
                    return (False, "(none selected)")
                return (self._option_matches_value(want, checked_txt.lower()), checked_txt)

            if ft == FieldType.DROPDOWN:
                actual = ""
                try:
                    actual = (await field.locator.evaluate(
                        "e => (e.tagName === 'SELECT' && e.selectedIndex >= 0) "
                        "? (e.options[e.selectedIndex].text || e.value || '') "
                        ": (e.value || '')"
                    )) or ""
                except Exception:
                    actual = ""
                if not actual:
                    try:
                        actual = (await field.locator.input_value()) or ""
                    except Exception:
                        actual = ""
                actual = actual.strip()
                if not actual:
                    return (None, "(unreadable)")  # custom widget — don't false-flag
                al = actual.lower()
                ok = (want in al or al in want or self._option_matches_value(want, al))
                return (ok, actual[:40])

            # TEXT / NUMERIC / TEXTAREA
            actual = ""
            try:
                actual = (await field.locator.input_value()) or ""
            except Exception:
                actual = ""
            actual = actual.strip()
            if not actual:
                return (False, "(empty)")
            al = actual.lower()
            ok = (want == al or want in al or al in want)
            return (ok, actual[:40])
        except Exception:
            return (None, "")  # never false-flag on a read error

    async def _resolve_nav_button_llm(self, listing, allow_submit: bool):
        """Dynamic fallback for a missing Next/Submit button.

        LinkedIn obfuscates its markup and sometimes renders the footer button
        late, so the deterministic selectors (and the class-based _primary_action)
        can miss it. Here we read the modal's actual visible button labels and
        ask ChatGPT which one advances/submits the application, then click it —
        resolving the button dynamically instead of giving up.

        Returns (action, label): action is "submit" | "next" | "".
        Records the assist (stage='nav_assist') so it shows on the dashboard.
        """
        # A late-rendering footer button is common — give it a moment first.
        await random_delay(1.0, 2.0)
        modal = await self._easy_apply_modal()
        scope = modal if modal is not None else self._page
        labels = await self._collect_button_labels(scope)
        if not labels:
            return "", ""

        llm = getattr(self._solver, "_llm", None)
        choice = ""
        via = "heuristic"
        if llm:
            try:
                summary = await self._page_text_summary(scope)
                choice = llm.choose_next_action(
                    labels, listing.title, listing.company,
                    page_summary=summary, allow_submit=allow_submit,
                )
                via = "chatgpt"
            except Exception as e:
                log.warning("apply.llm_nav_error", error=str(e)[:100])
        if not choice:
            choice = self._heuristic_next_button(labels, allow_submit=allow_submit)
            via = "heuristic"
        if not choice:
            return "", ""

        is_submit = any(k in choice.lower()
                        for k in ("submit application", "submit", "send application",
                                  "finish"))
        # SAFETY: never CLICK a terminal/submit button when submission isn't
        # allowed (dry-run, or external_apply.submit=false). Return the decision
        # WITHOUT clicking so the caller records DRY_RUN instead of submitting.
        if is_submit and not allow_submit:
            log.info("apply.dynamic_button_hold", chosen=choice,
                     reason="submit not allowed (dry-run)")
            return "submit", choice
        log.info("apply.dynamic_button", chosen=choice, via=via,
                 is_submit=is_submit, buttons=labels[:8])
        # Indicate on the dashboard that a dynamic (AI-assisted) resolution ran.
        try:
            self._db.log_error(
                stage="nav_assist", error_type="dynamic_button",
                run_id=self._run_id,
                message=(f"Resolved the '{choice}' button via {via} "
                         f"(no deterministic Next/Submit match); options were "
                         f"{labels[:6]}"),
                company=getattr(listing, "company", ""),
                job_title=getattr(listing, "title", ""),
                job_url=getattr(listing, "url", ""),
            )
        except Exception:
            pass

        await self._click_button_by_text(scope, choice)
        return ("submit" if is_submit else "next"), choice

    async def _primary_action(self):
        """Classify the Easy Apply modal's PRIMARY footer button.

        Returns ("submit"|"next", locator) or (None, None). This is the robust
        fallback when text/aria selectors miss LinkedIn's button — the footer
        primary button always exists on a valid step.
        """
        modal = await self._easy_apply_modal()
        root = modal if modal is not None else self._page
        selectors = (
            'button[data-easy-apply-next-button], '
            'button[data-live-test-easy-apply-next-button], '
            '.artdeco-modal__actionbar button.artdeco-button--primary, '
            'footer button.artdeco-button--primary, '
            'button.artdeco-button--primary'
        )
        try:
            btns = root.locator(selectors)
            n = await btns.count()
        except Exception:
            return None, None
        for i in range(min(n, 6)):
            b = btns.nth(i)
            try:
                if not await b.is_visible():
                    continue
                label = ((await b.get_attribute("aria-label")) or
                         (await b.text_content()) or "").lower()
                if any(k in label for k in ("submit", "send application", "finish")):
                    return "submit", b
                # anything else primary advances the form
                return "next", b
            except Exception:
                continue

        # Class-based lookup found nothing (LinkedIn obfuscates classes). Fall
        # back to classifying the modal's buttons BY TEXT — the reliable signal.
        try:
            all_btns = root.locator("button")
            m = await all_btns.count()
        except Exception:
            return None, None
        avoid = ("cancel", "back", "previous", "dismiss", "discard", "close",
                 "not now", "sign out", "log out", "save draft", "withdraw")
        submit_kw = ("submit application", "submit", "send application", "finish")
        forward_kw = ("review your application", "review", "next", "continue",
                      "save and continue", "proceed")
        found_next = None
        for i in range(min(m, 25)):
            b = all_btns.nth(i)
            try:
                if not await b.is_visible():
                    continue
                label = ((await b.get_attribute("aria-label")) or
                         (await b.text_content()) or "").strip().lower()
                if not label or any(a in label for a in avoid):
                    continue
                if any(k in label for k in submit_kw):
                    return "submit", b            # submit wins immediately
                if found_next is None and any(k in label for k in forward_kw):
                    found_next = b
            except Exception:
                continue
        if found_next is not None:
            return "next", found_next
        return None, None

    async def _nav_scope(self):
        """Scope Next/Submit lookups to the Easy Apply modal so stray page-wide
        buttons (page chrome behind the modal) can't be matched."""
        modal = await self._easy_apply_modal()
        return modal if modal is not None else self._page

    async def _is_review_step(self) -> bool:
        """Check if the current step has a Submit/Review button (modal-scoped)."""
        scope = await self._nav_scope()
        return await scope.locator(SUBMIT_SELECTOR).count() > 0

    async def _has_next_button(self) -> bool:
        scope = await self._nav_scope()
        return await scope.locator(NEXT_SELECTOR).count() > 0

    async def _click_next(self) -> None:
        scope = await self._nav_scope()
        next_btn = scope.locator(NEXT_SELECTOR)
        if await next_btn.count() > 0:
            await self._scroll_and_click(next_btn.first)

    async def _click_submit(self) -> None:
        scope = await self._nav_scope()
        submit_btn = scope.locator(SUBMIT_SELECTOR)
        if await submit_btn.count() > 0:
            await self._scroll_and_click(submit_btn.first)

    async def _scroll_and_click(self, locator) -> None:
        """Scroll a control into view (in case it's below the fold) then click."""
        try:
            await locator.scroll_into_view_if_needed(timeout=4000)
            await random_delay(0.2, 0.6)
        except Exception:
            pass
        await locator.click()

    async def _dismiss_modal(self) -> None:
        """Try to close the Easy Apply modal if it's still open."""
        try:
            dismiss = self._page.locator(
                'button[aria-label="Dismiss"], '
                'button[aria-label*="close"], '
                'button[aria-label*="Close"]'
            )
            if await dismiss.count() > 0:
                await dismiss.first.click()
                await random_delay(0.5, 1.0)

            # Handle "Discard application?" confirmation
            discard = self._page.locator('button:has-text("Discard"), button[data-control-name="discard_application_confirm_btn"]')
            if await discard.count() > 0:
                await discard.first.click()
        except Exception:
            pass

    async def _maybe_screenshot(self, listing: JobListing, suffix: str = "") -> None:
        """Capture a screenshot of the current form step if enabled. Pass a
        suffix (e.g. "filled") to capture the state AFTER the fields are answered,
        so a reviewer can see the actual selections (radios ticked, etc.)."""
        if not self._config.bot.get("capture_screenshots", False):
            return
        if not suffix:
            self._screenshot_count += 1
        ss_dir = Path(self._config.bot.get("screenshots_dir", "screenshots"))
        ss_dir.mkdir(parents=True, exist_ok=True)
        safe_company = "".join(c if c.isalnum() else "_" for c in listing.company)
        tag = f"step{self._screenshot_count}" + (f"_{suffix}" if suffix else "")
        filename = f"{safe_company}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{tag}.png"
        ss_path = str(ss_dir / filename)
        try:
            await self._page.screenshot(path=ss_path)
            self._screenshot_paths.append(ss_path)
            log.debug("apply.screenshot", path=ss_path)
        except Exception as e:
            log.warning("apply.screenshot_failed", error=str(e), path=ss_path)

    def _make_record(
        self,
        listing: JobListing,
        status: ApplicationStatus,
        notes: str = "",
    ) -> ApplicationRecord:
        has_review_items = any(q.needs_review for q in self._questions)
        final_status = status
        if status == ApplicationStatus.APPLIED and has_review_items:
            final_status = ApplicationStatus.NEEDS_REVIEW

        jd = listing.description
        if len(jd) > 5000:
            log.debug(
                "apply.jd_truncated",
                original_len=len(jd),
                truncated_to=5000,
                company=listing.company,
            )
            jd = jd[:5000]

        return ApplicationRecord(
            job_url=listing.url,
            job_title=listing.title,
            company=listing.company,
            location=listing.location,
            job_description=jd,
            status=final_status.value,
            match_score=listing.match_score,
            applied_at=datetime.utcnow().isoformat(),
            notes=notes,
            screenshot_path=";".join(self._screenshot_paths) if self._screenshot_paths else "",
            questions=self._questions,
        )
