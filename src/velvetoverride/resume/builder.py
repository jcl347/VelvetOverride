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

        # Write intermediate HTML (explicit UTF-8; Windows defaults to cp1252)
        html_path = self._output_dir / f"{filename}.html"
        html_path.write_text(html_content, encoding="utf-8")

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
        """Convert HTML to PDF.

        Tries WeasyPrint first (best fidelity, needs GTK system libs), then
        xhtml2pdf (pure-Python, works everywhere incl. Windows with no system
        deps). Raises if neither is available so the caller can fall back to HTML.
        """
        # Engine 1: WeasyPrint
        try:
            from weasyprint import HTML

            HTML(string=html_content).write_pdf(str(output_path))
            return
        except Exception as e:  # ImportError or GTK/runtime error
            log.debug("resume.weasyprint_unavailable", error=str(e)[:120])

        # Engine 2: xhtml2pdf (pure Python)
        try:
            from xhtml2pdf import pisa

            # xhtml2pdf mangles raw non-ASCII into mojibake ("—" -> "â€”").
            # Escaping to XML character refs ("&#8212;") renders correctly and
            # also protects unicode in company names (e.g. "mani™").
            safe_html = html_content.encode("ascii", "xmlcharrefreplace").decode("ascii")

            with open(output_path, "wb") as f:
                result = pisa.CreatePDF(src=safe_html, dest=f, encoding="utf-8")
            if result.err:
                raise RuntimeError(f"xhtml2pdf reported {result.err} errors")
            log.debug("resume.pdf_engine", engine="xhtml2pdf")
            return
        except Exception as e:
            log.debug("resume.xhtml2pdf_failed", error=str(e)[:120])

        raise RuntimeError(
            "No PDF engine available (install weasyprint or xhtml2pdf)."
        )
