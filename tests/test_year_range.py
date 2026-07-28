"""Year-range dropdown matching: a "How many years...?" dropdown offers ranges
("0-1 years", "3-5 years", "7+"), and a numeric answer must map to the range that
contains it (a live Tebra form stalled because "Yes"/a bare number matched no
option). Pure function — no browser needed."""

from velvetoverride.linkedin.apply import _match_year_range_option as mm


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
