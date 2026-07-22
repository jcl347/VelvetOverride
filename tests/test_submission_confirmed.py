"""_submission_confirmed must require a POSITIVE confirmation phrase, and must
NOT fire on the bare word 'applied' (all over a jobs page) or on the mere
absence of the form — both fabricated 'applied' records.
"""

import pytest

from velvetoverride.linkedin.apply import ApplicationFlow


class _FakeLocator:
    def __init__(self, text="", count=1):
        self._text, self._count = text, count
        self.first = self

    async def count(self):
        return self._count

    def nth(self, i):  # every dialog in this fake has the same text
        return self

    async def inner_text(self, timeout=None):
        return self._text


class _FakePage:
    """dialog_text=None => no dialog present (falls back to body)."""
    def __init__(self, dialog_text=None, body_text=""):
        self._dialog_text, self._body_text = dialog_text, body_text

    def locator(self, selector):
        if "dialog" in selector or "artdeco-modal" in selector:
            if self._dialog_text is None:
                return _FakeLocator(count=0)
            return _FakeLocator(text=self._dialog_text, count=1)
        return _FakeLocator(text=self._body_text, count=1)


def _flow(page):
    obj = ApplicationFlow.__new__(ApplicationFlow)
    obj._page = page
    return obj


@pytest.mark.asyncio
async def test_confirmed_on_real_confirmation_dialog():
    page = _FakePage(dialog_text="Your application was sent to Acme Corp")
    assert await _flow(page)._submission_confirmed() is True


@pytest.mark.asyncio
async def test_not_confirmed_on_bare_applied_word():
    # The regression: a jobs page full of "applied"/"applicants" is NOT proof.
    body = "28 applicants. Applied 2 hours ago. Be an early applicant. Easy Apply"
    page = _FakePage(dialog_text=None, body_text=body)
    assert await _flow(page)._submission_confirmed() is False


@pytest.mark.asyncio
async def test_not_confirmed_when_form_merely_absent():
    # Empty page (modal failed to open / navigated) must NOT count as submitted.
    page = _FakePage(dialog_text=None, body_text="")
    assert await _flow(page)._submission_confirmed() is False


@pytest.mark.asyncio
async def test_confirmed_variant_phrasings():
    for phrase in ("Your application has been sent",
                   "Application submitted successfully",
                   "your application has been submitted"):
        page = _FakePage(dialog_text=phrase)
        assert await _flow(page)._submission_confirmed() is True, phrase


@pytest.mark.asyncio
async def test_open_easy_apply_form_is_not_confirmation():
    # An OPEN Easy Apply modal (still filling) must not read as confirmed.
    page = _FakePage(dialog_text="Contact info. Email address. Resume. Next")
    assert await _flow(page)._submission_confirmed() is False
