"""EEO radio selection must click the DECLINE option ("I prefer not to specify")
for a "Prefer not to say" answer — never a real demographic (the first option),
and never guess when there is no decline option. Skipped if no browser."""

import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.asyncio

_RACE = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="r0" name="race"><label for="r0">Hispanic or Latino</label></div>
  <div><input type="radio" id="r1" name="race"><label for="r1">Asian (Not Hispanic or Latino)</label></div>
  <div><input type="radio" id="r2" name="race"><label for="r2">White (Not Hispanic or Latino)</label></div>
  <div><input type="radio" id="r7" name="race"><label for="r7">I prefer not to specify</label></div>
</fieldset></div>
"""

# A group with NO decline option — the bot must select NOTHING.
_NO_DECLINE = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="a0" name="g"><label for="a0">Male</label></div>
  <div><input type="radio" id="a1" name="g"><label for="a1">Female</label></div>
</fieldset></div>
"""


async def _run(html: str, value: str, input_id: str):
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
        field = FormField(label="Race/Ethnicity", field_type=FieldType.RADIO,
                          locator=page.locator(f"input#{input_id}"), options=[])
        await flow._select_radio_option(field, value)
        checked = {}
        for i in await page.locator("input[type=radio]").all():
            checked[await i.get_attribute("id")] = await i.is_checked()
        return checked
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


async def test_decline_selects_prefer_not_to_specify():
    checked = await _run(_RACE, "Prefer not to say", "r0")
    assert checked["r7"] is True                 # the decline option
    assert not any(checked[k] for k in ("r0", "r1", "r2"))  # no real demographic


async def test_no_decline_option_selects_nothing():
    checked = await _run(_NO_DECLINE, "Prefer not to say", "a0")
    assert not any(checked.values())             # nothing selected, no guess


# ── VEVRAA veteran status (3 options) — must NEVER claim protected-veteran ──
_VETERAN = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="v0" name="vet"><label for="v0">I identify as one or more of the classifications of protected veteran listed above</label></div>
  <div><input type="radio" id="v1" name="vet"><label for="v1">I am not a protected veteran</label></div>
  <div><input type="radio" id="v2" name="vet"><label for="v2">I prefer not to specify</label></div>
</fieldset></div>
"""

# CC-305 disability (3 options) — must NEVER claim a disability.
_DISABILITY = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="d0" name="dis"><label for="d0">Yes, I have a disability, or have had one in the past</label></div>
  <div><input type="radio" id="d1" name="dis"><label for="d1">No, I do not have a disability and have not had one in the past</label></div>
  <div><input type="radio" id="d2" name="dis"><label for="d2">I do not want to answer</label></div>
</fieldset></div>
"""


async def test_veteran_status_declines_never_claims_protected():
    checked = await _run(_VETERAN, "I prefer not to specify", "v0")
    assert checked["v2"] is True                      # the decline option
    assert checked["v0"] is False                     # NEVER "protected veteran"
    assert checked["v1"] is False


async def test_disability_declines_never_claims_disability():
    # Solver returns the generic "Prefer not to say"; selection must map it to
    # the real decline option and never "Yes, I have a disability".
    checked = await _run(_DISABILITY, "Prefer not to say", "d0")
    assert checked["d2"] is True                      # "I do not want to answer"
    assert checked["d0"] is False                     # NEVER "Yes, I have a disability"


# EEO options whose visible text is in a SIBLING span (empty label[for]) — the
# real-form case (Ranger, etc.) where the decline option read as empty and the
# whole EEO group was wrongly left blank ("no decline option").
_RACE_SIBLING_SPAN = """
<div role="dialog"><fieldset>
  <div><input type="radio" id="s0" name="race"><span>Hispanic or Latino</span></div>
  <div><input type="radio" id="s1" name="race"><span>White (Not Hispanic or Latino)</span></div>
  <div><input type="radio" id="s2" name="race"><span>Two or More Races</span></div>
  <div><input type="radio" id="s3" name="race"><span>I prefer not to specify</span></div>
</fieldset></div>
"""


async def test_decline_selected_when_option_text_in_sibling_span():
    checked = await _run(_RACE_SIBLING_SPAN, "Prefer not to say", "s0")
    assert checked["s3"] is True                              # the decline option
    assert not any(checked[k] for k in ("s0", "s1", "s2"))   # no real demographic
