"""Radio-group QUESTION extraction against LinkedIn-like DOM.

LinkedIn renders the question as a heading/label OUTSIDE the radio group (with an
empty <legend>); the group itself holds only the option texts. The extractor must
recover the question so the solver can match sponsorship / work-auth / EEO rules
instead of falling back to the obfuscated ``radio-group-:ri:`` name and letting
the LLM blindly answer "Yes". These tests mirror the real DOM captured from live
runs. Skipped automatically if no browser is available.
"""

import pytest

pytestmark = pytest.mark.asyncio

# LinkedIn puts the question in a sibling span before an empty-legend fieldset;
# option texts live in spans inside the group (NOT in <label> elements).
_SPONSOR_HTML = """
<div class="fe">
  <span class="lbl">Will you now or in the future require sponsorship for employment visa status?</span>
  <fieldset>
    <legend></legend>
    <div role="radiogroup">
      <div role="radio"><input type="radio" id="s0" name="radio-group-:r1:"><span>Yes</span></div>
      <div role="radio"><input type="radio" id="s1" name="radio-group-:r1:"><span>No</span></div>
    </div>
  </fieldset>
</div>
"""

_RACE_HTML = """
<div class="fe">
  <span>Race/Ethnicity</span>
  <span>Race categories are defined as follows: ...</span>
  <fieldset>
    <legend></legend>
    <div role="radiogroup">
      <div role="radio"><input type="radio" id="r0" name="radio-group-:r2:"><span>Hispanic or Latino</span></div>
      <div role="radio"><input type="radio" id="r1" name="radio-group-:r2:"><span>Asian (Not Hispanic or Latino)</span></div>
      <div role="radio"><input type="radio" id="r2" name="radio-group-:r2:"><span>I prefer not to specify</span></div>
    </div>
  </fieldset>
</div>
"""


async def _label_for(html: str, input_id: str):
    """Render html in a real browser and resolve the radio group's question."""
    try:
        from patchright.async_api import async_playwright
    except Exception:  # pragma: no cover
        pytest.skip("patchright not importable")
    from velvetoverride.linkedin.fields import _get_radio_group_label

    try:
        pw = await async_playwright().start()
    except Exception:  # pragma: no cover
        pytest.skip("playwright runtime unavailable")
    browser = None
    try:
        try:
            # Prefer real Chrome (what the bot uses); fall back to bundled chromium.
            try:
                browser = await pw.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = await pw.chromium.launch(headless=True)
        except Exception:  # pragma: no cover - no browser installed
            pytest.skip("no chromium/chrome available for integration test")
        page = await browser.new_page()
        await page.set_content(html)
        label = await _get_radio_group_label(page.locator(f"input#{input_id}"), page)
        return (label or "").lower()
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


async def test_sponsorship_question_recovered():
    label = await _label_for(_SPONSOR_HTML, "s0")
    assert "sponsorship" in label
    assert label.strip() not in ("yes no", "")  # not just the options / the name


async def test_eeo_race_question_recovered():
    label = await _label_for(_RACE_HTML, "r0")
    assert "race" in label
    # The option texts must not be all that comes back.
    assert "hispanic or latino asian" not in label.replace("  ", " ")
