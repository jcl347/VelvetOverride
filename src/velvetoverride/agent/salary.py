"""Salary range extraction from job descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from velvetoverride.utils.logging import get_logger

log = get_logger(__name__)

# Patterns to match salary ranges in job descriptions
# Handles: $120,000 - $150,000, $120K-$150K, 120k - 150k/yr, $55/hr - $75/hr, etc.
SALARY_PATTERNS = [
    # $120,000 - $180,000 (annual with commas)
    re.compile(
        r"\$\s*([\d,]+)\s*(?:[-–—to]+)\s*\$\s*([\d,]+)\s*"
        r"(?:per\s+(?:year|annum|yr)|/\s*(?:year|yr|annual)|annually|a\s+year)?",
        re.IGNORECASE,
    ),
    # $120K - $180K or $120k-$180k
    re.compile(
        r"\$\s*(\d+)\s*[kK]\s*(?:[-–—to]+)\s*\$?\s*(\d+)\s*[kK]",
        re.IGNORECASE,
    ),
    # 120,000 - 180,000 (no dollar sign but with context)
    re.compile(
        r"(?:salary|compensation|pay|range|base)[:\s]*"
        r"(?:\$\s*)?([\d,]+)\s*(?:[-–—to]+)\s*(?:\$\s*)?([\d,]+)",
        re.IGNORECASE,
    ),
    # $55/hr - $75/hr or $55 - $75 per hour
    re.compile(
        r"\$\s*(\d+(?:\.\d+)?)\s*(?:/\s*h(?:ou)?r)?\s*(?:[-–—to]+)\s*\$?\s*(\d+(?:\.\d+)?)\s*"
        r"(?:per\s+hour|/\s*h(?:ou)?r|hourly|an?\s+hour)",
        re.IGNORECASE,
    ),
    # Single salary: $150,000 or $150K
    re.compile(
        r"(?:up\s+to|starting\s+at|from|base[:\s]*)\s*\$\s*(\d[\d,]*)\s*[kK]?",
        re.IGNORECASE,
    ),
]

# Patterns that indicate hourly rates
HOURLY_INDICATORS = re.compile(
    r"per\s+hour|/\s*h(?:ou)?r|hourly|an?\s+hour", re.IGNORECASE
)


@dataclass
class SalaryRange:
    """Extracted salary range from a job description."""

    min_salary: int
    max_salary: int
    is_hourly: bool = False
    raw_text: str = ""

    @property
    def annual_min(self) -> int:
        """Annualized minimum (assumes 2,080 hours/year for hourly)."""
        return self.min_salary * 2080 if self.is_hourly else self.min_salary

    @property
    def annual_max(self) -> int:
        """Annualized maximum."""
        return self.max_salary * 2080 if self.is_hourly else self.max_salary

    @property
    def midpoint(self) -> int:
        """Midpoint of the annual range."""
        return (self.annual_min + self.annual_max) // 2

    def __str__(self) -> str:
        if self.is_hourly:
            return f"${self.min_salary}/hr - ${self.max_salary}/hr (≈${self.annual_min:,}-${self.annual_max:,}/yr)"
        return f"${self.min_salary:,} - ${self.max_salary:,}"


def extract_salary(description: str) -> SalaryRange | None:
    """Extract salary range from a job description.

    Returns a SalaryRange if found, or None if no salary info detected.
    """
    if not description:
        return None

    for pattern in SALARY_PATTERNS:
        match = pattern.search(description)
        if not match:
            continue

        groups = match.groups()
        raw_text = match.group(0)

        try:
            if len(groups) >= 2:
                val1 = _parse_salary_value(groups[0])
                val2 = _parse_salary_value(groups[1])
                if val1 is None or val2 is None:
                    continue
                min_sal = min(val1, val2)
                max_sal = max(val1, val2)
            elif len(groups) == 1:
                val = _parse_salary_value(groups[0])
                if val is None:
                    continue
                min_sal = val
                max_sal = val
            else:
                continue

            # Detect if it's an hourly rate
            is_hourly = bool(HOURLY_INDICATORS.search(raw_text))

            # Handle K suffix (already handled by _parse_salary_value for "$120K" patterns)
            # But for the K-specific pattern, multiply by 1000
            if "k" in raw_text.lower() and max_sal < 1000:
                min_sal *= 1000
                max_sal *= 1000

            # Sanity checks
            if is_hourly:
                if min_sal < 10 or max_sal > 500:
                    continue
            else:
                if min_sal < 20000 or max_sal > 1_000_000:
                    continue
                # If values are too small, they might be in K
                if min_sal < 1000:
                    min_sal *= 1000
                    max_sal *= 1000

            salary = SalaryRange(
                min_salary=min_sal,
                max_salary=max_sal,
                is_hourly=is_hourly,
                raw_text=raw_text.strip(),
            )
            log.debug("salary.extracted", salary=str(salary), raw=raw_text.strip())
            return salary

        except (ValueError, IndexError):
            continue

    return None


def _parse_salary_value(raw: str) -> int | None:
    """Parse a raw salary string into an integer."""
    if not raw:
        return None
    cleaned = raw.replace(",", "").replace("$", "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
        return int(value)
    except ValueError:
        return None


def salary_in_range(
    salary: SalaryRange | None,
    min_annual: int | None,
    max_annual: int | None,
) -> bool:
    """Check if a salary range overlaps with the desired range.

    Returns True if:
    - No salary was extracted (benefit of the doubt)
    - No filter is set
    - The ranges overlap
    """
    if salary is None:
        return True  # No salary info — don't filter out
    if min_annual is None and max_annual is None:
        return True  # No filter configured

    if min_annual is not None and salary.annual_max < min_annual:
        return False  # Job pays less than our minimum
    if max_annual is not None and salary.annual_min > max_annual:
        return False  # Job pays more than our maximum (unlikely filter, but supported)

    return True
