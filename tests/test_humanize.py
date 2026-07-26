"""Generated introductions (summaries, cover letters) must read like a human
wrote them and contain NO em/en dashes. _humanize_prose is the safety net that
runs on every generated prose string."""

from velvetoverride.agent.llm import _humanize_prose


def test_em_dash_becomes_comma():
    out = _humanize_prose("I build production systems — and I ship them.")
    assert "—" not in out
    assert out == "I build production systems, and I ship them."


def test_en_dash_stripped_too():
    out = _humanize_prose("Python and SQL – my daily tools.")
    assert "–" not in out and "—" not in out
    assert out == "Python and SQL, my daily tools."


def test_unspaced_em_dash():
    out = _humanize_prose("fast—reliable—tested")
    assert "—" not in out
    assert out == "fast, reliable, tested"


def test_no_double_comma_or_space_before_punct():
    # A dash right before existing punctuation must not yield ", ," or " ,".
    out = _humanize_prose("I lead teams —, deliver results .")
    assert ",  ," not in out and " ," not in out
    assert "—" not in out


def test_plain_text_unchanged():
    s = "A clean, human sentence with no dashes."
    assert _humanize_prose(s) == s


def test_hyphenated_words_preserved():
    # Real hyphens (not dashes) in compound words must survive.
    s = "A backend-focused, full-stack engineer."
    assert _humanize_prose(s) == s


def test_empty_and_none_safe():
    assert _humanize_prose("") == ""
    assert _humanize_prose(None) is None
