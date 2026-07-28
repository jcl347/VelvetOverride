"""Full-form read-back verification: after filling, the bot re-reads each field's
ACTUAL DOM value and flags a mismatch (a selection that silently didn't take).
This is the safety net that would have caught the sponsorship "recorded No but
shows Yes" bug on its own. Skipped if no browser is available."""

import pytest

pytestmark = pytest.mark.asyncio


async def _flow_page(html: str):
    try:
        from patchright.async_api import async_playwright
    except Exception:  # pragma: no cover
        pytest.skip("patchright not importable")
    from velvetoverride.linkedin.apply import ApplicationFlow
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
        yield flow, page
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


def _field(page, selector, ftype, label="Q"):
    from velvetoverride.linkedin.fields import FormField
    from velvetoverride.tracking.models import FieldType
    return FormField(label=label, field_type=getattr(FieldType, ftype),
                     locator=page.locator(selector), options=[])


async def test_radio_correct_selection_verifies():
    html = ('<div role="dialog"><fieldset>'
            '<div><input type="radio" id="y" name="s"><label for="y">Yes</label></div>'
            '<div><input type="radio" id="n" name="s"><label for="n">No</label></div>'
            '</fieldset></div>')
    async for flow, page in _flow_page(html):
        await page.locator("#n").check(force=True)
        status, actual = await flow._readback_field(_field(page, "#y", "RADIO"), "No")
        assert status is True and "no" in actual.lower()


async def test_radio_wrong_selection_flags_mismatch():
    html = ('<div role="dialog"><fieldset>'
            '<div><input type="radio" id="y" name="s"><label for="y">Yes</label></div>'
            '<div><input type="radio" id="n" name="s"><label for="n">No</label></div>'
            '</fieldset></div>')
    async for flow, page in _flow_page(html):
        await page.locator("#y").check(force=True)          # Yes selected...
        status, actual = await flow._readback_field(_field(page, "#y", "RADIO"), "No")  # ...but we intended No
        assert status is False                              # mismatch flagged


async def test_radio_none_selected_flags_mismatch():
    html = ('<div role="dialog"><fieldset>'
            '<div><input type="radio" id="y" name="s"><label for="y">Yes</label></div>'
            '<div><input type="radio" id="n" name="s"><label for="n">No</label></div>'
            '</fieldset></div>')
    async for flow, page in _flow_page(html):
        status, actual = await flow._readback_field(_field(page, "#y", "RADIO"), "No")
        assert status is False and "none" in actual.lower()


async def test_text_match_and_mismatch():
    html = '<div role="dialog"><input type="text" id="t" value="125000"></div>'
    async for flow, page in _flow_page(html):
        ok, _ = await flow._readback_field(_field(page, "#t", "TEXT"), "125000")
        assert ok is True
        bad, _ = await flow._readback_field(_field(page, "#t", "TEXT"), "999")
        assert bad is False


async def test_checkbox_state_verified():
    html = '<div role="dialog"><input type="checkbox" id="c"></div>'
    async for flow, page in _flow_page(html):
        # unchecked but intended Yes -> mismatch
        bad, _ = await flow._readback_field(_field(page, "#c", "CHECKBOX"), "Yes")
        assert bad is False
        await page.locator("#c").check(force=True)
        ok, _ = await flow._readback_field(_field(page, "#c", "CHECKBOX"), "Yes")
        assert ok is True


async def test_select_dropdown_verified():
    html = ('<div role="dialog"><select id="d">'
            '<option value="">Select</option><option value="us">United States</option>'
            '<option value="ca">Canada</option></select></div>')
    async for flow, page in _flow_page(html):
        await page.locator("#d").select_option("us")
        ok, actual = await flow._readback_field(_field(page, "#d", "DROPDOWN"), "United States")
        assert ok is True and "united states" in actual.lower()


async def test_eeo_decline_worded_differently_is_verified():
    # Intended generic "Prefer not to say" vs the form's "I prefer not to specify"
    # is the same decline choice — read-back must NOT flag it as a mismatch.
    html = ('<div role="dialog"><fieldset>'
            '<div><input type="radio" id="a" name="race"><label for="a">White</label></div>'
            '<div><input type="radio" id="z" name="race"><label for="z">I prefer not to specify</label></div>'
            '</fieldset></div>')
    async for flow, page in _flow_page(html):
        await page.locator("#z").check(force=True)
        ok, actual = await flow._readback_field(
            _field(page, "#a", "RADIO", label="Race/Ethnicity"), "Prefer not to say")
        assert ok is True
        assert "prefer not" in actual.lower()


async def test_eeo_disability_decline_variant_verified():
    # "I do not want to answer" also counts as a decline vs intended "Prefer not to say".
    html = ('<div role="dialog"><fieldset>'
            '<div><input type="radio" id="d0" name="dis"><label for="d0">Yes, I have a disability</label></div>'
            '<div><input type="radio" id="d2" name="dis"><label for="d2">I do not want to answer</label></div>'
            '</fieldset></div>')
    async for flow, page in _flow_page(html):
        await page.locator("#d2").check(force=True)
        ok, _ = await flow._readback_field(
            _field(page, "#d0", "RADIO", label="Disability"), "Prefer not to say")
        assert ok is True


async def test_compensation_dropdown_readback_matches_band():
    # The solver's answer is the raw number "125000"; the salary matcher selects
    # the "$100,000 - $150,000" band. Read-back sees the band text (not the number)
    # and must still verify by band-containment — not false-flag a correct pick.
    html = ('<div role="dialog"><select id="c">'
            '<option value="">Select</option>'
            '<option>$100,000 - $150,000</option>'
            '<option>$200,000+</option></select></div>')
    async for flow, page in _flow_page(html):
        await page.locator("#c").select_option(label="$100,000 - $150,000")
        ok, actual = await flow._readback_field(
            _field(page, "#c", "DROPDOWN", label="Compensation"), "125000")
        assert ok is True and "100,000" in actual
        # A number OUTSIDE the selected band is still a genuine mismatch.
        bad, _ = await flow._readback_field(
            _field(page, "#c", "DROPDOWN", label="Compensation"), "300000")
        assert bad is False
