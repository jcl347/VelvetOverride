"""choose_next_action must never return a terminal/submit button when
submission isn't allowed (dry-run), without even calling the model."""

from velvetoverride.agent.llm import LLMClient
from velvetoverride.utils.config import Config


def _client():
    c = LLMClient.__new__(LLMClient)
    c._complete = lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM must not be called"))
    return c


def test_no_submit_returned_when_not_allowed_only_terminal():
    c = _client()
    # Only terminal buttons + not allowed -> "" without calling the model.
    assert c.choose_next_action(["Submit application"], "MLE", "Acme", allow_submit=False) == ""


def test_empty_labels_returns_empty():
    c = _client()
    assert c.choose_next_action([], "MLE", "Acme", allow_submit=True) == ""
