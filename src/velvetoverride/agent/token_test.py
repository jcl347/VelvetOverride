"""Offline token-usage estimator.

Runs the exact LLM calls the apply pipeline makes for each job — keyword
extraction, bullet relevance scoring, summary tailoring, and a couple of
representative novel screening questions — against sample job descriptions,
using the real OpenAI API, and reports token consumption.

This is the safe way to "test-run 10 jobs to evaluate token use" without
logging into LinkedIn or submitting any real applications.
"""

from __future__ import annotations

from typing import Any

from velvetoverride.agent.field_solver import FieldSolver
from velvetoverride.agent.llm import LLMClient
from velvetoverride.utils.logging import get_logger

log = get_logger(__name__)

# Representative JDs across the four target role families.
SAMPLE_JOBS = [
    ("Senior Data Scientist", "Nimbus Health",
     "We are hiring a Senior Data Scientist to build production ML models over "
     "large healthcare datasets. Requirements: Python, PySpark, SQL, experience "
     "with logistic regression, random forests, and gradient boosting, plus "
     "strong statistical modeling and A/B testing. Cloud experience (Azure or "
     "GCP) required. $150,000-$190,000."),
    ("Software Engineer, Backend", "Corvus Labs",
     "Backend Software Engineer to design distributed services and data pipelines. "
     "Python and SQL required; experience with Docker, CI/CD, and cloud platforms. "
     "You will build scalable APIs and own reliability. 5+ years experience."),
    ("AI Engineer", "Halcyon AI",
     "AI Engineer to build LLM-powered NLP systems and inference infrastructure. "
     "Must have Python, experience deploying LLMs, prompt engineering, vector "
     "search, and MLOps. Bonus: PyTorch, real-time anomaly detection."),
    ("Machine Learning Engineer", "Delphi Systems",
     "ML Engineer to productionize models at terabyte scale using PySpark and "
     "Spark. Python, SQL, model deployment, and distributed data processing "
     "required. Experience with Snowflake and Hadoop a plus."),
    ("Data Scientist II", "Meridian Care",
     "Data Scientist to design predictive risk models for member health outcomes. "
     "Python, PySpark, statistics, and healthcare data modeling. RCT / causal "
     "inference experience valued. $130,000-$165,000."),
    ("Full Stack Software Engineer", "Beacon Digital",
     "Software Engineer with Python and JavaScript to ship web features end to "
     "end. Experience with REST APIs, SQL databases, and Git-based workflows."),
    ("Applied Scientist, NLP", "Northwind AI",
     "Applied Scientist to research and ship NLP models. Python, deep learning, "
     "transformer architectures, and publication track record preferred. GCP and "
     "Weights & Biases experience a plus."),
    ("Machine Learning Engineer, Platform", "Ionic Compute",
     "ML Platform Engineer to build distributed inference infrastructure on Azure. "
     "Python, Docker, MLOps, and pipeline orchestration. 4+ years."),
    ("Senior Data Scientist, Risk", "Aegis Health",
     "Senior Data Scientist focused on regulatory risk and anomaly detection. "
     "Python, PySpark, SQL, statistical confidence intervals, and large-scale "
     "data engineering. $160,000-$200,000."),
    ("AI Engineer, Agents", "Sable Research",
     "AI Engineer to build multi-agent autonomous research systems. Python, LLM "
     "orchestration, tool use, and evaluation. Startup pace, high ownership."),
]

# Novel screening questions a config/profile lookup would NOT answer, so they
# fall through to the LLM (the realistic per-application LLM field load).
SAMPLE_QUESTIONS = [
    ("Why are you interested in this role?", "textarea"),
    ("Describe a relevant project in 2-3 sentences.", "textarea"),
]


def run_token_test(config: Any, n: int = 10, apps_per_day: int = 5) -> dict:
    """Run the LLM pipeline for ``n`` sample jobs and return a usage report."""
    llm = LLMClient(config)
    solver = FieldSolver(config, llm)
    profile_summary = solver._build_profile_summary()

    jobs = (SAMPLE_JOBS * ((n // len(SAMPLE_JOBS)) + 1))[:n]
    per_job = []

    for i, (title, company, jd) in enumerate(jobs):
        before = llm.usage.total_tokens
        before_calls = llm.usage.calls

        # 1) Resume tailoring calls (same as ResumeTailor, minus PDF build)
        keywords = llm.extract_keywords(jd)
        bullets = _collect_bullets(config)
        if bullets:
            llm.score_bullet_relevance(bullets, keywords, title)
        llm.tailor_summary(config.profile.get("summary", ""), title, company, jd, keywords)

        # 2) Representative novel field questions (LLM fallback path)
        for q_text, q_type in SAMPLE_QUESTIONS:
            llm.answer_field(
                question=q_text, field_type=q_type, options=None,
                job_description=jd, job_title=title, company=company,
                profile_summary=profile_summary,
            )

        used = llm.usage.total_tokens - before
        calls = llm.usage.calls - before_calls
        per_job.append({"job": f"{title} @ {company}", "tokens": used, "calls": calls})
        log.info("token_test.job_done", index=i + 1, tokens=used, calls=calls)

    u = llm.usage
    avg = round(u.total_tokens / n) if n else 0
    report = {
        "provider": config.llm_provider,
        "field_model": llm.field_model,
        "resume_model": llm.resume_model,
        "jobs_tested": n,
        "total_tokens": u.total_tokens,
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_calls": u.calls,
        "avg_tokens_per_job": avg,
        "est_cost_usd": round(u.est_cost_usd, 4),
        "by_model": u.by_model,
        "per_job": per_job,
        "projection": {
            "apps_per_day": apps_per_day,
            "tokens_per_day": avg * apps_per_day,
            "tokens_per_month": avg * apps_per_day * 30,
            "est_cost_per_day_usd": round((u.est_cost_usd / n) * apps_per_day, 4) if n else 0,
        },
    }
    return report


def _collect_bullets(config: Any) -> list[dict]:
    bullets = []
    for job in config.profile.get("experience", []):
        for b in job.get("bullets", []):
            bullets.append({
                "text": b.get("text", ""),
                "skills": b.get("skills", []),
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "relevance_score": 50,
            })
    return bullets
