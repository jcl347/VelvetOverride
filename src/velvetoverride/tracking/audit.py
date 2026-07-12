"""Field-routing anomaly heuristic — shared by the run-end audit and the
dashboard feedback view so both flag the same mis-routings (e.g. a name field
that received a sentence, or a zip field that received a guessed value)."""

from __future__ import annotations

import re

_NAME_KW = ("first name", "last name", "full name", "your name",
            "legal name", "given name", "surname")
_NAME_EXCLUDE = ("company", "file", "user", "reference", "manager", "supervisor")


def flag_routing_problem(label: str, answer: str, field_type: str, source: str) -> str | None:
    """Return a short description if (label, answer) looks mis-routed, else None."""
    label = (label or "").lower().strip()
    ans = (answer or "").strip()
    ft = field_type or ""
    src = source or ""
    if not ans or ans.startswith("MISSING"):
        return None
    digits = re.sub(r"\D", "", ans)

    is_name = label == "name" or any(k in label for k in _NAME_KW)
    if is_name and not any(x in label for x in _NAME_EXCLUDE):
        if any(ch.isdigit() for ch in ans) or len(ans) > 50 or len(ans.split()) > 5:
            return "name field got a non-name value"
    elif "zip" in label or "postal" in label:
        if len(digits) not in (5, 9):
            return "zip/postal not a valid 5/9-digit code"
    elif "email" in label and "@" not in ans:
        return "email field without '@'"
    elif ("phone" in label or "mobile" in label) and "code" not in label and "country" not in label:
        if len(digits) < 7:
            return "phone field is not phone-like"
    elif ft == "numeric" and not re.search(r"\d", ans):
        return "numeric field got a non-numeric answer"

    if src == "llm" and ft == "text" and len(ans) > 180:
        return "long LLM answer in a single-line text field"
    return None
