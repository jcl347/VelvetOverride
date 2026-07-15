"""CLI entry point — orchestrates the full application pipeline."""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from pathlib import Path

import click
import yaml

from velvetoverride.utils.config import load_config
from velvetoverride.utils.logging import get_logger, setup_logging

log = get_logger(__name__)


async def run_bot(
    config_dir: str | None = None,
    dry_run: bool | None = None,
    max_apps_override: int | None = None,
    min_salary: int | None = None,
    max_salary: int | None = None,
    keep_open: bool | None = None,
    keywords: list[str] | None = None,
    locations: list[str] | None = None,
    experience: list[str] | None = None,
) -> None:
    """Main bot orchestration loop."""
    config = load_config(Path(config_dir) if config_dir else None)

    if dry_run is not None:
        config.settings.setdefault("bot", {})["dry_run"] = dry_run

    # Search overrides: CLI target roles / locations / experience take
    # precedence over settings.yaml so runs can be pointed on the fly.
    _apply_search_overrides(config, keywords, locations, experience)

    # SAFETY GUARD: never apply with the committed placeholder identity.
    # (config/profile.yaml is a template; your real data belongs in
    # config/profile.local.yaml.) This prevents submitting as "Jane Doe".
    placeholder_reason = _placeholder_profile_reason(config)
    if placeholder_reason:
        raise ValueError(
            "Refusing to run: the profile still looks like the committed "
            f"placeholder ({placeholder_reason}). Copy it to a real local file:\n"
            "  cp config/profile.yaml config/profile.local.yaml\n"
            "then edit config/profile.local.yaml with your real details."
        )

    # Whether to leave the browser window open when the run ends
    if keep_open is None:
        keep_open = config.browser.get("keep_open", False)

    is_dry_run = config.bot.get("dry_run", True)
    # Use `is not None` so an explicit 0 override is honored (not treated as unset)
    max_apps = max_apps_override if max_apps_override is not None else config.bot.get("max_applications", 25)

    # Salary filter: CLI overrides take precedence, then settings.yaml
    salary_cfg = config.settings.get("salary", {})
    effective_min_salary = min_salary if min_salary is not None else salary_cfg.get("min_annual")
    effective_max_salary = max_salary if max_salary is not None else salary_cfg.get("max_annual")

    log.info(
        "bot.starting",
        dry_run=is_dry_run,
        max_applications=max_apps,
        min_salary=effective_min_salary,
        max_salary=effective_max_salary,
        keywords=config.search.get("keywords", []),
        locations=config.search.get("locations", []),
    )

    # Late imports to avoid loading heavy deps at CLI parse time
    from velvetoverride.agent.field_solver import FieldSolver
    from velvetoverride.agent.llm import LLMClient
    from velvetoverride.agent.resume_tailor import ResumeTailor
    from velvetoverride.agent.salary import extract_salary, salary_in_range
    from velvetoverride.browser.captcha import detect_captcha, handle_captcha
    from velvetoverride.browser.engine import BrowserEngine
    from velvetoverride.browser.stealth import diversify_activity, random_delay
    from velvetoverride.linkedin.apply import ApplicationFlow
    from velvetoverride.linkedin.auth import login
    from velvetoverride.linkedin.search import scrape_job_listings
    from velvetoverride.resume.builder import ResumeBuilder
    from velvetoverride.resume.scorer import ATSScorer
    from velvetoverride.tracking.database import TrackingDB
    from velvetoverride.tracking.models import ApplicationStatus

    # ── Initialize components ──
    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()

    # Start run tracking — snapshot the effective search config (the dashboard
    # reads this back to show what a run searched for).
    config_snapshot = json.dumps({
        "dry_run": is_dry_run,
        "keywords": config.search.get("keywords", []),
        "locations": config.search.get("locations", []),
        "date_posted": config.search.get("date_posted", ""),
        "remote": config.search.get("remote", []),
        "experience_levels": config.search.get("experience_levels", []),
        "easy_apply_only": config.search.get("easy_apply_only", True),
        "external_apply": config.bot.get("external_apply", True),
        "min_salary": effective_min_salary,
        "max_salary": effective_max_salary,
        "fit": config.settings.get("fit", {}),
    }, default=str)
    run_id = db.start_run(
        dry_run=is_dry_run,
        max_apps=max_apps,
        min_salary=effective_min_salary,
        max_salary=effective_max_salary,
        config_snapshot=config_snapshot,
    )

    llm: LLMClient | None = None
    if config.has_llm:
        llm = LLMClient(config)
        log.info("bot.llm_ready", provider=config.llm_provider)
    else:
        log.warning("bot.no_api_key", msg="Running without LLM — config-only answers")

    field_solver = FieldSolver(config, llm)

    resume_builder = ResumeBuilder(
        template_name=config.resume_config.get("template", "default"),
        output_dir=Path("data/resumes"),
        output_format=config.resume_config.get("output_format", "html"),
    )
    ats_scorer = ATSScorer(
        target_coverage=config.resume_config.get("target_keyword_coverage", 0.70)
    )

    resume_tailor: ResumeTailor | None = None
    if llm:
        resume_tailor = ResumeTailor(config, llm, resume_builder, ats_scorer)

    browser = BrowserEngine(config)

    # Counters for run tracking
    applied_count = 0
    failed_count = 0
    skipped_count = 0
    listings_found = 0
    listings_after_filter = 0

    try:
        # ── Launch browser & authenticate ──
        page = await browser.launch()
        if not await login(page, config):
            log.error("bot.login_failed")
            db.finish_run(run_id, status="failed", error_message="Login failed")
            return

        # ── Check for CAPTCHA after login ──
        if await detect_captcha(page):
            log.warning("bot.captcha_after_login")
            if not await handle_captcha(page, config):
                log.error("bot.captcha_unresolved")
                db.finish_run(run_id, status="failed", error_message="CAPTCHA unresolved")
                return

        # ── Search for jobs ──
        listings = await scrape_job_listings(page, config)
        listings_found = len(listings)
        log.info("bot.listings_found", count=listings_found)

        if not listings:
            log.warning("bot.no_listings")
            db.finish_run(run_id, status="completed", listings_found=0, error_message="No listings found")
            return

        # ── Score and sort listings by match ──
        # Scoring is pure keyword overlap (no LLM), so it runs unconditionally.
        # Descriptions are usually empty at this stage (fetched per-job later),
        # so this is a coarse pre-sort; the real min_match_score gate is applied
        # after the full JD is fetched in the apply loop.
        min_score = config.search.get("min_match_score", 0)
        if listings:
            listings = _score_listings(listings, config, llm)
            listings.sort(key=lambda l: l.match_score, reverse=True)
            log.info("bot.after_scoring", count=len(listings))

        # ── Extract salary ranges and filter ──
        if effective_min_salary or effective_max_salary:
            pre_filter = len(listings)
            filtered = []
            for listing in listings:
                salary = extract_salary(listing.description)
                if salary:
                    listing.salary_min = salary.annual_min
                    listing.salary_max = salary.annual_max
                    listing.salary_raw = salary.raw_text
                if salary_in_range(salary, effective_min_salary, effective_max_salary):
                    filtered.append(listing)
                else:
                    log.info(
                        "bot.salary_filtered",
                        title=listing.title,
                        company=listing.company,
                        salary=str(salary) if salary else "unknown",
                    )
            listings = filtered
            log.info(
                "bot.after_salary_filter",
                before=pre_filter,
                after=len(listings),
                min_salary=effective_min_salary,
                max_salary=effective_max_salary,
            )
        else:
            # Still extract salary info for tracking even without filtering
            for listing in listings:
                salary = extract_salary(listing.description)
                if salary:
                    listing.salary_min = salary.annual_min
                    listing.salary_max = salary.annual_max
                    listing.salary_raw = salary.raw_text

        listings_after_filter = len(listings)

        # ── Base resume for fallback (real, profile-based — never a test file) ──
        default_resume = _ensure_base_resume(config, resume_builder) or _find_default_resume(config)

        # ── Apply to each listing ──
        app_flow = ApplicationFlow(page, config, field_solver, db)

        for i, listing in enumerate(listings):
            if applied_count >= max_apps:
                log.info("bot.max_reached", count=applied_count)
                break

            # Deduplication — by canonical LinkedIn job ID, then normalized URL
            if db.is_already_applied(listing.url, listing.job_id):
                log.info("bot.skip_duplicate", url=listing.url, job_id=listing.job_id)
                skipped_count += 1
                continue

            # Fuzzy dedup by title + company. Only successfully-applied jobs
            # block a match — FAILED/SKIPPED rows must remain retryable, matching
            # is_already_applied's intent.
            similar = db.find_similar_jobs(listing.title, listing.company)
            is_fuzzy_dup = False
            if similar:
                from thefuzz import fuzz
                for s in similar:
                    if s.status in ("failed", "skipped"):
                        continue
                    if fuzz.ratio(listing.title.lower(), s.job_title.lower()) > 85:
                        log.info(
                            "bot.skip_fuzzy_dup",
                            title=listing.title,
                            existing=s.job_title,
                        )
                        is_fuzzy_dup = True
                        break
            if is_fuzzy_dup:
                skipped_count += 1
                continue

            # Title-level seniority exclusion (e.g. skip "Principal" roles) while
            # still searching all experience levels.
            excluded = _title_excluded(
                listing.title, config.search.get("blacklist_title_keywords", [])
            )
            if excluded:
                log.info("bot.skip_title_excluded", title=listing.title, matched=excluded)
                skipped_count += 1
                continue

            log.info(
                "bot.applying",
                index=i + 1,
                total=len(listings),
                title=listing.title,
                company=listing.company,
                score=f"{listing.match_score:.0f}",
            )

            # Ensure we have the full job description before tailoring — the
            # list-scrape panel is unreliable, so fetch it directly if empty.
            if not listing.description or len(listing.description) < 50:
                from velvetoverride.linkedin.search import get_job_description
                try:
                    jd = await get_job_description(page, listing.url)
                    if jd:
                        listing.description = jd
                        # Re-extract salary now that we have the JD
                        salary = extract_salary(jd)
                        if salary:
                            listing.salary_min = salary.annual_min
                            listing.salary_max = salary.annual_max
                            listing.salary_raw = salary.raw_text
                        # Recompute the match score against the real JD (no LLM)
                        _score_listings([listing], config, llm)
                        log.info("bot.rescored", title=listing.title, score=f"{listing.match_score:.0f}")
                except Exception as e:
                    log.warning("bot.jd_fetch_error", error=str(e), url=listing.url)
                    if _is_browser_closed(e):
                        log.error("bot.browser_closed_aborting",
                                  remaining=len(listings) - i - 1)
                        break

            # ── Re-apply match + salary gates now that we have the full JD ──
            # (the initial pass ran against an empty description).
            if listing.description:
                if listing.match_score < min_score:
                    log.info("bot.skip_low_match", title=listing.title,
                             score=f"{listing.match_score:.0f}", min=min_score)
                    skipped_count += 1
                    continue
                if (effective_min_salary or effective_max_salary):
                    salary = extract_salary(listing.description)
                    if not salary_in_range(salary, effective_min_salary, effective_max_salary):
                        log.info("bot.skip_salary", title=listing.title,
                                 salary=str(salary) if salary else "unknown")
                        skipped_count += 1
                        continue

            # JD-keyword blacklist (applied now that we have the full description)
            blacklist_kw = [k.lower() for k in config.search.get("blacklist_keywords", [])]
            if listing.description and any(k in listing.description.lower() for k in blacklist_kw):
                log.info("bot.skip_blacklisted_keyword", title=listing.title, company=listing.company)
                skipped_count += 1
                continue

            # ── ChatGPT experience-fit check (am I really a lead?) ──
            fit_cfg = config.settings.get("fit", {})
            if llm and fit_cfg.get("enabled", True) and listing.description:
                try:
                    verdict = llm.evaluate_fit(
                        listing.title, listing.company, listing.description,
                        _fit_profile_summary(config),
                    )
                    listing.fit_score = verdict["fit_score"]
                    listing.fit_seniority = verdict["seniority"]
                    listing.fit_recommend = verdict["recommend"]
                    listing.fit_reasoning = verdict["reasoning"]
                    listing.fit_gaps = "; ".join(verdict["gaps"])
                    log.info(
                        "bot.fit", title=listing.title, score=verdict["fit_score"],
                        seniority=verdict["seniority"], recommend=verdict["recommend"],
                        gaps=verdict["gaps"][:3],
                    )

                    min_fit = fit_cfg.get("min_score", 0) or 0
                    skip_recs = set(fit_cfg.get("skip_recommendations", []) or [])
                    reason = ""
                    if verdict["fit_score"] < min_fit:
                        reason = f"fit {verdict['fit_score']} < min {min_fit}"
                    elif verdict["recommend"] in skip_recs:
                        reason = f"fit recommend={verdict['recommend']}"

                    if reason:
                        log.info("bot.skip_poor_fit", title=listing.title,
                                 company=listing.company, reason=reason,
                                 why=verdict["reasoning"][:120])
                        db.log_error(
                            stage="fit_skip", run_id=run_id, error_type="poor_fit",
                            message=f"{reason} — {verdict['reasoning']}",
                            company=listing.company, job_title=listing.title,
                            job_url=listing.url,
                        )
                        skipped_count += 1
                        continue
                except Exception as e:
                    log.warning("bot.fit_error", error=str(e), title=listing.title)

            # ── Choose the resume for this job ──
            # resume.mode == "static": always upload the user's own file.
            # resume.mode == "tailored": generate a per-job PDF (fallback to the
            # user's static file / base resume if generation fails).
            resume_mode = config.resume_config.get("mode", "tailored")
            resume_path = None
            if resume_mode == "static":
                resume_path = _static_resume(config) or default_resume
                if resume_path:
                    log.info("bot.resume_static", path=resume_path)
                else:
                    log.warning("bot.resume_static_missing",
                                msg="resume.mode=static but no static_resume_path/base resume found")
            elif resume_tailor and listing.description:
                try:
                    resume_path, ats_score, _ = resume_tailor.tailor_resume(
                        listing.title, listing.company, listing.description
                    )
                    log.info("bot.resume_tailored", ats_score=f"{ats_score:.0f}")
                except Exception as e:
                    log.warning("bot.resume_tailor_error", error=str(e))
                    db.log_error(
                        stage="resume_tailor", message=str(e), run_id=run_id,
                        error_type=type(e).__name__, company=listing.company,
                        job_title=listing.title, job_url=listing.url,
                    )
                    resume_path = _static_resume(config) or default_resume
                    if resume_path:
                        log.info("bot.resume_fallback", path=resume_path)

            if not resume_path:
                resume_path = _static_resume(config) or default_resume

            # Apply
            try:
                record = await app_flow.apply_to_job(listing, resume_path)
            except Exception as e:
                log.error("bot.apply_error", error=str(e), title=listing.title)
                db.log_error(
                    stage="apply", message=str(e), run_id=run_id,
                    error_type=type(e).__name__, company=listing.company,
                    job_title=listing.title, job_url=listing.url,
                )
                failed_count += 1
                # If the browser/context is gone, every remaining listing will
                # fail identically — abort the loop instead of logging hundreds
                # of phantom failures.
                if _is_browser_closed(e):
                    log.error("bot.browser_closed_aborting",
                              remaining=len(listings) - i - 1)
                    break
                continue

            record.job_id = listing.job_id
            if resume_path:
                record.resume_version = resume_path
            record.salary_min = listing.salary_min
            record.salary_max = listing.salary_max
            record.salary_raw = listing.salary_raw
            record.fit_score = listing.fit_score
            record.fit_seniority = listing.fit_seniority
            record.fit_recommend = listing.fit_recommend
            record.fit_reasoning = listing.fit_reasoning
            record.fit_gaps = listing.fit_gaps
            try:
                db.save_application(record, run_id=run_id)
            except Exception as e:
                # Never let a tracking write abort the whole run
                log.error("bot.save_failed", error=str(e), company=listing.company)
                db.log_error(
                    stage="save_application", message=str(e), run_id=run_id,
                    error_type=type(e).__name__, company=listing.company,
                    job_title=listing.title, job_url=listing.url,
                )

            # Track per-job outcome on the dashboard so failures are reviewable
            if record.status in (
                ApplicationStatus.FAILED.value,
                ApplicationStatus.NEEDS_REVIEW.value,
                ApplicationStatus.SKIPPED.value,
            ):
                db.log_error(
                    stage="apply_outcome", run_id=run_id,
                    error_type=record.status,
                    message=record.notes or f"status={record.status}",
                    company=listing.company, job_title=listing.title, job_url=listing.url,
                )

            if record.status in (ApplicationStatus.APPLIED.value, ApplicationStatus.DRY_RUN.value, ApplicationStatus.NEEDS_REVIEW.value):
                applied_count += 1
            elif record.status == ApplicationStatus.FAILED.value:
                failed_count += 1
            elif record.status == ApplicationStatus.SKIPPED.value:
                skipped_count += 1

            # Human-like delay between applications
            delay_min = config.bot.get("delay_min", 5)
            delay_max = config.bot.get("delay_max", 18)
            await random_delay(delay_min, delay_max)

            # Occasionally diversify activity (best-effort — a transient nav
            # error here must not abort the whole run after a good application)
            if random.random() < 0.2:
                try:
                    await diversify_activity(page)
                except Exception as e:
                    log.debug("bot.diversify_error", error=str(e)[:80])

        # ── Export results ──
        from velvetoverride.tracking.export import export_csv, export_json, export_review_queue

        export_path = config.tracking.get("export_path", "data/")
        export_fmt = config.tracking.get("export_format", "both")

        if export_fmt in ("csv", "both"):
            export_csv(db, export_path)
        if export_fmt in ("json", "both"):
            export_json(db, export_path)
        export_review_queue(db, export_path)

        # ── Record LLM token usage for this run ──
        _record_usage(db, run_id, llm)

        # ── Field-routing feedback audit (flags mis-routed fields for review) ──
        try:
            audit_field_routing(db, run_id)
        except Exception as e:
            log.warning("bot.field_audit_error", error=str(e))

        # ── Finish run tracking ──
        db.finish_run(
            run_id,
            status="completed",
            listings_found=listings_found,
            listings_after_filter=listings_after_filter,
            applied_count=applied_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

        # ── Summary ──
        stats = db.get_stats()
        log.info(
            "bot.complete",
            applied=applied_count,
            failed=failed_count,
            skipped=skipped_count,
            total_applications=stats["total_applications"],
        )

    except Exception as e:
        log.error("bot.fatal_error", error=str(e))
        db.log_error(stage="fatal", message=str(e), run_id=run_id, error_type=type(e).__name__)
        _record_usage(db, run_id, llm)
        db.finish_run(
            run_id,
            status="failed",
            listings_found=listings_found,
            listings_after_filter=listings_after_filter,
            applied_count=applied_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            error_message=str(e),
        )
        raise
    finally:
        db.close()
        if keep_open:
            log.info("bot.keep_open", msg="Run finished — leaving the browser open. Press Ctrl-C to close.")
            try:
                await asyncio.Event().wait()  # keep the process (and window) alive
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        await browser.close()


# Known placeholder values from the committed config/profile.yaml template.
_PLACEHOLDER_NAMES = {("jane", "doe")}
_PLACEHOLDER_EMAILS = {"jane.doe@example.com", "you@example.com", "jane.doe@example"}


def _placeholder_profile_reason(config) -> str:
    """Return a reason string if the profile is still the template, else ''.

    Guards against ever applying with the committed placeholder identity.
    """
    p = config.personal
    email = str(p.get("email", "")).strip().lower()
    first = str(p.get("first_name", "")).strip().lower()
    last = str(p.get("last_name", "")).strip().lower()

    if email in _PLACEHOLDER_EMAILS:
        return f"email={email}"
    if (first, last) in _PLACEHOLDER_NAMES:
        return f"name={first} {last}"
    if not first or not email:
        return "missing name/email"
    return ""


_VALID_EXPERIENCE_LEVELS = (
    "internship", "entry_level", "associate", "mid_senior", "director", "executive",
)


def _apply_search_overrides(config, keywords, locations, experience=None) -> None:
    """Override settings.yaml search roles/locations/experience with CLI values."""
    if keywords:
        config.settings.setdefault("search", {})["keywords"] = list(keywords)
    if locations:
        config.settings.setdefault("search", {})["locations"] = list(locations)
    if experience:
        levels = [e.strip().lower() for e in experience if e.strip()]
        unknown = [e for e in levels if e not in _VALID_EXPERIENCE_LEVELS]
        if unknown:
            log.warning("bot.unknown_experience_levels", unknown=unknown,
                        valid=list(_VALID_EXPERIENCE_LEVELS))
        valid = [e for e in levels if e in _VALID_EXPERIENCE_LEVELS]
        if valid:
            config.settings.setdefault("search", {})["experience_levels"] = valid


def audit_field_routing(db, run_id: int) -> list[dict]:
    """Review every field->answer this run and flag likely mis-routings.

    This is the feedback loop: anomalies are marked needs_review and logged as
    errors (visible on the dashboard + `velvetoverride feedback`), so routing can
    be corrected iteratively (approve/correct feeds the learned-answer memory).
    """
    from velvetoverride.tracking.audit import flag_routing_problem

    issues: list[dict] = []
    for q in db.get_run_questions(run_id):
        problem = flag_routing_problem(
            q.get("question_text", ""), q.get("answer_given", ""),
            q.get("field_type", ""), q.get("answer_source", ""),
        )
        if problem:
            issues.append({**q, "problem": problem})
            try:
                db.flag_question(q["id"])
                db.log_error(
                    stage="field_audit", run_id=run_id, error_type="routing",
                    message=f"{(q.get('question_text') or '')[:50]!r} -> "
                            f"{(q.get('answer_given') or '')[:50]!r} : {problem}",
                    company=q.get("company", ""), job_title=q.get("job_title", ""),
                )
            except Exception as e:  # never let auditing break a run
                log.debug("bot.field_audit_log_failed", error=str(e)[:80])

    if issues:
        log.warning("bot.field_audit", flagged=len(issues),
                    examples=[i["problem"] for i in issues[:4]])
    else:
        log.info("bot.field_audit", flagged=0)
    return issues


def _record_usage(db, run_id, llm) -> None:
    """Persist LLM token usage for a run (no-op if no LLM was used)."""
    if not llm:
        return
    u = llm.usage
    db.record_run_usage(
        run_id,
        u.prompt_tokens,
        u.completion_tokens,
        u.total_tokens,
        u.calls,
        round(u.est_cost_usd, 4),
    )
    log.info(
        "bot.token_usage",
        total_tokens=u.total_tokens,
        calls=u.calls,
        est_cost_usd=round(u.est_cost_usd, 4),
    )


def _fit_profile_summary(config) -> str:
    """A factual career summary for the fit judgement.

    Includes real titles and dates so the model can judge seniority honestly
    rather than taking a job's title at face value.
    """
    profile = config.profile
    parts: list[str] = []

    summary = profile.get("summary", "")
    if summary:
        parts.append(f"Summary: {' '.join(str(summary).split())}")

    roles = []
    for job in profile.get("experience", []):
        roles.append(
            f"- {job.get('title','')} at {job.get('company','')} "
            f"({job.get('start_date','')} to {job.get('end_date','')})"
        )
    if roles:
        parts.append("Career history (most recent first):\n" + "\n".join(roles))

    tech = config.technology_experience
    if tech:
        years = ", ".join(
            f"{k}: {v}y" for k, v in tech.items() if k != "default"
        )
        parts.append(f"Years per technology: {years}")

    edu = profile.get("education", [])
    if edu:
        parts.append(
            "Education: "
            + "; ".join(
                f"{e.get('degree','')} in {e.get('field','')}, {e.get('institution','')}"
                for e in edu
            )
        )
    return "\n\n".join(parts)


def _ensure_base_resume(config, resume_builder) -> str | None:
    """Generate a real base resume from the profile (untailored) as a fallback.

    Named after the applicant so uploads are never a stray 'test' file.
    """
    try:
        profile = config.profile
        name = f"{config.personal.get('first_name','')}_{config.personal.get('last_name','')}".strip("_") or "Resume"
        from velvetoverride.agent.resume_tailor import _build_projects, _pretty_term

        experience = []
        for job in profile.get("experience", []):
            techs: list[str] = []
            for b in job.get("bullets", []):
                for s in b.get("skills", []) or []:
                    pretty = _pretty_term(s)
                    if pretty and pretty.lower() not in {t.lower() for t in techs}:
                        techs.append(pretty)
            experience.append({
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "start_date": job.get("start_date", ""),
                "end_date": job.get("end_date", ""),
                "location": job.get("location", ""),
                "bullets": [b.get("text", "") for b in job.get("bullets", [])],
                "technologies": techs[:8],
            })
        data = {
            "personal": profile.get("personal", {}),
            "summary": profile.get("summary", ""),
            "experience": experience,
            "projects": _build_projects(profile.get("projects", [])),
            "education": profile.get("education", []),
            "skills": {k: v for k, v in profile.get("skills", {}).items() if isinstance(v, list)},
            "certifications": profile.get("certifications", []),
            "target_keywords": [],
        }
        path = resume_builder.build(data, f"{name}_Resume")
        log.info("bot.base_resume_ready", path=path)
        return path
    except Exception as e:
        log.warning("bot.base_resume_failed", error=str(e))
        return None


_RESUME_UPLOAD_EXTS = {".pdf", ".doc", ".docx"}


def _static_resume(config) -> str | None:
    """The user's own resume file (resume.static_resume_path), if it exists and
    is an uploadable format."""
    static_path = config.resume_config.get("static_resume_path")
    if static_path:
        p = Path(static_path)
        if p.exists() and p.suffix.lower() in _RESUME_UPLOAD_EXTS:
            return str(p)
        log.warning("bot.static_resume_invalid", path=static_path,
                    exists=p.exists(), suffix=p.suffix)
    return None


def _find_default_resume(config) -> str | None:
    """Fallback resume: the user's own static resume first, else the most
    recently generated PDF. Never returns .html (LinkedIn rejects it)."""
    # Prefer the user's own configured resume over a stale generated one
    static = _static_resume(config)
    if static:
        return static

    resume_dir = Path("data/resumes")
    if resume_dir.exists():
        resumes = sorted(resume_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if resumes:
            return str(resumes[0])
    return None


def _title_excluded(title: str, blacklist: list[str]) -> str | None:
    """Return the matched keyword if the job TITLE contains a blacklisted
    seniority/word (word-boundary match, so 'lead' won't hit 'leadership' and
    'principal' won't hit unrelated substrings), else None.

    Used to exclude seniorities the user doesn't want (e.g. 'principal') while
    keeping all experience levels in the search.
    """
    tl = (title or "").lower()
    for kw in blacklist or []:
        k = str(kw).lower().strip()
        if k and re.search(r"\b" + re.escape(k) + r"\b", tl):
            return k
    return None


def _is_browser_closed(err: Exception) -> bool:
    """True if the exception means the browser/page/context is gone.

    Once the browser dies, every remaining listing fails identically, so the
    apply loop must abort rather than record hundreds of phantom failures.
    """
    msg = str(err).lower()
    return any(s in msg for s in (
        "target page, context or browser has been closed",
        "context or browser has been closed",
        "browser has been closed",
        "target closed",
        "page has been closed",
        "browser closed",
    ))


def _score_listings(listings, config, llm):
    """Score listings by job match quality using keyword overlap."""
    profile = config.profile
    all_skills = []
    for cat_skills in profile.get("skills", {}).values():
        if isinstance(cat_skills, list):
            all_skills.extend([s.lower() for s in cat_skills])

    # Add technology experience keys
    for tech in profile.get("technology_experience", {}).keys():
        if tech != "default":
            all_skills.append(tech.lower())

    for listing in listings:
        if not listing.description:
            listing.match_score = 50.0
            continue

        desc_lower = listing.description.lower()
        matched = sum(1 for s in all_skills if s.lower() in desc_lower)
        total = len(all_skills) if all_skills else 1
        listing.match_score = min((matched / total) * 200, 100)

    return listings


@click.group()
@click.option("--config-dir", default=None, help="Path to config directory")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option("--json-log", is_flag=True, help="Output logs as JSON")
@click.pass_context
def cli(ctx, config_dir, verbose, json_log):
    """VelvetOverride — Automated LinkedIn job application bot."""
    setup_logging(level="DEBUG" if verbose else "INFO", json_output=json_log)
    ctx.ensure_object(dict)
    ctx.obj["config_dir"] = config_dir


@cli.command()
@click.option("--dry-run/--live", default=None, help="Override dry_run setting")
@click.option("--max-apps", type=int, default=None, help="Max applications this run (overrides settings.yaml)")
@click.option("--min-salary", type=int, default=None, help="Minimum annual salary filter (e.g. 100000)")
@click.option("--max-salary", type=int, default=None, help="Maximum annual salary filter (e.g. 200000)")
@click.option("--keyword", "-k", "keywords", multiple=True,
              help="Target role to search (repeatable). Overrides settings.yaml search.keywords.")
@click.option("--location", "-l", "locations", multiple=True,
              help="Location to search (repeatable). Overrides settings.yaml search.locations.")
@click.option("--experience", "-x", "experience", multiple=True,
              help="Experience level to target (repeatable): internship, entry_level, "
                   "associate (mid), mid_senior, director, executive. "
                   "Overrides settings.yaml search.experience_levels.")
@click.option("--keep-open/--no-keep-open", default=None, help="Leave the browser open after the run")
@click.option("--loop", is_flag=True, help="Keep re-running passes until stopped (Ctrl-C)")
@click.option("--loop-delay", type=int, default=300, help="Seconds to wait between loop passes")
@click.option("--max-loops", type=int, default=0, help="Stop after N passes (0 = unlimited)")
@click.pass_context
def run(ctx, dry_run, max_apps, min_salary, max_salary, keywords, locations,
        experience, keep_open, loop, loop_delay, max_loops):
    """Run the full application bot pipeline (optionally on a continuous loop).

    Target roles, locations, and experience levels come from
    config/settings.yaml, but can be overridden here, e.g.:

        velvetoverride run --live -k "Software Engineer" -x associate -x mid_senior
    """
    kw = list(keywords) or None
    loc = list(locations) or None
    exp = list(experience) or None
    if loop:
        # keep_open would block forever, so force it off between passes
        asyncio.run(
            _run_loop(
                ctx.obj["config_dir"], dry_run, max_apps, min_salary, max_salary,
                loop_delay=loop_delay, max_loops=max_loops,
                keywords=kw, locations=loc, experience=exp,
            )
        )
    else:
        asyncio.run(run_bot(
            ctx.obj["config_dir"], dry_run, max_apps, min_salary, max_salary,
            keep_open, keywords=kw, locations=loc, experience=exp,
        ))


async def _run_loop(
    config_dir, dry_run, max_apps, min_salary, max_salary,
    loop_delay: int = 300, max_loops: int = 0,
    keywords: list[str] | None = None, locations: list[str] | None = None,
    experience: list[str] | None = None,
) -> None:
    """Re-run the pipeline continuously.

    Each pass re-searches (fresh 24h postings), skips jobs already successfully
    applied to, and retries ones that previously failed. A pass that crashes is
    logged and the loop continues.
    """
    pass_num = 0
    while True:
        pass_num += 1
        log.info("loop.pass_starting", pass_num=pass_num, max_loops=max_loops or "unlimited")
        try:
            await run_bot(
                config_dir, dry_run, max_apps, min_salary, max_salary,
                keep_open=False, keywords=keywords, locations=locations,
                experience=experience,
            )
        except KeyboardInterrupt:
            log.info("loop.interrupted", pass_num=pass_num)
            raise
        except Exception as e:
            log.error("loop.pass_failed", pass_num=pass_num, error=str(e))

        if max_loops and pass_num >= max_loops:
            log.info("loop.finished", passes=pass_num)
            return

        log.info("loop.sleeping", seconds=loop_delay, next_pass=pass_num + 1)
        try:
            await asyncio.sleep(loop_delay)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("loop.interrupted_during_sleep")
            return


@cli.command()
@click.pass_context
def stats(ctx):
    """Show application statistics."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)

    from velvetoverride.tracking.database import TrackingDB

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()
    s = db.get_stats()
    db.close()

    click.echo(f"\n{'='*60}")
    click.echo("  VelvetOverride — Application Statistics")
    click.echo(f"{'='*60}")

    # Application counts
    click.echo(f"\nTotal applications: {s['total_applications']}")
    click.echo("By status:")
    for status, count in s.get("by_status", {}).items():
        click.echo(f"  {status}: {count}")

    # Answer sources
    by_source = s.get("answers_by_source", {})
    if by_source:
        click.echo("\nAnswer sources:")
        for source, count in sorted(by_source.items(), key=lambda x: x[1], reverse=True):
            click.echo(f"  {source}: {count}")

    # Salary statistics
    salary = s.get("salary", {})
    if salary.get("with_salary", 0) > 0:
        click.echo("\nSalary data:")
        click.echo(f"  Jobs with salary: {salary['with_salary']}")
        click.echo(f"  Jobs without salary: {salary['without_salary']}")
        if salary.get("range_min"):
            click.echo(f"  Salary range: ${salary['range_min']:,} — ${salary['range_max']:,}")
        if salary.get("avg_min"):
            click.echo(f"  Average range: ${salary['avg_min']:,} — ${salary['avg_max']:,}")

    # Failure analysis
    failures = s.get("top_failure_reasons", [])
    if failures:
        click.echo("\nTop failure reasons:")
        for f in failures:
            click.echo(f"  {f['reason']}: {f['count']}x")

    # Recent runs
    runs = s.get("recent_runs", [])
    if runs:
        click.echo("\nRecent runs:")
        for r in runs:
            mode = "dry-run" if r.get("dry_run") else "live"
            status = r.get("status", "?")
            started = r.get("started_at", "?")[:19]
            applied = r.get("applied_count", 0)
            failed = r.get("failed_count", 0)
            click.echo(f"  {started} [{mode}] {status} — applied:{applied} failed:{failed}")

    # Token usage across recent runs
    total_tokens = sum((r.get("total_tokens") or 0) for r in s.get("recent_runs", []))
    if total_tokens:
        click.echo("\nLLM token usage (recent runs):")
        click.echo(f"  Total tokens: {total_tokens:,}")

    # Errors
    err_count = db.error_count()
    click.echo(f"\nRecorded errors: {err_count}")

    # Review queue
    click.echo(f"\nQuestions needing review: {s['questions_needing_review']}")
    click.echo(f"{'='*60}\n")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", type=int, default=5000, help="Bind port")
@click.pass_context
def dashboard(ctx, host, port):
    """Launch the localhost web dashboard to track application progress."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)
    from velvetoverride.web.dashboard import run_dashboard

    db_path = config.tracking.get("database_path", "data/applications.db")
    click.echo(f"\n  VelvetOverride dashboard → http://{host}:{port}")
    click.echo("  (reads data/applications.db · Ctrl-C to stop)\n")
    run_dashboard(db_path=db_path, host=host, port=port)


@cli.command(name="token-test")
@click.option("--jobs", "-n", type=int, default=10, help="Number of sample jobs to run")
@click.option("--apps-per-day", type=int, default=None, help="For the daily projection")
@click.pass_context
def token_test(ctx, jobs, apps_per_day):
    """Estimate token use by running the LLM pipeline on sample jobs (no LinkedIn)."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)
    if not config.has_llm:
        click.echo("No LLM API key configured. Set OPENAI_API_KEY in config/.env.")
        return

    from velvetoverride.agent.token_test import run_token_test

    per_day = apps_per_day or config.bot.get("max_applications", 5)
    click.echo(f"\nRunning token test on {jobs} sample jobs "
               f"(provider={config.llm_provider}, model={config.llm.get('field_model')})...\n")
    r = run_token_test(config, n=jobs, apps_per_day=per_day)

    click.echo(f"{'='*64}")
    click.echo("  Token Usage Test")
    click.echo(f"{'='*64}")
    click.echo(f"  Provider / models : {r['provider']} - {r['field_model']} + {r['resume_model']}")
    click.echo(f"  Jobs tested       : {r['jobs_tested']}")
    click.echo(f"  Total LLM calls   : {r['total_calls']}")
    click.echo(f"  Total tokens      : {r['total_tokens']:,} "
               f"(prompt {r['prompt_tokens']:,} / completion {r['completion_tokens']:,})")
    click.echo(f"  Avg per job       : {r['avg_tokens_per_job']:,} tokens")
    click.echo(f"  Est. cost (test)  : ${r['est_cost_usd']:.4f}  (billed rate; $0 on shared free tokens)")
    click.echo("\n  Projection:")
    p = r["projection"]
    click.echo(f"    {p['apps_per_day']} apps/day  -> {p['tokens_per_day']:,} tokens/day, "
               f"~{p['tokens_per_month']:,}/month")
    click.echo(f"    Est. billed cost -> ${p['est_cost_per_day_usd']:.4f}/day")
    click.echo("\n  Free-token headroom (data-sharing enabled):")
    click.echo(f"    mini models: ~2.5M tokens/day (tier 1-2) -- usage is "
               f"{(p['tokens_per_day']/2_500_000*100):.2f}% of that bucket")
    click.echo(f"{'='*64}\n")


@cli.command()
@click.option("--run-id", type=int, default=None, help="Audit a specific run (default: most recent)")
@click.option("--all", "show_all", is_flag=True, help="Show every field->answer, not just flagged ones")
@click.pass_context
def feedback(ctx, run_id, show_all):
    """Field-routing feedback: which answer went into which field, with flagged
    mis-routings (e.g. a name field that got a sentence). Use this after a run to
    review and correct routing; `review --approve` then feeds the fixes back."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)
    from velvetoverride.tracking.database import TrackingDB

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()
    if run_id is None:
        runs_list = db.get_runs(limit=1)
        if not runs_list:
            click.echo("No runs recorded yet.")
            db.close()
            return
        run_id = runs_list[0]["id"]

    issues = audit_field_routing(db, run_id)
    questions = db.get_run_questions(run_id)
    flagged_ids = {i["id"] for i in issues}
    db.close()

    click.echo(f"\n{'='*74}\n  Field routing — run #{run_id}  "
               f"({len(questions)} fields, {len(issues)} flagged)\n{'='*74}")
    problems = {i["id"]: i["problem"] for i in issues}
    shown = 0
    for q in questions:
        flagged = q["id"] in flagged_ids
        if not show_all and not flagged:
            continue
        shown += 1
        mark = "⚠ " if flagged else "  "
        label = (q.get("question_text") or "")[:44]
        ans = (q.get("answer_given") or "")[:46]
        click.echo(f"\n{mark}[{q.get('company','')[:16]}] {label!r}  ({q.get('field_type','')}/{q.get('answer_source','')})")
        click.echo(f"     -> {ans!r}")
        if flagged:
            click.echo(f"     PROBLEM: {problems[q['id']]}")
    if not shown:
        click.echo("\n  No flagged routing issues 🎉  (use --all to see every field)")
    click.echo(f"\n  Flagged fields are marked needs_review — run "
               f"`velvetoverride review --approve` to correct them (feeds learned answers).")
    click.echo(f"{'='*74}\n")


@cli.command()
@click.option("--limit", type=int, default=50, help="Number of recent errors to show")
@click.pass_context
def errors(ctx, limit):
    """Show recorded errors from bot runs."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)
    from velvetoverride.tracking.database import TrackingDB

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()
    errs = db.get_errors(limit=limit)
    db.close()

    if not errs:
        click.echo("No errors recorded.")
        return

    click.echo(f"\n{'='*70}\n  Recorded Errors ({len(errs)})\n{'='*70}")
    for e in errs:
        when = (e.get("occurred_at") or "")[:19].replace("T", " ")
        click.echo(f"\n  [{when}] {e.get('stage','?')} · {e.get('error_type','')}")
        if e.get("company") or e.get("job_title"):
            click.echo(f"    Job: {e.get('company','')} — {e.get('job_title','')}")
        click.echo(f"    {e.get('message','')[:200]}")
    click.echo(f"\n{'='*70}\n")


@cli.command()
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "both"]), default="both")
@click.pass_context
def export(ctx, fmt):
    """Export application data to CSV/JSON."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)

    from velvetoverride.tracking.database import TrackingDB
    from velvetoverride.tracking.export import export_csv, export_json, export_review_queue

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()

    export_path = config.tracking.get("export_path", "data/")
    if fmt in ("csv", "both"):
        path = export_csv(db, export_path)
        click.echo(f"CSV exported: {path}")
    if fmt in ("json", "both"):
        path = export_json(db, export_path)
        click.echo(f"JSON exported: {path}")
    path = export_review_queue(db, export_path)
    click.echo(f"Review queue exported: {path}")

    db.close()


@cli.command()
@click.option("--approve", is_flag=True, help="Interactively approve/correct answers")
@click.pass_context
def review(ctx, approve):
    """Show questions flagged for human review. Use --approve to approve/correct."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)

    from velvetoverride.agent.field_solver import FieldSolver
    from velvetoverride.tracking.database import TrackingDB

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()
    items = db.get_needs_review()

    if not items:
        click.echo("No questions need review.")
        db.close()
        return

    if not approve:
        # Display-only mode
        for item in items:
            click.echo(f"\n{'='*60}")
            click.echo(f"  ID: {item.get('id', '?')}")
            click.echo(f"  Company: {item.get('company', '?')} — {item.get('job_title', '?')}")
            click.echo(f"  Question: {item.get('question_text', '?')}")
            click.echo(f"  Answer given: {item.get('answer_given', '?')}")
            click.echo(f"  Source: {item.get('answer_source', '?')}")

        click.echo(f"\n{'='*60}")
        click.echo(f"Total questions needing review: {len(items)}")
        click.echo("Run with --approve to approve or correct answers.")
        db.close()
        return

    # Interactive approval mode
    field_solver = FieldSolver(config)
    approved = 0
    corrected = 0

    for item in items:
        click.echo(f"\n{'='*60}")
        click.echo(f"  Company: {item.get('company', '?')} — {item.get('job_title', '?')}")
        click.echo(f"  Question: {item.get('question_text', '?')}")
        click.echo(f"  Answer given: {item.get('answer_given', '?')}")
        click.echo(f"  Field type: {item.get('field_type', '?')}")

        action = click.prompt(
            "\n  [a]pprove / [c]orrect / [s]kip / [q]uit",
            type=click.Choice(["a", "c", "s", "q"]),
            default="s",
        )

        if action == "q":
            break
        elif action == "s":
            continue
        elif action == "a":
            # Approve as-is: mark reviewed + learn the answer
            db.approve_answer(item["id"])
            field_solver.learn_answer(
                item["question_text"],
                item["answer_given"],
                item.get("field_type", "text"),
            )
            approved += 1
            click.echo("  -> Approved and learned.")
        elif action == "c":
            new_answer = click.prompt("  Enter corrected answer")
            db.approve_answer(item["id"])
            field_solver.learn_answer(
                item["question_text"],
                new_answer,
                item.get("field_type", "text"),
            )
            corrected += 1
            click.echo(f"  -> Corrected to: {new_answer}")

    # Persist learned answers to answers.yaml
    if approved + corrected > 0:
        _persist_learned_answers(config, field_solver)

    db.close()
    click.echo(f"\nApproved: {approved}, Corrected: {corrected}")


def _persist_learned_answers(config, field_solver) -> None:
    """Write learned answers back to answers.yaml."""
    answers_path = config.config_dir / "answers.yaml"
    try:
        with open(answers_path, encoding="utf-8") as f:
            answers_data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        answers_data = {}

    # Merge learned answers
    answers_data["learned"] = field_solver._answers.get("learned", {})

    with open(answers_path, "w", encoding="utf-8") as f:
        yaml.dump(answers_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    log.info("review.answers_persisted", path=str(answers_path), count=len(answers_data.get("learned", {})))


@cli.command()
@click.argument("job_title")
@click.argument("company")
@click.argument("job_description_file", type=click.Path(exists=True))
@click.pass_context
def tailor(ctx, job_title, company, job_description_file):
    """Generate a tailored resume for a specific job (without applying)."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)

    from velvetoverride.agent.llm import LLMClient
    from velvetoverride.agent.resume_tailor import ResumeTailor
    from velvetoverride.resume.builder import ResumeBuilder
    from velvetoverride.resume.scorer import ATSScorer

    jd = Path(job_description_file).read_text(encoding="utf-8")

    llm = LLMClient(config)
    builder = ResumeBuilder(
        template_name=config.resume_config.get("template", "default"),
        output_dir=Path("data/resumes"),
    )
    scorer = ATSScorer(target_coverage=config.resume_config.get("target_keyword_coverage", 0.70))
    rt = ResumeTailor(config, llm, builder, scorer)

    pdf_path, ats_score, keywords = rt.tailor_resume(job_title, company, jd)

    click.echo(f"\nResume generated: {pdf_path}")
    click.echo(f"ATS Score: {ats_score:.1f}/100")
    click.echo(f"Keywords targeted: {', '.join(keywords[:10])}")


@cli.command()
@click.option("--limit", type=int, default=20, help="Number of recent runs to show")
@click.pass_context
def runs(ctx, limit):
    """Show recent bot run history."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)

    from velvetoverride.tracking.database import TrackingDB

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()
    run_list = db.get_runs(limit=limit)
    db.close()

    if not run_list:
        click.echo("No runs recorded yet.")
        return

    click.echo(f"\n{'='*70}")
    click.echo("  Recent Bot Runs")
    click.echo(f"{'='*70}")

    for r in run_list:
        mode = "dry-run" if r.get("dry_run") else "LIVE"
        status = r.get("status", "?")
        started = r.get("started_at", "?")[:19]
        finished = (r.get("finished_at") or "running")[:19]
        click.echo(f"\n  Run #{r['id']} — {started} to {finished}")
        click.echo(f"    Mode: {mode}  |  Status: {status}")
        click.echo(f"    Listings: {r.get('listings_found', 0)} found, {r.get('listings_after_filter', 0)} after filter")
        click.echo(f"    Applied: {r.get('applied_count', 0)}  Failed: {r.get('failed_count', 0)}  Skipped: {r.get('skipped_count', 0)}")
        if r.get("error_message"):
            click.echo(f"    Error: {r['error_message'][:100]}")
        if r.get("min_salary") or r.get("max_salary"):
            click.echo(f"    Salary filter: ${r.get('min_salary', '?'):,} — ${r.get('max_salary', '?'):,}")

    click.echo(f"\n{'='*70}\n")


if __name__ == "__main__":
    cli()
