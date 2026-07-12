"""Resume builder — renders YAML data through Jinja2 templates to PDF."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2

from velvetoverride.utils.logging import get_logger

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def available_pdf_engine() -> str | None:
    """Return the name of the first importable PDF engine, or None."""
    try:
        import weasyprint  # noqa: F401
        return "weasyprint"
    except Exception:
        pass
    try:
        from xhtml2pdf import pisa  # noqa: F401
        return "xhtml2pdf"
    except Exception:
        pass
    return None


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

        # Fail loudly at startup if no PDF engine works, rather than silently
        # degrading to unusable .html resumes at apply time.
        engine = available_pdf_engine()
        if engine:
            log.info("resume.pdf_engine_ready", engine=engine)
        else:
            log.error(
                "resume.no_pdf_engine",
                msg="No PDF engine importable — resumes cannot be generated. "
                    "Install xhtml2pdf (pip install xhtml2pdf) or weasyprint.",
            )

        self._jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_html(self, resume_data: dict[str, Any]) -> str:
        """Render the resume template to an HTML string (no file written)."""
        template_file = f"{self._template_name}.html.j2"
        template = self._jinja_env.get_template(template_file)
        return template.render(**resume_data)

    def build(self, resume_data: dict[str, Any], filename: str) -> str:
        """Render the resume template and generate a PDF.

        Returns the path to the generated PDF. Raises RuntimeError if no PDF
        engine is available — we must NEVER return a .html path here, because
        LinkedIn/ATS resume uploads reject non-PDF/DOC files and the application
        would silently fail while logging success.
        """
        html_content = self.render_html(resume_data)
        pdf_path = self._output_dir / f"{filename}.pdf"
        self._html_to_pdf(html_content, pdf_path)  # raises if no engine
        log.info("resume.built", format="pdf", path=str(pdf_path))
        return str(pdf_path)

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
