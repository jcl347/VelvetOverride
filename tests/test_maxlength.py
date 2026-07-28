"""A short-maxlength text input must not receive a verbose LLM answer that
LinkedIn rejects as "Invalid input", stalling a required field (the Visionary
"Marketing products exp ?*" case: maxlength=20 got a 189-char essay). The bot
condenses an affirmative/negative answer to Yes/No, pulls out an embedded
number, or truncates at a word boundary. Browser-backed; skipped if no browser."""

import pytest

pytestmark = pytest.mark.asyncio


async def _fit(html: str, selector: str, value: str, label: str = "Q"):
    try:
        from patchright.async_api import async_playwright
    except Exception:  # pragma: no cover
        pytest.skip("patchright not importable")
    from velvetoverride.linkedin.apply import ApplicationFlow
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType

    try:
        pw = await async_playwright().start()
    except Exception:  # pragma: no cover
        pytest.skip("playwright runtime unavailable")
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True, channel="chrome")
        except Exception:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception:  # pragma: no cover
                pytest.skip("no chromium/chrome available")
        page = await browser.new_page()
        await page.set_content(html)
        flow = ApplicationFlow.__new__(ApplicationFlow)
        flow._page = page
        field = FormField(label=label, field_type=FieldType.TEXT,
                          locator=page.locator(selector), options=[])
        fitted = await flow._fit_to_maxlength(field, value)
        # Also fill for real and confirm the browser accepted the full string
        # (i.e. it was not silently clipped by the maxlength attribute).
        await field.locator.fill(fitted)
        actual = await field.locator.input_value()
        return fitted, actual
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


_SHORT = '<div role="dialog"><input type="text" id="t" maxlength="20"></div>'
_NOMAX = '<div role="dialog"><input type="text" id="t"></div>'


async def test_verbose_yes_condensed_to_yes():
    fitted, actual = await _fit(
        _SHORT, "#t",
        "Yes, I have experience building and integrating marketing product features "
        "via REST APIs and full-stack development.")
    assert fitted == "Yes"
    assert actual == "Yes"          # fully accepted, no "Invalid input"


async def test_verbose_no_condensed_to_no():
    fitted, _ = await _fit(_SHORT, "#t", "No, I have not worked with that technology.")
    assert fitted == "No"


async def test_embedded_number_extracted():
    fitted, _ = await _fit(_SHORT, "#t", "About 3 years of hands-on experience", label="Years?")
    assert fitted == "3"


async def test_short_answer_passes_through_unchanged():
    fitted, actual = await _fit(_SHORT, "#t", "Yes")
    assert fitted == "Yes" and actual == "Yes"


async def test_no_maxlength_keeps_full_answer():
    long = "Yes, I have deep experience across many marketing platforms and APIs."
    fitted, actual = await _fit(_NOMAX, "#t", long)
    assert fitted == long and actual == long


async def test_non_yesno_verbose_truncated_at_word_boundary():
    fitted, actual = await _fit(
        _SHORT, "#t", "Experienced full-stack developer with cloud expertise")
    assert len(fitted) <= 20
    assert fitted == fitted.rstrip()        # no dangling trailing space
    assert not fitted.endswith(" ")
    assert actual == fitted                 # accepted whole
