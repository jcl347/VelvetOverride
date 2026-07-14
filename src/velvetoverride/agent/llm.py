"""LLM client for form Q&A and resume tailoring.

Provider-agnostic: supports OpenAI (ChatGPT, default) and Anthropic Claude.
Tracks cumulative token usage so runs can report / budget token consumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from velvetoverride.utils.config import Config

log = get_logger(__name__)


# Approximate USD price per 1M tokens (prompt, completion) for cost estimation.
# These are only used for the local cost estimate shown in stats/dashboard;
# when data-sharing complimentary tokens are active, real cost can be $0.
_PRICING = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o4-mini": (1.10, 4.40),
    # Anthropic (fallback)
    "claude-sonnet-4-6": (3.00, 15.00),
}


@dataclass
class Usage:
    """Running tally of token consumption."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    est_cost_usd: float = 0.0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.calls += 1
        m = self.by_model.setdefault(
            model, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        )
        m["prompt_tokens"] += prompt
        m["completion_tokens"] += completion
        m["calls"] += 1
        in_price, out_price = _PRICING.get(model, (0.0, 0.0))
        self.est_cost_usd += (prompt / 1_000_000) * in_price
        self.est_cost_usd += (completion / 1_000_000) * out_price

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "est_cost_usd": round(self.est_cost_usd, 4),
            "by_model": self.by_model,
        }


class LLMClient:
    """Provider-agnostic LLM wrapper (OpenAI default, Anthropic fallback)."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._provider = config.llm.get("provider", "openai").lower()
        self.usage = Usage()

        if self._provider == "openai":
            api_key = config.openai_api_key
            if not api_key:
                raise ValueError(
                    "OPENAI_API_KEY not configured. Set it in config/.env"
                )
            import openai

            self._client = openai.OpenAI(api_key=api_key)
        elif self._provider == "anthropic":
            api_key = config.anthropic_api_key
            if not api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY not configured. Set it in config/.env"
                )
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key)
        else:
            raise ValueError(f"Unknown llm.provider: {self._provider!r}")

    # ── Provider-agnostic completion ──

    def _complete(self, prompt: str, model: str, max_tokens: int) -> str:
        """Run a single-prompt completion and record token usage."""
        if self._provider == "openai":
            return self._complete_openai(prompt, model, max_tokens)
        return self._complete_anthropic(prompt, model, max_tokens)

    def _complete_openai(self, prompt: str, model: str, max_tokens: int) -> str:
        messages = [{"role": "user", "content": prompt}]
        # Newer models (gpt-5, o-series) require max_completion_tokens; older ones
        # want max_tokens. Try the modern param first, but ONLY fall back when the
        # error is specifically about that unsupported param — otherwise a
        # transient/rate-limit error would be masked and retried wrongly.
        try:
            resp = self._client.chat.completions.create(
                model=model,
                max_completion_tokens=max_tokens,
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "max_completion_tokens" not in msg and "max_tokens" not in msg \
                    and "unsupported" not in msg and "unknown" not in msg:
                raise  # real error (rate limit, auth, network) — don't mask it
            log.warning("llm.openai_param_retry", error=str(e)[:120], model=model)
            resp = self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )

        text = ""
        if resp.choices:
            text = (resp.choices[0].message.content or "").strip()

        if resp.usage:
            self.usage.add(
                model,
                resp.usage.prompt_tokens or 0,
                resp.usage.completion_tokens or 0,
            )
        return text

    def _complete_anthropic(self, prompt: str, model: str, max_tokens: int) -> str:
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = self._extract_anthropic_text(resp)
        if getattr(resp, "usage", None):
            self.usage.add(
                model,
                resp.usage.input_tokens or 0,
                resp.usage.output_tokens or 0,
            )
        return text

    @staticmethod
    def _extract_anthropic_text(response) -> str:
        if not response.content:
            log.warning("llm.empty_response")
            return ""
        block = response.content[0]
        if hasattr(block, "text"):
            return block.text.strip()
        return str(block)

    # ── Model helpers ──

    @property
    def field_model(self) -> str:
        default = "gpt-4.1-mini" if self._provider == "openai" else "claude-sonnet-4-6"
        return self._config.llm.get("field_model", default)

    @property
    def resume_model(self) -> str:
        default = "gpt-4.1" if self._provider == "openai" else "claude-sonnet-4-6"
        return self._config.llm.get("resume_model", default)

    @property
    def fit_model(self) -> str:
        """Model for experience-fit judgement (reasoning-heavier than field Q&A)."""
        return self._config.llm.get("fit_model", self.field_model)

    # ── Public API (unchanged signatures) ──

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
        """Answer a form field question contextually."""
        options_text = ""
        if options:
            options_text = f"\nAvailable options: {', '.join(options)}"
            options_text += "\nYou MUST respond with one of the available options EXACTLY as written."

        prompt = f"""You are filling out a job application form for the role of "{job_title}" at "{company}".

Question: {question}
Field type: {field_type}{options_text}

Job description excerpt:
{job_description[:1500]}

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

        max_tokens = self._config.llm.get("field_max_tokens", 300)
        log.info("llm.field_query", question=question[:80], model=self.field_model)
        answer = self._complete(prompt, self.field_model, max_tokens)
        log.info("llm.field_answer", question=question[:40], answer=answer[:60])
        return answer

    def extract_keywords(self, job_description: str) -> list[str]:
        """Extract key skills and requirements from a job description."""
        prompt = f"""Extract the key technical skills, tools, frameworks, and requirements from this job description.
Return them as a comma-separated list, nothing else. Focus on concrete, specific skills.

Job description:
{job_description[:3000]}"""

        text = self._complete(prompt, self.field_model, 400)
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
        """Rewrite the professional summary to target a specific role.

        Guardrail: aligns wording/keywords only — never fabricates experience.
        """
        prompt = f"""Lightly rewrite this professional summary so it targets the role of "{job_title}" at "{company}".

Original summary:
{base_summary}

Relevant keywords from the job description: {', '.join(skills[:20])}

Job description excerpt:
{job_description[:1500]}

STRICT rules:
- Keep it to 2-3 sentences.
- Only surface keywords that the applicant genuinely already has, based on the original summary.
- Do NOT invent skills, tools, years of experience, titles, or metrics not in the original.
- Do NOT change or inflate any numbers or impact claims.
- Preserve the applicant's authentic voice and real experience.
- Return ONLY the rewritten summary, nothing else."""

        return self._complete(prompt, self.resume_model, 400)

    def score_bullet_relevance(
        self,
        bullets: list[dict],
        job_keywords: list[str],
        job_title: str,
    ) -> list[dict]:
        """Score and rank resume bullet points by relevance to a job.

        Only reorders/selects existing bullets — it never edits their text.
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

        text = self._complete(prompt, self.field_model, 500)

        scores = {}
        for line in text.split("\n"):
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

    def evaluate_fit(
        self,
        job_title: str,
        company: str,
        job_description: str,
        profile_summary: str,
    ) -> dict:
        """Judge how well the applicant actually fits a role.

        Answers the honest question — "am I really a lead / senior / staff?" —
        by comparing the JD's seniority and hard requirements against the
        applicant's real titles, years, and skills.

        Returns a dict with keys: fit_score (0-100), seniority
        ("under_qualified" | "match" | "over_qualified"), recommend
        ("apply" | "stretch" | "skip"), gaps (list[str]), reasoning (str).
        """
        prompt = f"""Assess whether this applicant genuinely fits this job. Be honest and critical — do not flatter.

ROLE: {job_title} at {company}

JOB DESCRIPTION:
{job_description[:3000]}

APPLICANT (their real background):
{profile_summary[:1800]}

Evaluate:
- Seniority: does the applicant's actual title/scope history support this level?
  (e.g. do NOT call them a Lead/Staff/Principal unless their real experience shows it)
- Years of experience versus what the role requires.
- Hard requirements they clearly do not meet.

Respond with ONLY a JSON object, no markdown fence, exactly these keys:
{{"fit_score": <0-100 integer>,
  "seniority": "under_qualified" | "match" | "over_qualified",
  "recommend": "apply" | "stretch" | "skip",
  "gaps": ["<missing requirement>", ...],
  "reasoning": "<2 sentences max>"}}

Guidance: "apply" = strong fit. "stretch" = plausible but a reach. "skip" = clearly not qualified or badly mismatched in level."""

        raw = self._complete(prompt, self.fit_model, 300)
        verdict = self._parse_fit(raw)
        log.info(
            "llm.fit_evaluated",
            title=job_title[:40],
            score=verdict["fit_score"],
            seniority=verdict["seniority"],
            recommend=verdict["recommend"],
        )
        return verdict

    @staticmethod
    def _parse_fit(raw: str) -> dict:
        """Parse the fit JSON defensively; never raise."""
        import json
        import re as _re

        default = {
            "fit_score": 50,
            "seniority": "match",
            "recommend": "stretch",
            "gaps": [],
            "reasoning": "",
        }
        if not raw:
            return default

        text = raw.strip()
        # Strip a ```json fence if the model added one
        if text.startswith("```"):
            text = _re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        m = _re.search(r"\{.*\}", text, _re.S)
        if not m:
            log.warning("llm.fit_unparseable", raw=text[:120])
            return default
        try:
            data = json.loads(m.group(0))
        except Exception:
            log.warning("llm.fit_bad_json", raw=text[:120])
            return default

        out = dict(default)
        try:
            out["fit_score"] = max(0, min(100, int(data.get("fit_score", 50))))
        except (TypeError, ValueError):
            pass
        if data.get("seniority") in ("under_qualified", "match", "over_qualified"):
            out["seniority"] = data["seniority"]
        if data.get("recommend") in ("apply", "stretch", "skip"):
            out["recommend"] = data["recommend"]
        gaps = data.get("gaps")
        if isinstance(gaps, list):
            out["gaps"] = [str(g)[:120] for g in gaps[:6]]
        out["reasoning"] = str(data.get("reasoning", ""))[:400]
        return out

    def optimize_keywords(
        self,
        candidate_terms: list[str],
        job_keywords: list[str],
        job_title: str,
        limit: int = 12,
    ) -> list[str]:
        """Order the applicant's OWN terms by relevance to the job.

        The model may only reorder/select from ``candidate_terms`` — the result
        is filtered back against that list, so it can never introduce a skill
        the applicant doesn't actually have.
        """
        if not candidate_terms:
            return []

        prompt = f"""Rank these skills by how relevant they are to a "{job_title}" role.

Applicant's skills (choose ONLY from this list):
{', '.join(candidate_terms)}

Job's required keywords:
{', '.join(job_keywords[:30])}

Rules:
- Return a comma-separated list, most relevant first.
- Use ONLY skills from the applicant's list, spelled exactly as given.
- Do NOT add any skill that is not in the applicant's list.
- Return at most {limit} skills. Nothing else."""

        try:
            text = self._complete(prompt, self.field_model, 150)
        except Exception as e:  # noqa: BLE001
            log.warning("llm.optimize_keywords_failed", error=str(e)[:100])
            return candidate_terms[:limit]

        lookup = {t.lower().strip(): t for t in candidate_terms}
        ordered: list[str] = []
        for raw in text.split(","):
            key = raw.strip().lower()
            if key in lookup and lookup[key] not in ordered:
                ordered.append(lookup[key])

        # Append any real terms the model omitted, so nothing is silently lost
        for t in candidate_terms:
            if t not in ordered:
                ordered.append(t)

        log.info("llm.keywords_optimized", kept=len(ordered[:limit]), of=len(candidate_terms))
        return ordered[:limit]

    def fix_field_value(
        self,
        question: str,
        error_message: str,
        current_value: str,
        field_type: str = "text",
        job_title: str = "",
        company: str = "",
        profile_summary: str = "",
    ) -> str:
        """Given a form field's validation error, return a corrected value.

        Used when the mechanical fixers (numeric cleanup, etc.) can't resolve an
        inline error — we hand the error text to the model and ask for a value
        that will satisfy it.
        """
        prompt = f"""A job application form rejected a field value. Provide a corrected value.

Field question: {question}
Field type: {field_type}
Value entered: {current_value!r}
Validation error shown: {error_message}

Applicant profile:
{profile_summary[:600]}

Rules:
- Return ONLY the corrected value to type into the field. No explanation, no quotes.
- Obey the validation error exactly (e.g. whole number, max length, date format, decimal places).
- If it wants a number, return digits only.
- Stay truthful to the applicant's profile. Do not invent experience.
- If the error cannot be satisfied, return the single word: SKIP"""

        answer = self._complete(prompt, self.field_model, 60).strip().strip('"').strip("'")
        if answer.upper() == "SKIP":
            return ""
        log.info("llm.field_fixed", question=question[:50], error=error_message[:60], value=answer[:40])
        return answer

    def choose_next_action(
        self,
        button_labels: list[str],
        job_title: str,
        company: str,
        page_summary: str = "",
        allow_submit: bool = True,
    ) -> str:
        """Pick which button advances an external application form.

        Used by the dynamic (non-Easy-Apply) navigator to move through arbitrary
        ATS pages (Workday, Greenhouse, Lever, ...). Returns the exact button
        label to click, or "" if none should be clicked.
        """
        if not button_labels:
            return ""

        numbered = "\n".join(f"{i+1}. {b}" for i, b in enumerate(button_labels))
        submit_rule = (
            "- Prefer buttons that advance the form (Next, Continue, Save and continue, Review)."
            if not allow_submit
            else "- Choose the button that advances or submits the application "
            "(Next, Continue, Review, Submit)."
        )
        prompt = f"""You are navigating an online job application for "{job_title}" at "{company}".
Below are the clickable buttons currently on the page.

Buttons:
{numbered}

Page context: {page_summary[:400]}

Rules:
{submit_rule}
- NEVER choose Cancel, Back, Save draft, Sign out, or anything that abandons the application.
- If none of the buttons advance the application, answer NONE.
- Answer with the EXACT button label only, nothing else."""

        choice = self._complete(prompt, self.field_model, 30).strip()
        if choice.upper() == "NONE" or not choice:
            return ""
        # Normalize to an actual button label
        for b in button_labels:
            if b.lower() == choice.lower() or choice.lower() in b.lower() or b.lower() in choice.lower():
                return b
        return ""

    def generate_cover_letter_snippet(
        self,
        prompt_label: str,
        job_title: str,
        company: str,
        job_description: str,
        profile_summary: str,
    ) -> str:
        """Answer an open-ended application prompt (cover letter, 'why
        interested', 'tell us about yourself', 'anything else', …) with a
        compelling narrative that DIRECTLY addresses that specific prompt and is
        grounded in the applicant's real background.

        ``prompt_label`` is the actual form question so the answer targets it —
        rather than always answering a hardcoded "why interested".
        """
        label = (prompt_label or "").strip() or "Why are you interested in this role?"
        is_cover_letter = any(
            k in label.lower() for k in ("cover letter", "letter of interest")
        )
        length_rule = (
            "Write two short paragraphs (a genuine cover letter body)."
            if is_cover_letter
            else "Write 3-5 sentences."
        )
        prompt = f"""A job applicant must answer this application question:

QUESTION: "{label}"

For the role "{job_title}" at "{company}".

Job description excerpt:
{job_description[:1400]}

Applicant's real background (use ONLY these facts — never invent skills,
employers, metrics, titles, or credentials):
{profile_summary}

Write the applicant's answer. Requirements:
- Directly and specifically answer THAT question — do not paste a generic
  cover letter or answer a different question.
- Ground every claim in the real background above; cite concrete experience or
  achievements from it. Never fabricate.
- Connect the applicant's actual experience to this specific role and company.
- Be genuine and concrete — no clichés, no flattery, no buzzword padding.
- {length_rule}
- Return ONLY the answer text (no preamble, no "Dear Hiring Manager" unless it
  is explicitly a cover letter)."""

        return self._complete(prompt, self.field_model, 600)
