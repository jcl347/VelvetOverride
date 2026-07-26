"""A certification/consent checkbox whose descriptive text lives in a sibling
element (not a <label for>) must still yield a real label, so the consent tier
can recognize and tick it. Otherwise a REQUIRED certification checkbox stays
"unknown_field", stays unticked, and stalls the whole form (seen live on the
Elder Research government-contractor form). Skipped if no browser is available.
"""

import pytest

pytestmark = pytest.mark.asyncio

# Certification text in a SIBLING span — the case that used to fail.
_CERT_SIBLING = (
    '<div role="dialog"><div class="q">'
    '<input type="checkbox" id="cb1">'
    '<span>I certify that the information provided is true and accurate.</span>'
    '</div></div>'
)
# Wrapping <label> — already worked; must keep working.
_CERT_WRAP = (
    '<div role="dialog"><label><input type="checkbox" id="cb2">'
    'I agree to the privacy policy</label></div>'
)
# A genuinely bare checkbox with no nearby text stays unknown.
_BARE = '<div role="dialog"><div><input type="checkbox" id="cb3"></div></div>'


async def _label(html: str, cb_id: str):
    try:
        from patchright.async_api import async_playwright
    except Exception:  # pragma: no cover
        pytest.skip("patchright not importable")
    from velvetoverride.linkedin.fields import _get_field_label

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
        return await _get_field_label(page.locator(f"#{cb_id}"), page)
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


async def test_certification_sibling_text_is_extracted():
    label = await _label(_CERT_SIBLING, "cb1")
    assert "certify" in label.lower()
    assert label != "unknown_field"


async def test_wrapping_label_still_works():
    label = await _label(_CERT_WRAP, "cb2")
    assert "privacy policy" in label.lower()


async def test_bare_checkbox_stays_unknown():
    label = await _label(_BARE, "cb3")
    assert label == "unknown_field"


async def test_extracted_cert_label_is_recognized_as_consent():
    # End-to-end: the extracted label must trip the consent detector so the box
    # gets ticked (not left unchecked as "unknown_field" would be).
    from velvetoverride.agent.field_solver import FieldSolver
    label = await _label(_CERT_SIBLING, "cb1")
    assert FieldSolver._is_consent_checkbox(label) is True


# ── Shadow-DOM consent checkbox (the Elder Research case) ──
# The certification checkbox renders inside a shadow root with the "I Agree" /
# "Please confirm..." text in the LIGHT DOM around the shadow host. Plain
# parentElement climbing never reaches it (→ "unknown_field" → unticked → stuck).
# Label extraction must cross the shadow boundary via getRootNode().host.
_SHADOW_JS = """() => {
  const dlg = document.querySelector('div[role=dialog]');
  const q = document.createElement('div');
  q.innerHTML = '<p>Please confirm you read and understand the above.</p>';
  const host = document.createElement('div');
  q.appendChild(host);
  dlg.appendChild(q);
  const sr = host.attachShadow({mode: 'open'});
  const cb = document.createElement('input');
  cb.type = 'checkbox'; cb.id = 'shcb';
  sr.appendChild(cb);
}"""


async def _label_shadow():
    try:
        from patchright.async_api import async_playwright
    except Exception:  # pragma: no cover
        pytest.skip("patchright not importable")
    from velvetoverride.linkedin.fields import _get_field_label
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
        await page.set_content('<div role="dialog"></div>')
        await page.evaluate(_SHADOW_JS)
        return await _get_field_label(page.locator("#shcb"), page)
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


async def test_shadow_dom_checkbox_label_crosses_boundary():
    label = await _label_shadow()
    assert label != "unknown_field"
    assert "confirm" in label.lower() or "understand" in label.lower()
