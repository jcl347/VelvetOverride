"""Skip roles requiring a security clearance the applicant does not hold — but
NOT roles where the clearance is sponsored ("ability to obtain")."""

from velvetoverride.main import role_requires_clearance


def test_title_active_secret_clearance_is_skipped():
    assert role_requires_clearance(
        "Staff Machine Learning Engineer (Active Secret Clearance)", "") is True


def test_title_ts_sci_is_skipped():
    assert role_requires_clearance("Data Engineer, TS/SCI", "") is True


def test_plain_title_not_skipped():
    assert role_requires_clearance("Senior Software Engineer", "") is False


def test_jd_must_have_active_clearance_skipped():
    jd = "Requirements: The candidate must have an active security clearance."
    assert role_requires_clearance("Software Engineer", jd) is True


def test_jd_ability_to_obtain_not_skipped():
    # Sponsored clearance — the applicant CAN apply.
    jd = "Must have the ability to obtain a security clearance after hire."
    assert role_requires_clearance("Software Engineer", jd) is False


def test_jd_active_but_or_ability_to_obtain_not_skipped():
    jd = ("Requires an active security clearance, or the ability to obtain "
          "one within 90 days of hire.")
    assert role_requires_clearance("Software Engineer", jd) is False


def test_jd_clearance_is_a_plus_not_skipped():
    jd = "An existing security clearance is a plus but not required."
    assert role_requires_clearance("Software Engineer", jd) is False


def test_empty_inputs_not_skipped():
    assert role_requires_clearance("", "") is False
    assert role_requires_clearance(None, None) is False
