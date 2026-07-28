"""Radio selection must click the option matching the intended VALUE, and must
never leave a Yes/No group at its default because an option's label came back
empty. Root cause of a live bug: sponsorship recorded "No" but showed "Yes"
because "" is a substring of every value, so an empty-labeled "Yes" option
falsely matched "No". Skipped if no browser is available."""

import pytest

pytestmark = pytest.mark.asyncio

# Standard Yes/No sponsorship radio.
_YESNO = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="sp_y" name="sp"><label for="sp_y">Yes</label></div>
  <div><input type="radio" id="sp_n" name="sp"><label for="sp_n">No</label></div>
</fieldset></div>
"""

# The bug case: the "Yes" option's label text is EMPTY. Selecting "No" must NOT
# fall onto the empty-labeled "Yes" option.
_EMPTY_YES = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="e_y" name="sp"><label for="e_y"></label></div>
  <div><input type="radio" id="e_n" name="sp"><label for="e_n">No</label></div>
</fieldset></div>
"""

# Verbose options ("No, I will not require sponsorship").
_VERBOSE = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="v_y" name="sp"><label for="v_y">Yes, I will require sponsorship</label></div>
  <div><input type="radio" id="v_n" name="sp"><label for="v_n">No, I will not require sponsorship</label></div>
</fieldset></div>
"""


async def _run(html: str, value: str, first_id: str):
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
        field = FormField(label="Will you require sponsorship?", field_type=FieldType.RADIO,
                          locator=page.locator(f"input#{first_id}"), options=[])
        await flow._select_radio_option(field, value)
        checked = {}
        for i in await page.locator("input[type=radio]").all():
            checked[await i.get_attribute("id")] = await i.is_checked()
        return checked
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


async def test_no_selects_no_standard():
    checked = await _run(_YESNO, "No", "sp_y")
    assert checked["sp_n"] is True and checked["sp_y"] is False


async def test_yes_selects_yes_standard():
    checked = await _run(_YESNO, "Yes", "sp_y")
    assert checked["sp_y"] is True and checked["sp_n"] is False


async def test_no_not_flipped_by_empty_yes_label():
    # The regression: value "No" must NOT land on the empty-labeled "Yes".
    checked = await _run(_EMPTY_YES, "No", "e_y")
    assert checked["e_n"] is True, "should select No"
    assert checked["e_y"] is False, "must NOT select the empty-labeled Yes"


async def test_no_selects_verbose_no_option():
    checked = await _run(_VERBOSE, "No", "v_y")
    assert checked["v_n"] is True and checked["v_y"] is False
