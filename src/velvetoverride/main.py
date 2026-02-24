"""CLI entry point — orchestrates the full application pipeline."""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import click

from velvetoverride.utils.config import load_config
from velvetoverride.utils.logging import get_logger, setup_logging

log = get_logger(__name__)


async def run_bot(
    config_dir: str | None = None,
    dry_run: bool | None = None,
    max_apps_override: int | None = None,
    min_salary: int | None = None,
    max_salary: int | None = None,
) -> None:
    """Main bot orchestration loop."""
    config = load_config(Path(config_dir) if config_dir else None)

    if dry_run is not None:
        config.settings.setdefault("bot", {})["dry_run"] = dry_run

    is_dry_run = config.bot.get("dry_run", True)
    max_apps = max_apps_override or config.bot.get("max_applications", 25)

    # Salary filter: CLI overrides take precedence, then settings.yaml
    salary_cfg = config.settings.get("salary", {})
    effective_min_salary = min_salary or salary_cfg.get("min_annual")
    effective_max_salary = max_salary or salary_cfg.get("max_annual")

    log.info(
        "bot.starting",
        dry_run=is_dry_run,
        max_applications=max_apps,
        min_salary=effective_min_salary,
        max_salary=effective_max_salary,
        keywords=config.search.get("keywords", []),
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

    llm: LLMClient | None = None
    if config.anthropic_api_key:
        llm = LLMClient(config)
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

    try:
        # ── Launch browser & authenticate ──
        page = await browser.launch()
        if not await login(page, config):
            log.error("bot.login_failed")
            return

        # ── Check for CAPTCHA after login ──
        if await detect_captcha(page):
            log.warning("bot.captcha_after_login")
            if not await handle_captcha(page, config):
                log.error("bot.captcha_unresolved")
                return

        # ── Search for jobs ──
        listings = await scrape_job_listings(page, config)
        log.info("bot.listings_found", count=len(listings))

        if not listings:
            log.warning("bot.no_listings")
            return

        # ── Score and sort listings by match ──
        min_score = config.search.get("min_match_score", 0)
        if llm and listings:
            listings = _score_listings(listings, config, llm)
            listings = [l for l in listings if l.match_score >= min_score]
            listings.sort(key=lambda l: l.match_score, reverse=True)
            log.info("bot.after_scoring", remaining=len(listings))

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

        # ── Apply to each listing ──
        app_flow = ApplicationFlow(page, config, field_solver, db)
        applied_count = 0

        for i, listing in enumerate(listings):
            if applied_count >= max_apps:
                log.info("bot.max_reached", count=applied_count)
                break

            # Deduplication
            if db.is_already_applied(listing.url):
                log.info("bot.skip_duplicate", url=listing.url)
                continue

            # Fuzzy dedup by title + company
            similar = db.find_similar_jobs(listing.title, listing.company)
            if similar:
                from thefuzz import fuzz
                for s in similar:
                    if fuzz.ratio(listing.title.lower(), s.job_title.lower()) > 85:
                        log.info(
                            "bot.skip_fuzzy_dup",
                            title=listing.title,
                            existing=s.job_title,
                        )
                        continue

            log.info(
                "bot.applying",
                index=i + 1,
                total=len(listings),
                title=listing.title,
                company=listing.company,
                score=f"{listing.match_score:.0f}",
            )

            # Tailor resume if LLM available
            resume_path = None
            if resume_tailor and listing.description:
                try:
                    resume_path, ats_score, _ = resume_tailor.tailor_resume(
                        listing.title, listing.company, listing.description
                    )
                    log.info("bot.resume_tailored", ats_score=f"{ats_score:.0f}")
                except Exception as e:
                    log.warning("bot.resume_tailor_error", error=str(e))

            # Apply
            record = await app_flow.apply_to_job(listing, resume_path)
            if resume_path:
                record.resume_version = resume_path
            record.salary_min = listing.salary_min
            record.salary_max = listing.salary_max
            record.salary_raw = listing.salary_raw
            db.save_application(record)
            applied_count += 1

            # Human-like delay between applications
            delay_min = config.bot.get("delay_min", 5)
            delay_max = config.bot.get("delay_max", 18)
            await random_delay(delay_min, delay_max)

            # Occasionally diversify activity
            if random.random() < 0.2:
                await diversify_activity(page)

        # ── Export results ──
        from velvetoverride.tracking.export import export_csv, export_json, export_review_queue

        export_path = config.tracking.get("export_path", "data/")
        export_fmt = config.tracking.get("export_format", "both")

        if export_fmt in ("csv", "both"):
            export_csv(db, export_path)
        if export_fmt in ("json", "both"):
            export_json(db, export_path)
        export_review_queue(db, export_path)

        # ── Summary ──
        stats = db.get_stats()
        log.info("bot.complete", **stats)

    finally:
        await browser.close()
        db.close()


def _score_listings(listings, config, llm):
    """Score listings by job match quality using keyword overlap."""
    from velvetoverride.resume.scorer import ATSScorer

    profile = config.profile
    all_skills = []
    for cat_skills in profile.get("skills", {}).values():
        if isinstance(cat_skills, list):
            all_skills.extend([s.lower() for s in cat_skills])

    # Add technology experience keys
    for tech in profile.get("technology_experience", {}).keys():
        if tech != "default":
            all_skills.append(tech.lower())

    scorer = ATSScorer()

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
@click.pass_context
def run(ctx, dry_run, max_apps, min_salary, max_salary):
    """Run the full application bot pipeline."""
    asyncio.run(run_bot(ctx.obj["config_dir"], dry_run, max_apps, min_salary, max_salary))


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

    click.echo(f"\nTotal applications: {s['total_applications']}")
    click.echo("By status:")
    for status, count in s.get("by_status", {}).items():
        click.echo(f"  {status}: {count}")
    click.echo(f"Questions needing review: {s['questions_needing_review']}\n")


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
@click.pass_context
def review(ctx):
    """Show questions flagged for human review."""
    config = load_config(Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None)

    from velvetoverride.tracking.database import TrackingDB

    db = TrackingDB(config.tracking.get("database_path", "data/applications.db"))
    db.connect()
    items = db.get_needs_review()
    db.close()

    if not items:
        click.echo("No questions need review.")
        return

    for item in items:
        click.echo(f"\n{'='*60}")
        click.echo(f"Company: {item.get('company', '?')} — {item.get('job_title', '?')}")
        click.echo(f"Question: {item.get('question_text', '?')}")
        click.echo(f"Answer given: {item.get('answer_given', '?')}")
        click.echo(f"Source: {item.get('answer_source', '?')}")

    click.echo(f"\n{'='*60}")
    click.echo(f"Total questions needing review: {len(items)}")


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

    jd = Path(job_description_file).read_text()

    llm = LLMClient(config)
    builder = ResumeBuilder(
        template_name=config.resume_config.get("template", "default"),
        output_dir=Path("data/resumes"),
    )
    scorer = ATSScorer(target_coverage=config.resume_config.get("target_keyword_coverage", 0.70))
    tailor = ResumeTailor(config, llm, builder, scorer)

    pdf_path, ats_score, keywords = tailor.tailor_resume(job_title, company, jd)

    click.echo(f"\nResume generated: {pdf_path}")
    click.echo(f"ATS Score: {ats_score:.1f}/100")
    click.echo(f"Keywords targeted: {', '.join(keywords[:10])}")


if __name__ == "__main__":
    cli()
