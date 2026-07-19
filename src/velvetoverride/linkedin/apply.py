"""Application flow controller — walks through Easy Apply multi-step forms."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from velvetoverride.browser.stealth import random_delay
from velvetoverride.linkedin.fields import FormField, detect_form_fields
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

            try:
                await easy_apply_btn.first.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            await easy_apply_btn.first.click()
            await random_delay(2.0, 4.0)

            # Walk through multi-step form
            max_steps = int(self._config.bot.get("max_form_steps", 20))
            max_stuck = int(self._config.bot.get("max_stuck_retries", 4))
            last_signature = None
            stuck_count = 0
            last_errors: list[str] = []

            for step in range(max_steps):
                log.info("apply.form_step", step=step + 1, title=listing.title)

                # Capture screenshot if enabled
                await self._maybe_screenshot(listing)

                # Detect and fill all fields on this step — scoped to the Easy
                # Apply modal so we don't touch page chrome ("Set alert" etc.)
                modal = await self._easy_apply_modal()
                if modal is None:
                    # SAFETY: detect_form_fields treats scope=None as "scan the
                    # whole page". On LinkedIn that means the bot would start
                    # clicking the user's OWN account UI — the messaging drawer's
                    # conversation checkboxes, etc. Never do that: abort instead.
                    if await self._submission_confirmed():
                        log.info("apply.submitted", company=listing.company, title=listing.title)
                        return self._make_record(listing, ApplicationStatus.APPLIED)
                    log.error("apply.modal_missing", step=step + 1, company=listing.company,
                              title=listing.title,
                              msg="Easy Apply modal not found — aborting rather than scanning the page")
                    await self._dismiss_modal()
                    return self._make_record(
                        listing, ApplicationStatus.FAILED,
                        f"Easy Apply modal not found at step {step + 1} — aborted "
                        "(refused to scan the full page)",
                    )
                fields = await detect_form_fields(self._page, scope=modal)
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

                await random_delay(0.6, 1.2)

                # Detect a form that isn't advancing (same step + same errors)
                signature = (
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
                    if stuck_count >= max_stuck:
                        reason = (
                            f"Stuck on form step {step + 1}: {last_errors[0]}"
                            if last_errors else
                            f"Stuck on form step {step + 1} (form not advancing)"
                        )
                        log.error("apply.form_stuck_giveup", reason=reason)
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
                        # Truly no navigation — log the actual buttons for diagnosis
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

    async def _easy_apply_modal(self):
        """Return a locator for the Easy Apply modal container, or None."""
        modal = self._page.locator(
            'div[data-test-modal-id="easy-apply-modal"], '
            '.jobs-easy-apply-modal, '
            'div[role="dialog"].artdeco-modal'
        )
        try:
            if await modal.count() > 0:
                return modal.first
        except Exception:
            pass
        return None

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
        """Detect that an Easy Apply submission actually completed.

        LinkedIn shows a "Your application was sent" / "Application submitted"
        post-apply modal, and the Submit/Review buttons disappear. Returns True
        if we see a confirmation OR the apply form is gone.
        """
        try:
            body = (await self._page.locator("body").inner_text(timeout=3000) or "").lower()
        except Exception:
            body = ""
        markers = (
            "application sent", "your application was sent", "application submitted",
            "applied", "your application has been submitted", "premium",
        )
        # "premium" alone is too weak; require a real confirmation phrase
        confirm_phrases = [m for m in markers if m != "premium"]
        if any(m in body for m in confirm_phrases):
            return True
        # Or: the Easy Apply modal / submit button is gone (form closed on submit)
        try:
            modal = self._page.locator('div[data-test-modal-id="easy-apply-modal"]')
            submit = self._page.locator(SUBMIT_SELECTOR)
            if await modal.count() == 0 and await submit.count() == 0:
                return True
        except Exception:
            pass
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
        forward = ["next", "continue", "save and continue", "review", "proceed", "start application"]
        submit = ["submit application", "submit", "send application", "finish", "apply"]
        avoid = ["cancel", "back", "sign out", "save draft", "log out"]
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
                is_checked = await field.locator.is_checked()
                if should_check != is_checked:
                    await field.locator.click()

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
            best = None
            for i in range(await options.count()):
                opt = options.nth(i)
                text = (await opt.text_content() or "").strip()
                tl = text.lower()
                if not tl:
                    continue
                if value_l == tl or value_l in tl or tl in value_l:
                    best = text
                    break
            if best is not None:
                await field.locator.select_option(label=best, timeout=3000)
                return
        except Exception:
            pass
        # 3) Last resort: first non-placeholder option
        try:
            await field.locator.select_option(index=1, timeout=3000)
        except Exception as e:
            log.warning("apply.dropdown_unresolved", label=field.label, value=value, error=str(e)[:80])

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
            radio_id = await radio.get_attribute("id")
            if radio_id:
                label = self._page.locator(f'label[for="{radio_id}"]')
                if await label.count() > 0:
                    label_text = (await label.text_content() or "").strip().lower()
                    if value_lower in label_text or label_text in value_lower:
                        await label.click()
                        return

            # Try matching by value attribute
            radio_value = (await radio.get_attribute("value") or "").lower()
            if value_lower in radio_value or radio_value in value_lower:
                await radio.click()
                return

        # Fallback: click first option if it's a yes/no and value suggests yes
        if value_lower in ("yes", "true") and await radios.count() > 0:
            await radios.first.click()

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
        return None, None

    async def _is_review_step(self) -> bool:
        """Check if the current step has a Submit/Review button."""
        submit_btn = self._page.locator(SUBMIT_SELECTOR)
        return await submit_btn.count() > 0

    async def _has_next_button(self) -> bool:
        next_btn = self._page.locator(NEXT_SELECTOR)
        return await next_btn.count() > 0

    async def _click_next(self) -> None:
        next_btn = self._page.locator(NEXT_SELECTOR)
        if await next_btn.count() > 0:
            await self._scroll_and_click(next_btn.first)

    async def _click_submit(self) -> None:
        submit_btn = self._page.locator(SUBMIT_SELECTOR)
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

    async def _maybe_screenshot(self, listing: JobListing) -> None:
        """Capture a screenshot of the current form step if enabled."""
        if not self._config.bot.get("capture_screenshots", False):
            return
        self._screenshot_count += 1
        ss_dir = Path(self._config.bot.get("screenshots_dir", "screenshots"))
        ss_dir.mkdir(parents=True, exist_ok=True)
        safe_company = "".join(c if c.isalnum() else "_" for c in listing.company)
        filename = f"{safe_company}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_step{self._screenshot_count}.png"
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
