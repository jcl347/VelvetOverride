"""Non-ASCII must survive YAML load and PDF rendering (no mojibake)."""

import re

from velvetoverride.resume.builder import ResumeBuilder
from velvetoverride.utils.config import load_yaml

# The string an em dash turns into when UTF-8 is misread as cp1252
MOJIBAKE = "â€"


def _data(**over):
    base = {
        "personal": {"first_name": "Jordan", "last_name": "Limperis", "city": "Seattle"},
        "summary": "Engineer — builder of systems.",  # em dash
        "experience": [],
        "projects": [
            {"name": "Put Sale App", "detail": "Predictor — 79% accuracy",
             "url": "https://github.com/jl-ml", "tech": ["Python"]},
        ],
        "education": [],
        "skills": {},
        "certifications": [],
        "target_keywords": [],
    }
    base.update(over)
    return base


def test_yaml_loads_as_utf8_not_cp1252(tmp_path):
    """The real bug: open() without encoding mangles an em dash on Windows."""
    p = tmp_path / "p.yaml"
    p.write_text('detail: "Predictor — 79% accuracy"\n', encoding="utf-8")
    detail = load_yaml(p)["detail"]
    assert "—" in detail
    assert MOJIBAKE not in detail
    assert detail.count("—") == 1  # one char, not three


def test_em_dash_survives_pdf_render(tmp_path):
    """The em dash must reach the rendered HTML intact (no mojibake)."""
    b = ResumeBuilder(output_dir=tmp_path)
    path = b.build(_data(), "encoding_test")
    assert path.endswith(".pdf"), "expected a PDF (is a PDF engine installed?)"
    html = b.render_html(_data())
    assert MOJIBAKE not in html
    assert "Predictor — 79% accuracy" in html


def test_trademark_and_accents_do_not_crash(tmp_path):
    """Company names like 'mani™' or 'Café' must not break rendering."""
    b = ResumeBuilder(output_dir=tmp_path)
    data = _data(summary="Worked at mani™ and Café Corp — shipped things.")
    path = b.build(data, "unicode_test")
    assert path.endswith(".pdf")


def test_rendered_html_is_utf8_clean(tmp_path):
    b = ResumeBuilder(output_dir=tmp_path)
    html = b.render_html(_data())
    assert "—" in html  # readable UTF-8, not cp1252-mangled


def test_build_never_returns_html(tmp_path):
    """build() must return a .pdf, never the intermediate HTML."""
    b = ResumeBuilder(output_dir=tmp_path)
    path = b.build(_data(), "nohtml_test")
    assert path.endswith(".pdf")
    # And it should not leave a stray .html sidecar behind
    assert not (tmp_path / "noml_test.html").exists()


def test_project_link_is_embedded(tmp_path):
    b = ResumeBuilder(output_dir=tmp_path)
    path = b.build(_data(), "link_test")
    raw = open(path, "rb").read()
    assert b"jl-ml" in raw


def test_resume_fits_one_page(tmp_path):
    b = ResumeBuilder(output_dir=tmp_path)
    path = b.build(_data(), "onepage_test")
    raw = open(path, "rb").read()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    assert pages == 1, f"expected 1 page, got {pages}"
