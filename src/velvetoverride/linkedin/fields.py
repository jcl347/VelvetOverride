"""Form field detection and classification for LinkedIn Easy Apply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from velvetoverride.tracking.models import FieldType
from velvetoverride.utils.logging import get_logger

if TYPE_CHECKING:
    from patchright.async_api import Locator, Page

log = get_logger(__name__)


@dataclass
class FormField:
    """A detected form field with its metadata."""

    label: str
    field_type: FieldType
    locator: Locator
    required: bool = False
    options: list[str] | None = None  # For radio/dropdown/checkbox
    current_value: str = ""


async def detect_form_fields(page: Page, scope=None) -> list[FormField]:
    """Scan a form and detect all input fields.

    ``scope`` optionally restricts scanning to a container (e.g. the Easy Apply
    modal) so we don't pick up unrelated page chrome like "Set alert" toggles or
    job-alert switches. Label resolution still uses the full ``page``.
    """
    fields: list[FormField] = []
    root = scope if scope is not None else page

    # --- Text inputs ---
    text_inputs = root.locator(
        'input[type="text"], '
        'input:not([type]), '
        'input[type="email"], '
        'input[type="tel"], '
        'input[type="url"]'
    )
    for i in range(await text_inputs.count()):
        inp = text_inputs.nth(i)
        if not await inp.is_visible():
            continue
        label = await _get_field_label(inp, page)
        current = await inp.input_value() or ""
        fields.append(FormField(
            label=label,
            field_type=FieldType.TEXT,
            locator=inp,
            required=await _is_required(inp),
            current_value=current,
        ))

    # --- Numeric inputs ---
    num_inputs = root.locator('input[type="number"]')
    for i in range(await num_inputs.count()):
        inp = num_inputs.nth(i)
        if not await inp.is_visible():
            continue
        label = await _get_field_label(inp, page)
        current = await inp.input_value() or ""
        fields.append(FormField(
            label=label,
            field_type=FieldType.NUMERIC,
            locator=inp,
            required=await _is_required(inp),
            current_value=current,
        ))

    # --- Textareas ---
    textareas = root.locator("textarea")
    for i in range(await textareas.count()):
        ta = textareas.nth(i)
        if not await ta.is_visible():
            continue
        label = await _get_field_label(ta, page)
        current = await ta.input_value() or ""
        fields.append(FormField(
            label=label,
            field_type=FieldType.TEXTAREA,
            locator=ta,
            required=await _is_required(ta),
            current_value=current,
        ))

    # --- Selects (dropdowns) ---
    selects = root.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        if not await sel.is_visible():
            continue
        label = await _get_field_label(sel, page)
        options = await _get_select_options(sel)
        fields.append(FormField(
            label=label,
            field_type=FieldType.DROPDOWN,
            locator=sel,
            required=await _is_required(sel),
            options=options,
        ))

    # --- Radio button groups ---
    radio_groups = await _detect_radio_groups(root, page)
    fields.extend(radio_groups)

    # --- Checkboxes ---
    checkboxes = root.locator('input[type="checkbox"]')
    for i in range(await checkboxes.count()):
        cb = checkboxes.nth(i)
        if not await _input_interactable(cb):
            continue
        label = await _get_field_label(cb, page)
        fields.append(FormField(
            label=label,
            field_type=FieldType.CHECKBOX,
            locator=cb,
            required=await _is_required(cb),
        ))

    # --- File upload ---
    file_inputs = root.locator('input[type="file"]')
    for i in range(await file_inputs.count()):
        fi = file_inputs.nth(i)
        label = await _get_field_label(fi, page)
        fields.append(FormField(
            label=label,
            field_type=FieldType.FILE_UPLOAD,
            locator=fi,
        ))

    log.info("fields.detected", count=len(fields), types=[f.field_type.value for f in fields])
    return fields


async def _get_field_label(element: Locator, page: Page) -> str:
    """Try multiple strategies to find the label for a form element.

    Ordered from most to least reliable, and resilient to LinkedIn's obfuscated
    CSS classes: aria-label, aria-labelledby, label[for], a wrapping <label>,
    placeholder, a nearby label/legend, then name.
    """
    # Strategy 1: aria-label attribute
    aria_label = await element.get_attribute("aria-label")
    if aria_label and aria_label.strip():
        return aria_label.strip()

    # Strategy 2: aria-labelledby -> text of referenced element(s)
    labelledby = await element.get_attribute("aria-labelledby")
    if labelledby:
        parts = []
        for ref in labelledby.split():
            try:
                ref_el = page.locator(f'#{ref}')
                if await ref_el.count() > 0:
                    t = (await ref_el.first.text_content()) or ""
                    if t.strip():
                        parts.append(t.strip())
            except Exception:
                pass
        if parts:
            return " ".join(parts)[:200]

    # Strategy 3: associated <label> via 'for' attribute
    el_id = await element.get_attribute("id")
    # Skip if the id would break the selector string (a stray quote raises,
    # and this block is not try-wrapped — it would fail the whole application).
    if el_id and '"' not in el_id and "\\" not in el_id:
        label_el = page.locator(f'label[for="{el_id}"]')
        if await label_el.count() > 0:
            text = await label_el.first.text_content()
            if text and text.strip():
                return text.strip()

    # Strategy 4: a wrapping <label> (common for checkboxes/radios:
    # <label><input ...>Text</label>) — obfuscation-proof.
    try:
        wrap = element.locator("xpath=ancestor::label[1]")
        if await wrap.count() > 0:
            text = await wrap.first.text_content()
            if text and text.strip():
                return text.strip()[:200]
    except Exception:
        pass

    # Strategy 5: placeholder
    placeholder = await element.get_attribute("placeholder")
    if placeholder and placeholder.strip():
        return placeholder.strip()

    # Strategy 6: nearest label / legend in an ancestor group (class-agnostic)
    try:
        anc = element.locator(
            "xpath=ancestor::*[self::fieldset or @role='group' or "
            "contains(@class,'form-element') or contains(@class,'fb-form')][1]"
        )
        if await anc.count() > 0:
            lbl = anc.first.locator("label, legend, .fb-form-element-label, "
                                    ".artdeco-text-input--label")
            if await lbl.count() > 0:
                text = await lbl.first.text_content()
                if text and text.strip():
                    return text.strip()[:200]
    except Exception:
        pass

    # Strategy 7: name attribute as last resort
    name = await element.get_attribute("name")
    if name and name.strip():
        return name.strip()

    return "unknown_field"


async def _is_required(element: Locator) -> bool:
    """Check if a field is required."""
    required = await element.get_attribute("required")
    if required is not None:
        return True
    aria_required = await element.get_attribute("aria-required")
    return aria_required == "true"


async def _get_select_options(select: Locator) -> list[str]:
    """Extract option text values from a <select> element."""
    options = select.locator("option")
    texts = []
    for i in range(await options.count()):
        text = (await options.nth(i).text_content() or "").strip()
        value = await options.nth(i).get_attribute("value")
        # Skip placeholder options
        if text and value != "":
            texts.append(text)
    return texts


async def _get_radio_group_label(radio, page: Page) -> str:
    """Resolve the QUESTION for a radio group (not one option's text).

    Prefers the group container's label (fieldset legend, role=group aria-label,
    or the LinkedIn form-element label span); only falls back to the per-radio
    label as a last resort so the solver matches the actual question.
    """
    # 1) fieldset > legend
    try:
        fieldset = radio.locator("xpath=ancestor::fieldset[1]")
        if await fieldset.count() > 0:
            legend = fieldset.locator("legend")
            if await legend.count() > 0:
                t = (await legend.first.text_content() or "").strip()
                if t:
                    return " ".join(t.split())
    except Exception:
        pass
    # 2) role=group aria-label
    try:
        grp = radio.locator("xpath=ancestor::*[@role='group'][1]")
        if await grp.count() > 0:
            al = (await grp.first.get_attribute("aria-label") or "").strip()
            if al:
                return " ".join(al.split())
    except Exception:
        pass
    # 3) LinkedIn form-element wrapper label
    try:
        wrapper = radio.locator(
            "xpath=ancestor::*[contains(@class,'fb-dash-form-element') "
            "or contains(@class,'jobs-easy-apply-form-element')][1]"
        )
        if await wrapper.count() > 0:
            lbl = wrapper.locator("label, span[data-test-form-builder-radio-button-form-component__title], legend")
            if await lbl.count() > 0:
                t = (await lbl.first.text_content() or "").strip()
                if t:
                    return " ".join(t.split())
    except Exception:
        pass
    # 4) last resort: the individual radio's label
    return await _get_field_label(radio, page)


async def _input_interactable(el: Locator) -> bool:
    """True if a checkbox/radio input should be filled.

    LinkedIn (and many ATS) visually hide the native <input> and render a
    styled label as the clickable proxy, so Playwright's is_visible() on the
    input returns False even though it is functional. Accept such inputs when
    the input OR its label occupies space and the input is enabled and not
    display:none/visibility:hidden — while still excluding disabled and truly
    hidden template inputs.
    """
    try:
        if await el.is_disabled():
            return False
    except Exception:
        pass
    try:
        if await el.is_visible():
            return True
    except Exception:
        pass
    try:
        return await el.evaluate(
            "e => {"
            " const s = getComputedStyle(e);"
            " if (s.display === 'none' || s.visibility === 'hidden') return false;"
            " const r = e.getBoundingClientRect();"
            " let lbl = e.closest('label');"
            " if (!lbl && e.id) lbl = document.querySelector(\"label[for='\" + e.id + \"']\");"
            " const lr = lbl ? lbl.getBoundingClientRect() : null;"
            " return (r.width > 0 && r.height > 0) || (!!lr && lr.width > 0 && lr.height > 0);"
            "}"
        )
    except Exception:
        return False


async def _detect_radio_groups(root, page: Page) -> list[FormField]:
    """Detect radio button groups and return them as single fields."""
    groups: dict[str, FormField] = {}

    radios = root.locator('input[type="radio"]')
    for i in range(await radios.count()):
        radio = radios.nth(i)
        if not await _input_interactable(radio):
            continue

        name = await radio.get_attribute("name") or f"radio_group_{i}"
        if name not in groups:
            label = await _get_radio_group_label(radio, page)

            groups[name] = FormField(
                label=label,
                field_type=FieldType.RADIO,
                locator=radio,
                options=[],
            )

        # Collect this option's label
        radio_label = await _get_radio_option_label(radio, page)
        if radio_label and groups[name].options is not None:
            groups[name].options.append(radio_label)

    return list(groups.values())


async def _get_radio_option_label(radio: Locator, page: Page) -> str:
    """Get the label text for a single radio button option."""
    radio_id = await radio.get_attribute("id")
    if radio_id and '"' not in radio_id and "\\" not in radio_id:
        label = page.locator(f'label[for="{radio_id}"]')
        if await label.count() > 0:
            text = await label.text_content()
            if text:
                return text.strip()

    value = await radio.get_attribute("value")
    return value or ""
