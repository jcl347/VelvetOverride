"""Claude API client for form Q&A and resume tailoring."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anthropic

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.utils.config import Config

log = get_logger(__name__)


class LLMClient:
    """Thin wrapper around the Anthropic Claude API."""

    def __init__(self, config: Config) -> None:
        api_key = config.anthropic_api_key
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not configured. Set it in config/.env"
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._config = config

    def answer_field(
        self,
        question: str,
        field_type: str,
        options: list[str] | None,
        job_description: str,
        job_title: str,
        company: str,
        profile_summary: str,
    ) -> str:
        """Ask Claude to answer a form field question contextually."""
        options_text = ""
        if options:
            options_text = f"\nAvailable options: {', '.join(options)}"
            options_text += "\nYou MUST respond with one of the available options EXACTLY as written."

        prompt = f"""You are filling out a job application form for the role of "{job_title}" at "{company}".

Question: {question}
Field type: {field_type}{options_text}

Job description excerpt:
{job_description[:2000]}

Applicant profile:
{profile_summary}

Rules:
- Answer ONLY with the value to enter in the field. No explanation.
- For Yes/No questions, answer "Yes" or "No".
- For numeric questions, answer with just the number.
- For text fields, keep answers concise and professional.
- For EEO/demographic questions, answer "Decline to self-identify" or "Prefer not to say".
- If the question asks about a technology, answer with years of experience.
- Be honest. Do not fabricate experience."""

        model = self._config.llm.get("field_model", "claude-sonnet-4-6")
        max_tokens = self._config.llm.get("field_max_tokens", 300)

        log.info("llm.field_query", question=question[:80], model=model)

        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        answer = response.content[0].text.strip()
        log.info("llm.field_answer", question=question[:40], answer=answer[:60])
        return answer

    def extract_keywords(self, job_description: str) -> list[str]:
        """Extract key skills and requirements from a job description."""
        prompt = f"""Extract the key technical skills, tools, frameworks, and requirements from this job description.
Return them as a comma-separated list, nothing else. Focus on concrete, specific skills.

Job description:
{job_description[:3000]}"""

        model = self._config.llm.get("field_model", "claude-sonnet-4-6")

        response = self._client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        keywords = [kw.strip().lower() for kw in text.split(",") if kw.strip()]
        log.info("llm.keywords_extracted", count=len(keywords))
        return keywords

    def tailor_summary(
        self,
        base_summary: str,
        job_title: str,
        company: str,
        job_description: str,
        skills: list[str],
    ) -> str:
        """Rewrite the professional summary to target a specific role."""
        prompt = f"""Rewrite this professional summary to be tailored for the role of "{job_title}" at "{company}".

Original summary:
{base_summary}

Key skills from job description: {', '.join(skills[:20])}

Job description excerpt:
{job_description[:2000]}

Rules:
- Keep it to 2-3 sentences.
- Naturally incorporate relevant keywords from the job description.
- Maintain the applicant's authentic voice and real experience.
- Do not fabricate skills or experience not present in the original.
- Return ONLY the rewritten summary, nothing else."""

        model = self._config.llm.get("resume_model", "claude-sonnet-4-6")

        response = self._client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text.strip()

    def score_bullet_relevance(
        self,
        bullets: list[dict],
        job_keywords: list[str],
        job_title: str,
    ) -> list[dict]:
        """Score and rank resume bullet points by relevance to a job.

        Each bullet dict should have 'text' and 'skills' keys.
        Returns the same list with an added 'relevance_score' key (0-100).
        """
        bullets_text = "\n".join(
            f"{i+1}. {b['text']} [skills: {', '.join(b.get('skills', []))}]"
            for i, b in enumerate(bullets)
        )

        prompt = f"""Rate each resume bullet point's relevance to a "{job_title}" role.
Target skills: {', '.join(job_keywords[:25])}

Bullet points:
{bullets_text}

For each bullet, return ONLY a line in the format: <number>: <score>
where score is 0-100 (100 = perfectly relevant).
Return nothing else."""

        model = self._config.llm.get("field_model", "claude-sonnet-4-6")

        response = self._client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        # Parse scores
        scores = {}
        for line in response.content[0].text.strip().split("\n"):
            line = line.strip()
            if ":" in line:
                parts = line.split(":")
                try:
                    idx = int(parts[0].strip()) - 1
                    score = int(parts[1].strip())
                    scores[idx] = score
                except (ValueError, IndexError):
                    continue

        for i, bullet in enumerate(bullets):
            bullet["relevance_score"] = scores.get(i, 50)

        return bullets

    def generate_cover_letter_snippet(
        self,
        job_title: str,
        company: str,
        job_description: str,
        profile_summary: str,
    ) -> str:
        """Generate a short cover letter / 'why interested' response."""
        prompt = f"""Write a brief, compelling answer for "Why are you interested in this role?" for a "{job_title}" position at "{company}".

Job description excerpt:
{job_description[:1500]}

Applicant background:
{profile_summary}

Rules:
- 3-4 sentences maximum.
- Be specific about the company and role.
- Connect the applicant's experience to the role.
- Sound genuine, not generic.
- Return ONLY the answer text."""

        model = self._config.llm.get("field_model", "claude-sonnet-4-6")

        response = self._client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text.strip()
