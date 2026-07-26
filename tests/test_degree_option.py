"""Degree-dropdown matching: a degree answer like "Master's Degree" must map to
an option worded differently ("Master of Science"), without misfiring on
"undergraduate" or picking a non-degree option."""

from velvetoverride.linkedin.apply import _match_degree_option


def test_masters_maps_to_master_of_science():
    opts = ["Bachelor of Science", "Master of Science", "Doctor of Philosophy"]
    assert _match_degree_option("Master's Degree", opts) == "Master of Science"


def test_masters_plain_option():
    opts = ["High School Diploma", "Bachelor's", "Master's", "PhD"]
    assert _match_degree_option("Master's Degree", opts) == "Master's"


def test_bachelor_maps_to_bachelor_option():
    opts = ["Master of Science", "Bachelor of Arts", "Associate Degree"]
    assert _match_degree_option("Bachelor's Degree", opts) == "Bachelor of Arts"


def test_masters_does_not_match_undergraduate():
    # "undergraduate" must NOT be picked for a master's answer.
    opts = ["Undergraduate degree", "Postgraduate degree"]
    assert _match_degree_option("Master's Degree", opts) == "Postgraduate degree"


def test_doctorate_before_master_collision():
    opts = ["Master's Degree", "PhD / Doctorate"]
    assert _match_degree_option("Doctorate", opts) == "PhD / Doctorate"


def test_prefers_shortest_plain_level():
    opts = ["Master's or higher with 5+ years experience", "Master's"]
    assert _match_degree_option("Master's Degree", opts) == "Master's"


def test_no_matching_level_returns_none():
    opts = ["Bachelor of Science", "Doctor of Philosophy"]
    assert _match_degree_option("Master's Degree", opts) is None


def test_non_degree_value_returns_none():
    # A non-degree dropdown value must never be treated as a degree.
    assert _match_degree_option("Yes", ["Yes", "No"]) is None
    assert _match_degree_option("United States", ["United States", "Canada"]) is None
