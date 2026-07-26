"""The stuck detector must tell genuinely-different steps apart even when both
have no fillable fields. _step_marker (progress % + heading) provides that: two
different empty steps get different markers (counter resets = form advances),
while a truly-repeated step keeps the same marker (still caught). Skipped if no
browser is available."""

import pytest

pytestmark = pytest.mark.asyncio

_WORK = ('<div role="dialog"><div role="progressbar" aria-valuenow="60"></div>'
         '<h3>Work experience</h3><button>Add work experience</button>'
         '<button>Next</button></div>')
_EDU = ('<div role="dialog"><div role="progressbar" aria-valuenow="80"></div>'
        '<h3>Education</h3><button>Next</button></div>')
_PAGES3 = ('<div role="dialog"><span>3/6 pages</span><h3>Resume</h3>'
           '<button>Next</button></div>')
_PAGES4 = ('<div role="dialog"><span>4/6 pages</span><h3>Additional Questions</h3>'
           '<button>Next</button></div>')


async def _marker(html: str):
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
        return await flow._step_marker(page.locator("div[role=dialog]"))
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


async def test_different_empty_steps_get_different_markers():
    m_work, m_edu = await _marker(_WORK), await _marker(_EDU)
    assert m_work and m_edu and m_work != m_edu


async def test_same_step_gets_same_marker():
    assert await _marker(_WORK) == await _marker(_WORK)


async def test_page_counter_distinguishes_steps():
    m3, m4 = await _marker(_PAGES3), await _marker(_PAGES4)
    assert m3 and m4 and m3 != m4
