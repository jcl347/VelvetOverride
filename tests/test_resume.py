"""Tests for resume builder and ATS scorer."""

from pathlib import Path

import pytest

from velvetoverride.resume.scorer import ATSScorer


@pytest.fixture
def sample_resume_data():
    return {
        "personal": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@test.com",
        },
        "summary": "Experienced Python developer with expertise in REST APIs and cloud infrastructure.",
        "experience": [
            {
                "title": "Software Engineer",
                "company": "Acme Corp",
                "start_date": "2022-01",
                "end_date": "present",
                "location": "Remote",
                "bullets": [
                    "Built REST APIs using Python and FastAPI serving 1M requests/day",
                    "Deployed microservices on AWS using Docker and Kubernetes",
                    "Implemented CI/CD pipelines with GitHub Actions",
                ],
            }
        ],
        "skills": {
            "languages": ["Python", "Go", "TypeScript"],
            "frameworks": ["FastAPI", "React"],
            "infrastructure": ["AWS", "Docker", "Kubernetes"],
        },
        "education": [
            {
                "degree": "BS",
                "field": "Computer Science",
                "institution": "MIT",
                "graduation_date": "2021",
            }
        ],
        "certifications": [{"name": "AWS Solutions Architect", "date": "2023"}],
    }


class TestATSScorer:
    def test_perfect_coverage(self, sample_resume_data):
        scorer = ATSScorer(target_coverage=0.70)
        keywords = ["python", "rest api", "aws", "docker"]
        score = scorer.score(sample_resume_data, keywords)
        assert score >= 80

    def test_partial_coverage(self, sample_resume_data):
        scorer = ATSScorer(target_coverage=0.70)
        keywords = ["python", "rust", "elixir", "haskell", "scala"]
        score = scorer.score(sample_resume_data, keywords)
        assert 0 < score < 100

    def test_no_keywords(self, sample_resume_data):
        scorer = ATSScorer()
        score = scorer.score(sample_resume_data, [])
        assert score == 100.0

    def test_keyword_variations(self, sample_resume_data):
        scorer = ATSScorer()
        # "ci/cd" should match "CI/CD" in bullets
        assert scorer._keyword_present("ci/cd", "implemented ci/cd pipelines")
        assert scorer._keyword_present("ci cd", "implemented ci/cd pipelines")

    def test_suggestions(self, sample_resume_data):
        scorer = ATSScorer()
        keywords = ["python", "java", "spring boot"]
        result = scorer.get_suggestions(sample_resume_data, keywords)
        assert "python" in result["matched_keywords"]
        assert "java" in result["missing_keywords"]
        assert isinstance(result["score"], float)


class TestResumeBuilder:
    def test_template_exists(self):
        from velvetoverride.resume.builder import TEMPLATES_DIR
        template = TEMPLATES_DIR / "default.html.j2"
        assert template.exists()

    def test_build_html(self, sample_resume_data, tmp_path):
        from velvetoverride.resume.builder import ResumeBuilder

        builder = ResumeBuilder(output_dir=tmp_path)
        path = builder.build(sample_resume_data, "test_resume")

        assert Path(path).exists()

        # If PDF was generated, check the intermediate HTML instead
        html_path = tmp_path / "test_resume.html"
        assert html_path.exists(), "Intermediate HTML should always be written"
        content = html_path.read_text()
        assert "Jane" in content
        assert "Doe" in content
        assert "Python" in content
