"""Application flow controller — walks through Easy Apply multi-step forms."""

from __future__ import annotations

import os
from datetime import datetime, timezone
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
            if await easy_apply_btn.count() == 0:
                log.warning("apply.no_easy_apply_button", url=listing.url)
                return self._make_record(listing, ApplicationStatus.SKIPPED, "No Easy Apply button found")

            await easy_apply_btn.first.click()
            await random_delay(2.0, 4.0)

            # Walk through multi-step form
            max_steps = 15
            for step in range(max_steps):
                log.info("apply.form_step", step=step + 1, title=listing.title)

                # Capture screenshot if enabled
                await self._maybe_screenshot(listing)

                # Detect and fill all fields on this step
                fields = await detect_form_fields(self._page)
                await self._fill_fields(fields, listing)

                await random_delay(1.0, 2.5)

                # Check if this is the final step (Submit button) or Next
                if await self._is_review_step():
                    if dry_run:
                        log.info("apply.dry_run_skip", company=listing.company)
                        await self._dismiss_modal()
                        return self._make_record(listing, ApplicationStatus.DRY_RUN, "Dry run — not submitted")

                    await self._click_submit()
                    await random_delay(2.0, 4.0)
                    log.info("apply.submitted", company=listing.company, title=listing.title)
                    return self._make_record(listing, ApplicationStatus.APPLIED)

                elif await self._has_next_button():
                    await self._click_next()
                    await random_delay(1.5, 3.0)
                else:
                    # No next or submit — might be an error state
                    log.warning("apply.no_navigation", step=step)
                    break

            log.warning("apply.max_steps_exceeded", title=listing.title)
            await self._dismiss_modal()
            return self._make_record(listing, ApplicationStatus.FAILED, "Max form steps exceeded")

        except Exception as e:
            log.error("apply.error", error=str(e), title=listing.title)
            await self._dismiss_modal()
            return self._make_record(listing, ApplicationStatus.FAILED, str(e))

    async def _fill_fields(self, fields: list[FormField], listing: JobListing) -> None:
        """Fill all detected fields using the hybrid solver."""
        for field in fields:
            # Skip if already has a value (pre-filled by LinkedIn)
            if field.current_value and field.field_type not in (FieldType.FILE_UPLOAD,):
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
                await field.locator.fill(str(value))

            case FieldType.TEXTAREA:
                await field.locator.fill("")
                await field.locator.fill(str(value))

            case FieldType.DROPDOWN:
                await field.locator.select_option(label=str(value))

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

    async def _is_review_step(self) -> bool:
        """Check if the current step has a Submit/Review button."""
        submit_btn = self._page.locator(
            'button[aria-label*="Submit"], '
            'button:has-text("Submit application"), '
            'button:has-text("Submit")'
        )
        return await submit_btn.count() > 0

    async def _has_next_button(self) -> bool:
        next_btn = self._page.locator(
            'button[aria-label*="Continue"], '
            'button[aria-label*="Next"], '
            'button:has-text("Next"), '
            'button:has-text("Continue"), '
            'button:has-text("Review")'
        )
        return await next_btn.count() > 0

    async def _click_next(self) -> None:
        next_btn = self._page.locator(
            'button[aria-label*="Continue"], '
            'button[aria-label*="Next"], '
            'button:has-text("Next"), '
            'button:has-text("Continue"), '
            'button:has-text("Review")'
        )
        if await next_btn.count() > 0:
            await next_btn.first.click()

    async def _click_submit(self) -> None:
        submit_btn = self._page.locator(
            'button[aria-label*="Submit"], '
            'button:has-text("Submit application"), '
            'button:has-text("Submit")'
        )
        if await submit_btn.count() > 0:
            await submit_btn.first.click()

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
        filename = f"{safe_company}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_step{self._screenshot_count}.png"
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
            applied_at=datetime.now(timezone.utc).isoformat(),
            notes=notes,
            screenshot_path=";".join(self._screenshot_paths) if self._screenshot_paths else "",
            questions=self._questions,
        )
