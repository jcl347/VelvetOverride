"""Consent/authorization dropdowns are answered "Yes" but their options are
worded "I agree" / "I acknowledge", so an exact match misses and a required
field stalls. The consent matcher maps an affirmative answer onto the agreement
option, and never onto a refusal option."""

from velvetoverride.linkedin.apply import _match_clearance_option, _match_consent_option


def test_yes_maps_to_i_agree():
    label = ("I consent to and authorize Consensus to conduct background checks "
             "in connection with my candidacy for employment.")
    opts = ["I agree", "I do not agree"]
    assert _match_consent_option("Yes", opts, label) == "I agree"


def test_never_picks_refusal():
    label = "I authorize the company to verify my background."
    opts = ["I do not agree", "No", "Decline"]
    assert _match_consent_option("Yes", opts, label) is None


def test_prefers_shortest_affirmative():
    label = "Please acknowledge the terms and conditions."
    opts = ["I acknowledge and agree to all of the terms above", "I agree"]
    assert _match_consent_option("Yes", opts, label) == "I agree"


def test_not_a_consent_field_returns_none():
    # A non-consent dropdown must not be treated as consent.
    assert _match_consent_option("Yes", ["I agree", "No"], "Do you have a car?") is None


def test_non_affirmative_value_returns_none():
    label = "I consent to a background check."
    assert _match_consent_option("No", ["I agree", "I do not agree"], label) is None


def test_agree_option_with_embedded_not_still_selected():
    # "I agree" option that mentions "have not been convicted" must still count
    # as affirmative (the negation guard only rejects leading refusals).
    label = "I certify and consent to the following statements."
    opts = ["I agree and certify I have not misrepresented anything", "I decline"]
    assert _match_consent_option("Yes", opts, label) == \
        "I agree and certify I have not misrepresented anything"


# ── Security-clearance LEVEL dropdown ──

def test_clearance_no_maps_to_none():
    label = "What level of active security clearance do you have?"
    opts = ["None", "Confidential", "Secret", "Top Secret"]
    assert _match_clearance_option("No", opts, label) == "None"


def test_clearance_never_picks_a_real_level():
    # If there is no "None"-type option, do NOT invent a clearance level.
    label = "Security clearance level"
    opts = ["Confidential", "Secret", "Top Secret"]
    assert _match_clearance_option("No", opts, label) is None


def test_clearance_matcher_ignores_non_clearance_fields():
    assert _match_clearance_option("No", ["None", "Some"], "Do you have a car?") is None
