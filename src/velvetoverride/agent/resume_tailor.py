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
            tailored_summary, scored_bullets, keywords
        )

        # Step 5: Generate PDF
        safe_company = "".join(c if c.isalnum() else "_" for c in company)
        safe_title = "".join(c if c.isalnum() else "_" for c in job_title)
        filename = f"resume_{safe_company}_{safe_title}"
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

    def _build_tailored_data(
        self,
        summary: str,
        scored_bullets: list[dict],
        keywords: list[str],
    ) -> dict:
        """Assemble the full resume data for template rendering."""
        profile = self._config.profile

        # Group bullets back by job, ordered by relevance
        experience_data = []
        for job in profile.get("experience", []):
            job_bullets = [
                b for b in scored_bullets
                if b.get("company") == job.get("company")
                and b.get("title") == job.get("title")
            ]
            # Sort by relevance, keep top bullets
            job_bullets.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
            # Keep top 4 bullets per job for a concise resume
            top_bullets = job_bullets[:4]

            experience_data.append({
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "start_date": job.get("start_date", ""),
                "end_date": job.get("end_date", ""),
                "location": job.get("location", ""),
                "bullets": [b["text"] for b in top_bullets],
            })

        # Prioritize skills that match JD keywords
        all_skills = {}
        for category, skill_list in profile.get("skills", {}).items():
            if isinstance(skill_list, list):
                all_skills[category] = skill_list

        return {
            "personal": profile.get("personal", {}),
            "summary": summary,
            "experience": experience_data,
            "education": profile.get("education", []),
            "skills": all_skills,
            "certifications": profile.get("certifications", []),
            "target_keywords": keywords,
        }
