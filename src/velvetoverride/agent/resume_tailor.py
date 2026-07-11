"""Resume tailoring — analyzes JDs and generates role-specific resumes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.agent.llm import LLMClient
    from velvetoverride.resume.builder import ResumeBuilder
    from velvetoverride.resume.scorer import ATSScorer
    from velvetoverride.utils.config import Config

log = get_logger(__name__)

# Canonical display casing for tech terms (skill tags are lowercase in YAML).
_CANONICAL_TERMS = {
    "sql": "SQL", "nlp": "NLP", "llm": "LLM", "llms": "LLMs",
    "pyspark": "PySpark", "gcp": "GCP", "aws": "AWS", "ci/cd": "CI/CD",
    "ml": "ML", "ai": "AI", "mlops": "MLOps", "etl": "ETL", "api": "API",
    "rest api": "REST API", "kpi": "KPI", "gpu": "GPU", "rct": "RCT",
    "pytorch": "PyTorch", "tensorflow": "TensorFlow", "postgresql": "PostgreSQL",
    "javascript": "JavaScript", "typescript": "TypeScript",
    "github actions": "GitHub Actions", "weights & biases": "Weights & Biases",
    "sdlc": "SDLC", "dhcs": "DHCS", "spark": "Spark", "azure": "Azure",
}


def _pretty_term(term: str) -> str:
    """Display casing for a skill tag (acronyms stay uppercase)."""
    t = str(term).strip()
    if not t:
        return ""
    canon = _CANONICAL_TERMS.get(t.lower())
    if canon:
        return canon
    # Preserve author-supplied capitalization if present
    if any(c.isupper() for c in t):
        return t
    return t.title()


def _build_projects(projects: list[dict], keywords: list[str] | None = None) -> list[dict]:
    """Normalize the profile's projects for the resume template.

    Tech tags get canonical casing and, when JD keywords are supplied, are
    ordered so the relevant ones read first. Only the applicant's own tags are
    used — nothing is pulled in from the job description.
    """
    keywords = keywords or []
    out: list[dict] = []
    for p in projects or []:
        name = str(p.get("name", "")).strip()
        if not name:
            continue
        tech = [_pretty_term(t) for t in (p.get("tech") or []) if str(t).strip()]
        if keywords and tech:
            matched = [t for t in tech if ResumeTailor._matches_keyword(t, keywords)]
            tech = matched + [t for t in tech if t not in matched]
        out.append({
            "name": name,
            "detail": str(p.get("detail", "")).strip(),
            "url": str(p.get("url", "")).strip(),
            "tech": tech[:4],
        })
    return out


class ResumeTailor:
    """Orchestrates the resume tailoring pipeline:
    1. Extract keywords from JD
    2. Score bullet points by relevance
    3. Rewrite professional summary
    4. Build tailored PDF
    5. Score against ATS criteria
    """

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        builder: ResumeBuilder,
        scorer: ATSScorer,
    ) -> None:
        self._config = config
        self._llm = llm
        self._builder = builder
        self._scorer = scorer

    def tailor_resume(
        self,
        job_title: str,
        company: str,
        job_description: str,
    ) -> tuple[str, float, list[str]]:
        """Generate a tailored resume for a specific job.

        Returns:
            (pdf_path, ats_score, keywords_used)
        """
        log.info("tailor.starting", title=job_title, company=company)

        # Step 1: Extract keywords from JD
        keywords = self._llm.extract_keywords(job_description)
        log.info("tailor.keywords", count=len(keywords), sample=keywords[:5])

        # Step 2: Score and rank bullet points by relevance
        all_bullets = self._collect_all_bullets()
        if all_bullets:
            scored_bullets = self._llm.score_bullet_relevance(
                all_bullets, keywords, job_title
            )
        else:
            scored_bullets = all_bullets

        # Step 3: Rewrite the professional summary
        base_summary = self._config.profile.get("summary", "")
        tailored_summary = self._llm.tailor_summary(
            base_summary=base_summary,
            job_title=job_title,
            company=company,
            job_description=job_description,
            skills=keywords,
        )

        # Step 4: Build the tailored resume data
        resume_data = self._build_tailored_data(
            tailored_summary, scored_bullets, keywords, job_title
        )

        # Step 5: Generate PDF — named "<First_Last>_resume_<Company>"
        personal = self._config.personal
        name = f"{personal.get('first_name', '')}_{personal.get('last_name', '')}".strip("_") or "Resume"
        safe_company = "_".join(
            "".join(c for c in word if c.isalnum()) for word in company.split()
        ).strip("_") or "Company"
        filename = f"{name}_resume_{safe_company}"
        pdf_path = self._builder.build(resume_data, filename)

        # Step 6: ATS score
        ats_score = self._scorer.score(resume_data, keywords)
        log.info(
            "tailor.complete",
            pdf_path=pdf_path,
            ats_score=f"{ats_score:.1f}",
            keywords_covered=len(keywords),
        )

        return pdf_path, ats_score, keywords

    def _collect_all_bullets(self) -> list[dict]:
        """Collect all bullet points from all jobs in the profile."""
        bullets = []
        experience = self._config.profile.get("experience", [])
        for job in experience:
            company = job.get("company", "")
            title = job.get("title", "")
            for bullet in job.get("bullets", []):
                bullets.append({
                    "text": bullet.get("text", ""),
                    "skills": bullet.get("skills", []),
                    "company": company,
                    "title": title,
                    "relevance_score": 50,  # Default
                })
        return bullets

    @staticmethod
    def _matches_keyword(term: str, keywords: list[str]) -> bool:
        """True if a profile skill overlaps any JD keyword (substring, both ways)."""
        t = term.lower().strip()
        if not t:
            return False
        for kw in keywords:
            k = kw.lower().strip()
            if not k:
                continue
            if t == k or t in k or k in t:
                return True
        return False

    @classmethod
    def _prioritize(cls, terms: list[str], keywords: list[str]) -> list[str]:
        """Reorder terms so JD-matching ones come first (nothing added/removed)."""
        matched = [t for t in terms if cls._matches_keyword(t, keywords)]
        rest = [t for t in terms if t not in matched]
        return matched + rest

    def _role_technologies(
        self, job_bullets: list[dict], keywords: list[str],
        job_title: str = "", limit: int = 8,
    ) -> list[str]:
        """Technologies for one role, taken from its bullets' skill tags.

        ChatGPT ranks them against the job description; the result is filtered
        back to the applicant's own tags, so the Experience section carries the
        key technical keywords an ATS scans for without inventing anything.
        """
        seen: list[str] = []
        for b in job_bullets:
            for skill in b.get("skills", []) or []:
                pretty = _pretty_term(skill)
                if pretty and pretty.lower() not in {x.lower() for x in seen}:
                    seen.append(pretty)
        if not seen:
            return []

        if self._llm is not None and keywords:
            try:
                return self._llm.optimize_keywords(seen, keywords, job_title, limit=limit)
            except Exception as e:  # noqa: BLE001
                log.warning("tailor.optimize_failed", error=str(e)[:80])
        return self._prioritize(seen, keywords)[:limit]

    def _build_tailored_data(
        self,
        summary: str,
        scored_bullets: list[dict],
        keywords: list[str],
        job_title: str = "",
    ) -> dict:
        """Assemble the full resume data for template rendering."""
        profile = self._config.profile

        # Guardrails: by default we keep EVERY bullet verbatim and preserve the
        # original order, so real experience and impact metrics are never
        # dropped or altered. Tailoring is limited to the summary + skill
        # emphasis. These can be opted into via settings.yaml → resume.
        resume_cfg = self._config.resume_config
        reorder = resume_cfg.get("reorder_bullets", False)
        max_bullets = resume_cfg.get("max_bullets_per_job")  # None = keep all

        experience_data = []
        for job_index, job in enumerate(profile.get("experience", [])):
            job_bullets = [
                b for b in scored_bullets
                if b.get("company") == job.get("company")
                and b.get("title") == job.get("title")
            ]

            if reorder:
                # Stable sort by relevance while preserving original order on ties
                job_bullets = sorted(
                    enumerate(job_bullets),
                    key=lambda pair: (-pair[1].get("relevance_score", 0), pair[0]),
                )
                job_bullets = [b for _, b in job_bullets]

            if max_bullets:
                job_bullets = job_bullets[:max_bullets]

            experience_data.append({
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "start_date": job.get("start_date", ""),
                "end_date": job.get("end_date", ""),
                "location": job.get("location", ""),
                # Bullet text is used verbatim — never rewritten by the LLM
                "bullets": [b["text"] for b in job_bullets],
                # Real technologies for this role (from the bullets' skill tags),
                # LLM-ranked against the JD. Nothing invented.
                "technologies": self._role_technologies(job_bullets, keywords, job_title),
            })

        # Prioritize skills that match the JD's keywords within each category
        all_skills = {}
        for category, skill_list in profile.get("skills", {}).items():
            if isinstance(skill_list, list):
                all_skills[category] = self._prioritize(skill_list, keywords)

        return {
            "personal": profile.get("personal", {}),
            "summary": summary,
            "experience": experience_data,
            "projects": _build_projects(profile.get("projects", []), keywords),
            "education": profile.get("education", []),
            "skills": all_skills,
            "certifications": profile.get("certifications", []),
            "target_keywords": keywords,
        }
