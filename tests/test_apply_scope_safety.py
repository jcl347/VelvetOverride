"""Safety: the bot must never interact with the user's own LinkedIn UI.

A missing Easy Apply modal previously made detect_form_fields fall back to
scanning the WHOLE page, so the bot detected the messaging drawer's
conversation checkboxes and clicked them.
"""

import inspect

from velvetoverride.linkedin import apply as apply_mod


def test_missing_modal_aborts_instead_of_scanning_page():
    src = inspect.getsource(apply_mod.EasyApplyFlow._apply_easy_apply) \
        if hasattr(apply_mod, "EasyApplyFlow") and hasattr(
            getattr(apply_mod, "EasyApplyFlow"), "_apply_easy_apply") \
        else inspect.getsource(apply_mod)
    # The guard must exist between getting the modal and detecting fields
    assert "modal_missing" in src or "refused to scan the full page" in src
    i_modal = src.index("_easy_apply_modal()")
    i_guard = src.index("if modal is None:", i_modal)
    i_detect = src.index("detect_form_fields(self._page, scope=modal)", i_modal)
    assert i_modal < i_guard < i_detect, "modal-None guard must precede field detection"


def test_unlabeled_fields_are_skipped():
    src = inspect.getsource(apply_mod)
    assert "unlabeled_field_skipped" in src
    # and it guards before the solver is consulted
    i_guard = src.index("unlabeled_field_skipped")
    i_solve = src.index("await self._solver.solve(")
    assert i_guard < i_solve, "unlabeled-field guard must run before solving"


def test_detect_form_fields_still_supports_scope():
    from velvetoverride.linkedin.fields import detect_form_fields
    sig = inspect.signature(detect_form_fields)
    assert "scope" in sig.parameters
