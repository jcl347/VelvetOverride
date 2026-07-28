"""Year-range dropdown matching: a "How many years...?" dropdown offers ranges
("0-1 years", "3-5 years", "7+"), and a numeric answer must map to the range that
contains it (a live Tebra form stalled because "Yes"/a bare number matched no
option). Pure function — no browser needed."""

from velvetoverride.linkedin.apply import _match_year_range_option as mm
from velvetoverride.linkedin.apply import _match_salary_range_option as sm
from velvetoverride.linkedin.apply import _parse_money


def test_number_maps_to_containing_range():
    opts = ["Select an option", "0-1 years", "2-3 years", "3-5 years",
            "6-10 years", "10+ years"]
    assert mm("4", opts) == "3-5 years"
    assert mm("2", opts) == "2-3 years"
    assert mm("8", opts) == "6-10 years"
    assert mm("0", opts) == "0-1 years"


def test_open_ended_plus_bucket():
    opts = ["Less than 1 year", "1-2 years", "3+ years"]
    assert mm("5", opts) == "3+ years"
    assert mm("3", opts) == "3+ years"


def test_less_than_bucket():
    opts = ["Less than 2 years", "2-5 years", "5+ years"]
    assert mm("1", opts) == "Less than 2 years"


def test_non_numeric_answer_returns_none():
    opts = ["0-1 years", "2-3 years", "3+ years"]
    assert mm("Yes", opts) is None
    assert mm("", opts) is None


def test_no_matching_range_returns_none():
    # 20 fits no bucket and there's no open-ended option.
    opts = ["0-1 years", "2-3 years", "4-6 years"]
    assert mm("20", opts) is None


def test_prefers_bounded_range_over_open_ended_plus():
    # 5 should land in the bounded "3-5" bucket, not the open "3+".
    opts = ["3-5 years", "3+ years"]
    assert mm("5", opts) == "3-5 years"


# ── Salary/compensation dropdowns must NOT be touched by the year matcher, and
#    the dedicated salary matcher picks the $ band containing the amount. This is
#    the Storable regression: a $125,000 answer wrongly picked "$200,000+". ──
_COMP = ["$50,000 - $100,000", "$100,000 - $150,000",
         "$150,000 - $200,000", "$200,000+"]


def test_year_matcher_refuses_dollar_options():
    # A $ amount is never a year count — the year matcher must bail out.
    assert mm("125000", _COMP) is None
    assert mm("125000", ["$100k - $150k", "$150k+"]) is None


def test_salary_matcher_picks_containing_band():
    assert sm("125000", _COMP) == "$100,000 - $150,000"
    assert sm("175000", _COMP) == "$150,000 - $200,000"


def test_salary_matcher_open_ended_plus():
    assert sm("210000", _COMP) == "$200,000+"


def test_salary_matcher_k_suffix_bands():
    opts = ["$100K-$150K", "$150K-$200K", "$200K+"]
    assert sm("125000", opts) == "$100K-$150K"


def test_salary_matcher_below_all_returns_none():
    # 40000 fits no band and there's no "less than" option.
    assert sm("40000", _COMP) is None


def test_salary_matcher_ignores_years_number():
    # A small number (years) must never be treated as a salary.
    assert sm("5", _COMP) is None


def test_parse_money_handles_formats():
    assert _parse_money("$100,000") == [100000]
    assert _parse_money("$150K") == [150000]
    assert _parse_money("$1.2M") == [1200000]
    assert _parse_money("$100,000 - $150,000") == [100000, 150000]
