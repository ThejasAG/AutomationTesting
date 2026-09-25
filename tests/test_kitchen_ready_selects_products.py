"""Ready is disabled until the kitchen ticks the products.

The kitchen segment could never mark anything Prepared. @kitchen_ready tapped
'orderReadyBtn' straight away, but KitchenCards renders it as

    disabled={!readyActive(orders?._id)}                       KitchenCards:1129
    readyActive = id => selectedPrepList2.find(e => e.aptId == id)

so with no product selected the tap lands on a DISABLED button, changes nothing,
and the step reports "no order to Ready or Close (kitchen queue empty)" — a true
sentence about the wrong cause, on a board that plainly showed the waiter's order.

Each product row is a Radio labelled `${order.data.name}Btn` with its spaces
removed (KitchenCards:303): 'Penne Pollo' -> 'PennePolloBtn'.
"""
import inspect
import re

from automation.scenarios.cross_app_flows import FlowRunner

_READY = inspect.getsource(FlowRunner._kitchen_ready)
_PICK = inspect.getsource(FlowRunner._kitchen_pick_products)


def test_products_are_selected_before_ready_is_tapped():
    assert "_kitchen_pick_products" in _READY
    assert _READY.index("_kitchen_pick_products") < _READY.index('_appium_click_first("orderReadyBtn")')


def test_the_docstring_no_longer_denies_the_per_item_step():
    """It used to state "NO per-item selection here" outright, which is what sent
    every past investigation looking at the queue instead of the button's gate."""
    doc = inspect.getdoc(FlowRunner._kitchen_ready) or ""
    assert "NO per-item selection" not in doc
    assert "disabled" in doc


def test_the_product_label_is_the_name_with_spaces_stripped():
    """The app does (`${order.data.name}Btn`).replace(/\\s+/g, '')."""
    for name, want in [("Penne Pollo", "PennePolloBtn"),
                       ("Tagliatelle al Salmone", "TagliatellealSalmoneBtn")]:
        assert re.sub(r"\s+", "", name + "Btn") == want


def test_the_screens_static_controls_are_excluded():
    """Every 'Btn' on the kitchen screen comes from one of two files, so the static
    set is closed and source-verified:

        kitchenAllBtn kitchenPickupBtn kitchenTableBtn   Screens/Home/kitchen.js
        orderCloseBtn orderPrintBtn orderReadyBtn        Components/KitchenCards

    plus the persistent nav rail. Anything else ending 'Btn' IS a product.

    MEASURED failure of the earlier blacklist: it missed orderPrintBtn and
    preOrderBtn, tapped them as products and navigated off the kitchen board — the
    step then reported "kitchen queue empty" from the Pre-Orders screen."""
    for ident in ("orderReadyBtn", "orderCloseBtn", "orderPrintBtn",
                  "kitchenAllBtn", "kitchenTableBtn", "kitchenPickupBtn",
                  "preOrderBtn", "homeBtn", "historyBtn", "menuBtn"):
        assert ident in _PICK, f"{ident} must be excluded from product matching"


def test_a_card_with_no_product_rows_is_not_an_error():
    """The product radio renders only for items carrying modifiers (KitchenCards:299
    gates on item.data[idx2]?.header). A plain item has no row at all and Ready is
    enabled from the start — MEASURED on ticket 4698, which showed only '4698', 'I1'
    and the two buttons with orderReadyBtn already enabled."""
    assert "Ready needs no selection" in _PICK
    assert "return 0" in _PICK


def test_the_selection_count_is_returned_and_reported():
    """'products selected but no orderReadyBtn' and 'no queued order' are different
    failures and need different fixes."""
    assert "return ticked" in _PICK
    assert "selected {ticked} product(s)" in _PICK


def test_ready_remains_the_assertion():
    """Closing a leftover prepared ticket must not make the step green — that bug
    was fixed before and must stay fixed."""
    assert "if readied:\n            return True" in _READY
    assert "only closed a" in _READY
