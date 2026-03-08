"""CLI entry point — orchestrates the full application pipeline."""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

import click
import yaml

from velvetoverride.utils.config import list_job_profiles, load_config
from velvetoverride.utils.logging import get_logger, setup_logging

log = get_logger(__name__)


async def run_bot(
    config_dir: str | None = None,
    dry_run: bool | None = None,
    max_apps_override: int | None = None,
    min_salary: int | None = None,
    max_salary: int | None = None,
    job_profile: str | None = None,
) -> None:
    """Main bot orchestration loop."""
    config = load_config(
        Path(config_dir) if config_dir else None,
        job_profile=job_profile,
    )

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
        job_profile=config.active_job_profile,
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

    # Start run tracking
    config_snapshot = json.dumps({
        "dry_run": is_dry_run,
        "keywords": config.search.get("keywords", []),
        "location": config.search.get("location", ""),
        "job_profile": config.active_job_profile,
    }, default=str)
    run_id = db.start_run(
        dry_run=is_dry_run,
        max_apps=max_apps,
        min_salary=effective_min_salary,
        max_salary=effective_max_salary,
        config_snapshot=config_snapshot,
    )

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

        listings_after_filter = len(listings)

        # ── Find default resume for fallback ──
        default_resume = _find_default_resume(config)

        # ── Apply to each listing ──
        app_flow = ApplicationFlow(page, config, field_solver, db)

        for i, listing in enumerate(listings):
            if applied_count >= max_apps:
                log.info("bot.max_reached", count=applied_count)
                break

            # Deduplication
            if db.is_already_applied(listing.url):
                log.info("bot.skip_duplicate", url=listing.url)
                skipped_count += 1
                continue

            # Fuzzy dedup by title + company
            similar = db.find_similar_jobs(listing.title, listing.company)
            is_fuzzy_dup = False
            if similar:
                from thefuzz import fuzz
                for s in similar:
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

            log.info(
                "bot.applying",
                index=i + 1,
                total=len(listings),
                title=listing.title,
                company=listing.company,
                score=f"{listing.match_score:.0f}",
            )

            # Tailor resume if LLM available, with fallback to default resume
            resume_path = None
            if resume_tailor and listing.description:
                try:
                    resume_path, ats_score, _ = resume_tailor.tailor_resume(
                        listing.title, listing.company, listing.description
                    )
                    log.info("bot.resume_tailored", ats_score=f"{ats_score:.0f}")
                except Exception as e:
                    log.warning("bot.resume_tailor_error", error=str(e))
                    resume_path = default_resume
                    if resume_path:
                        log.info("bot.resume_fallback", path=resume_path)

            if not resume_path and default_resume:
                resume_path = default_resume

            # Apply
            record = await app_flow.apply_to_job(listing, resume_path)
            if resume_path:
                record.resume_version = resume_path
            record.salary_min = listing.salary_min
            record.salary_max = listing.salary_max
            record.salary_raw = listing.salary_raw
            db.save_application(record, run_id=run_id)

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
        await browser.close()
        db.close()


def _find_default_resume(config) -> str | None:
    """Find a default resume file for fallback when tailoring fails."""
    resume_dir = Path("data/resumes")
    if resume_dir.exists():
        # Use the most recently generated resume
        resumes = sorted(resume_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if resumes:
            return str(resumes[0])
        resumes = sorted(resume_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        if resumes:
            return str(resumes[0])

    # Check for a configured static resume path
    static_path = config.resume_config.get("static_resume_path")
    if static_path and Path(static_path).exists():
        return static_path

    return None


def _score_listings(listings, config, llm):
    """Score listings by job match quality using keyword overlap.

    When a job profile with skill_emphasis is active, emphasized skills
    are weighted 3x higher than general skills, so jobs matching the
    profile's target skills rank significantly higher.
    """
    profile = config.profile
    emphasis = [s.lower() for s in config.skill_emphasis]

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

        if emphasis:
            # Weighted scoring: emphasized skills count 3x
            emphasis_matched = sum(1 for s in emphasis if s in desc_lower)
            general_only = [s for s in all_skills if s not in emphasis]
            general_matched = sum(1 for s in general_only if s in desc_lower)
            weighted = emphasis_matched * 3 + general_matched
            total = len(emphasis) * 3 + len(general_only) if (emphasis or general_only) else 1
            listing.match_score = min((weighted / total) * 200, 100)
        else:
            matched = sum(1 for s in all_skills if s in desc_lower)
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
@click.option("--profile", "job_profile", default=None, help="Job profile to use (e.g. backend_engineer, frontend_engineer)")
@click.pass_context
def run(ctx, dry_run, max_apps, min_salary, max_salary, job_profile):
    """Run the full application bot pipeline."""
    asyncio.run(run_bot(ctx.obj["config_dir"], dry_run, max_apps, min_salary, max_salary, job_profile))


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

    # Review queue
    click.echo(f"\nQuestions needing review: {s['questions_needing_review']}")
    click.echo(f"{'='*60}\n")


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
        with open(answers_path) as f:
            answers_data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        answers_data = {}

    # Merge learned answers
    answers_data["learned"] = field_solver._answers.get("learned", {})

    with open(answers_path, "w") as f:
        yaml.dump(answers_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    log.info("review.answers_persisted", path=str(answers_path), count=len(answers_data.get("learned", {})))


@cli.command()
@click.argument("job_title")
@click.argument("company")
@click.argument("job_description_file", type=click.Path(exists=True))
@click.option("--profile", "job_profile", default=None, help="Job profile to use (e.g. backend_engineer)")
@click.pass_context
def tailor(ctx, job_title, company, job_description_file, job_profile):
    """Generate a tailored resume for a specific job (without applying)."""
    config = load_config(
        Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None,
        job_profile=job_profile,
    )

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


@cli.command()
@click.pass_context
def profiles(ctx):
    """List available job profiles."""
    config_dir = Path(ctx.obj["config_dir"]) if ctx.obj["config_dir"] else None
    profile_list = list_job_profiles(config_dir)

    if not profile_list:
        click.echo("No job profiles found.")
        click.echo("Create YAML files in config/job_profiles/ to define profiles.")
        click.echo("See config/job_profiles/backend_engineer.yaml for an example.")
        return

    click.echo(f"\n{'='*60}")
    click.echo("  Available Job Profiles")
    click.echo(f"{'='*60}")

    for p in profile_list:
        click.echo(f"\n  {p['slug']}")
        click.echo(f"    Name: {p['name']}")
        if p['description']:
            click.echo(f"    {p['description']}")

    click.echo(f"\n{'='*60}")
    click.echo(f"  {len(profile_list)} profile(s) available")
    click.echo(f"  Usage: velvetoverride run --profile <slug>")
    click.echo(f"{'='*60}\n")


if __name__ == "__main__":
    cli()
