"""Resume builder — renders YAML data through Jinja2 templates to PDF."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2

from velvetoverride.utils.logging import get_logger

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


class ResumeBuilder:
    """Generates PDF resumes from structured data using Jinja2 templates."""

    def __init__(
        self,
        template_name: str = "default",
        output_dir: str | Path = "data/resumes",
        output_format: str = "html",
    ) -> None:
        self._template_name = template_name
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._output_format = output_format

        self._jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build(self, resume_data: dict[str, Any], filename: str) -> str:
        """Render the resume template and generate a PDF.

        Returns the path to the generated PDF.
        """
        template_file = f"{self._template_name}.html.j2"
        template = self._jinja_env.get_template(template_file)

        html_content = template.render(**resume_data)

        # Write intermediate HTML
        html_path = self._output_dir / f"{filename}.html"
        html_path.write_text(html_content)

        # Generate PDF from HTML
        pdf_path = self._output_dir / f"{filename}.pdf"
        try:
            self._html_to_pdf(html_content, pdf_path)
            log.info("resume.built", format="pdf", path=str(pdf_path))
            return str(pdf_path)
        except Exception as e:
            log.warning(
                "resume.pdf_fallback",
                error=str(e),
                msg="WeasyPrint not available; using HTML output",
            )
            log.info("resume.built", format="html", path=str(html_path))
            return str(html_path)

    def _html_to_pdf(self, html_content: str, output_path: Path) -> None:
        """Convert HTML to PDF using WeasyPrint."""
        try:
            from weasyprint import HTML
        except ImportError:
            raise ImportError(
                "WeasyPrint is required for PDF generation. "
                "Install it with: pip install weasyprint"
            )
        HTML(string=html_content).write_pdf(str(output_path))
