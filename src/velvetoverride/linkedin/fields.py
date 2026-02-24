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


async def detect_form_fields(page: Page) -> list[FormField]:
    """Scan the current Easy Apply form step and detect all input fields.

    Returns a list of FormField objects describing each field.
    """
    fields: list[FormField] = []

    # --- Text inputs ---
    text_inputs = page.locator(
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
    num_inputs = page.locator('input[type="number"]')
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
    textareas = page.locator("textarea")
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
    selects = page.locator("select")
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
    radio_groups = await _detect_radio_groups(page)
    fields.extend(radio_groups)

    # --- Checkboxes ---
    checkboxes = page.locator('input[type="checkbox"]')
    for i in range(await checkboxes.count()):
        cb = checkboxes.nth(i)
        if not await cb.is_visible():
            continue
        label = await _get_field_label(cb, page)
        fields.append(FormField(
            label=label,
            field_type=FieldType.CHECKBOX,
            locator=cb,
            required=await _is_required(cb),
        ))

    # --- File upload ---
    file_inputs = page.locator('input[type="file"]')
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
    """Try multiple strategies to find the label for a form element."""
    # Strategy 1: aria-label attribute
    aria_label = await element.get_attribute("aria-label")
    if aria_label:
        return aria_label.strip()

    # Strategy 2: associated <label> via 'for' attribute
    el_id = await element.get_attribute("id")
    if el_id:
        label_el = page.locator(f'label[for="{el_id}"]')
        if await label_el.count() > 0:
            text = await label_el.text_content()
            if text:
                return text.strip()

    # Strategy 3: placeholder
    placeholder = await element.get_attribute("placeholder")
    if placeholder:
        return placeholder.strip()

    # Strategy 4: closest parent with label-like class
    parent = element.locator("xpath=ancestor::div[contains(@class, 'form-element') or contains(@class, 'fb-form')]")
    if await parent.count() > 0:
        label_in_parent = parent.first.locator("label, .fb-form-element-label, .artdeco-text-input--label")
        if await label_in_parent.count() > 0:
            text = await label_in_parent.first.text_content()
            if text:
                return text.strip()

    # Strategy 5: name attribute as last resort
    name = await element.get_attribute("name")
    if name:
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


async def _detect_radio_groups(page: Page) -> list[FormField]:
    """Detect radio button groups and return them as single fields."""
    groups: dict[str, FormField] = {}

    radios = page.locator('input[type="radio"]')
    for i in range(await radios.count()):
        radio = radios.nth(i)
        if not await radio.is_visible():
            continue

        name = await radio.get_attribute("name") or f"radio_group_{i}"
        if name not in groups:
            label = await _get_field_label(radio, page)
            # Try to get the group label from a parent fieldset or form-group
            fieldset = radio.locator("xpath=ancestor::fieldset")
            if await fieldset.count() > 0:
                legend = fieldset.locator("legend")
                if await legend.count() > 0:
                    label = (await legend.text_content() or label).strip()

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
    if radio_id:
        label = page.locator(f'label[for="{radio_id}"]')
        if await label.count() > 0:
            text = await label.text_content()
            if text:
                return text.strip()

    value = await radio.get_attribute("value")
    return value or ""
