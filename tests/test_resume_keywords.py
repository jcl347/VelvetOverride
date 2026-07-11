"""Tests for JD keyword surfacing in the Experience section.

The critical invariant: technical terms shown on the resume must come from the
applicant's own skill tags — a JD keyword the applicant lacks must NEVER appear.
"""

from velvetoverride.agent.resume_tailor import (
    ResumeTailor,
    _build_projects,
    _pretty_term,
)


class _StubLLM:
    """Stands in for ChatGPT; tries to smuggle in a skill the applicant lacks."""

    def __init__(self, reply):
        self.reply = reply

    def optimize_keywords(self, candidate_terms, job_keywords, job_title, limit=12):
        # Mimic the real method's filtering contract
        lookup = {t.lower(): t for t in candidate_terms}
        ordered = []
        for raw in self.reply.split(","):
            key = raw.strip().lower()
            if key in lookup and lookup[key] not in ordered:
                ordered.append(lookup[key])
        for t in candidate_terms:
            if t not in ordered:
                ordered.append(t)
        return ordered[:limit]


def _tailor(llm=None):
    t = ResumeTailor.__new__(ResumeTailor)  # bypass __init__ (no config needed)
    t._llm = llm
    return t


BULLETS = [
    {"skills": ["python", "pyspark", "azure", "llm"]},
    {"skills": ["sql", "data modeling"]},
]


# ── Casing ──

def test_pretty_term_acronyms():
    assert _pretty_term("sql") == "SQL"
    assert _pretty_term("pyspark") == "PySpark"
    assert _pretty_term("llm") == "LLM"
    assert _pretty_term("nlp") == "NLP"
    assert _pretty_term("data modeling") == "Data Modeling"


def test_pretty_term_preserves_author_casing():
    assert _pretty_term("PyTorch Lightning") == "PyTorch Lightning"


# ── No fabrication ──

def test_never_invents_a_jd_skill_heuristic():
    jd = ["kubernetes", "terraform", "pyspark"]
    techs = _tailor()._role_technologies(BULLETS, jd, "ML Engineer")
    assert "Kubernetes" not in techs
    assert "Terraform" not in techs
    assert "PySpark" in techs


def test_never_invents_a_jd_skill_even_if_llm_hallucinates():
    """If the model returns a skill the applicant lacks, it must be dropped."""
    llm = _StubLLM("Kubernetes, Terraform, PySpark, LLM")
    techs = _tailor(llm)._role_technologies(BULLETS, ["kubernetes"], "ML Engineer")
    assert "Kubernetes" not in techs
    assert "Terraform" not in techs
    assert techs[0] == "PySpark"  # real, model-ranked term wins


def test_llm_ranking_is_used_when_available():
    llm = _StubLLM("Azure, SQL")
    techs = _tailor(llm)._role_technologies(BULLETS, ["azure"], "Data Engineer")
    assert techs[0] == "Azure"


def test_falls_back_to_heuristic_when_llm_raises():
    class Boom:
        def optimize_keywords(self, *a, **k):
            raise RuntimeError("api down")

    techs = _tailor(Boom())._role_technologies(BULLETS, ["pyspark"], "ML Engineer")
    assert techs[0] == "PySpark"  # heuristic still puts the JD match first


# ── Prioritization keeps all real terms ──

def test_prioritize_keeps_every_term():
    terms = ["Java", "PySpark", "SQL"]
    out = ResumeTailor._prioritize(terms, ["pyspark"])
    assert out[0] == "PySpark"
    assert sorted(out) == sorted(terms)


def test_role_technologies_dedupes():
    bullets = [{"skills": ["python", "Python"]}, {"skills": ["python"]}]
    techs = _tailor()._role_technologies(bullets, [])
    assert techs == ["Python"]


def test_empty_bullets_returns_empty():
    assert _tailor()._role_technologies([], ["python"]) == []


# ── Projects section ──

PROJECTS = [
    {"name": "Put Sale App", "detail": "79% directional accuracy",
     "url": "https://github.com/jl-ml", "tech": ["python", "machine learning"]},
    {"name": "Autonomous Research Agent", "detail": "Multi-agent",
     "url": "", "tech": ["llm", "python"]},
]


def test_projects_keep_name_detail_and_url():
    out = _build_projects(PROJECTS)
    assert out[0]["name"] == "Put Sale App"
    assert out[0]["url"] == "https://github.com/jl-ml"
    assert out[0]["detail"] == "79% directional accuracy"


def test_projects_tech_gets_canonical_casing():
    out = _build_projects(PROJECTS)
    assert out[1]["tech"][0] in ("LLM", "Python")
    assert "LLM" in out[1]["tech"]


def test_projects_tech_prioritized_by_jd_keywords():
    out = _build_projects(PROJECTS, ["machine learning"])
    assert out[0]["tech"][0] == "Machine Learning"


def test_projects_never_add_jd_keywords():
    out = _build_projects(PROJECTS, ["kubernetes", "rust"])
    for p in out:
        assert "Kubernetes" not in p["tech"]
        assert "Rust" not in p["tech"]


def test_projects_allow_missing_url():
    out = _build_projects(PROJECTS)
    assert out[1]["url"] == ""  # renders unlinked


def test_projects_skip_unnamed_entries():
    assert _build_projects([{"detail": "orphan"}]) == []


def test_projects_empty_input():
    assert _build_projects([]) == []
    assert _build_projects(None) == []
