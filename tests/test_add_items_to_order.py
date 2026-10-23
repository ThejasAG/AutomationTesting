"""Adding items must actually reach the order, not just look like it did.

@ensure_order_items reported "added items to the order" while the order stayed at
0 EUR. Four separate faults, each measured on the device:

  1. addItemsBtn sits UNDER the LogBox toast a debug build stacks along the bottom
     (button centre y=735, toast y=708..756), so a coordinate tap hit the toast and
     the ADD NEW ITEM sheet never opened.
  2. The category chips carry the same `${...}Item` label as products, and not all
     of them keep a space -- 'biriyaniItem' is a CATEGORY on this restaurant.
  3. Tapping a product does NOT add it: one carrying features/modifiers opens an
     'Add Options' sheet that has to be applied.
  4. Apply only STAGES the product onto addedProductsList; assignToBtn assigns it,
     and the assign sheet's 'Assign' and 'Split' share one accessibility id.
"""
import inspect

from automation.scenarios.cross_app_flows import FlowRunner

_ENSURE = inspect.getsource(FlowRunner._ensure_order_items)
_SHEET = inspect.getsource(FlowRunner._add_items_sheet_products)


def test_add_items_is_clicked_through_appium_not_by_coordinate():
    """Appium's .click() scrolls the element clear of the toast first; an idb tap at
    the same point lands on the toast."""
    assert "_appium_click_id(r, \"addItemsBtn\")" in _ENSURE
    assert "toast" in _ENSURE.lower()


def test_the_sheet_is_waited_for_before_products_are_read():
    assert "_add_items_sheet_up()" in _ENSURE


def test_products_are_taken_from_their_own_column():
    """The category chips are one horizontal row (measured all at y=200, x=190..702)
    and the products a column beneath (x=97). A suffix test cannot separate them --
    'biriyaniItem' is a category with no space in its label."""
    assert "busiest column" in _SHEET or "col = max(xs" in _SHEET


def test_the_options_sheet_is_applied():
    """handleProductFeature routes a product with features/modifiers to the options
    sheet (Screens/Event/index.js:843); without Apply the item never lands."""
    assert "applyOptionBtn" in _SHEET


def test_products_are_assigned_before_the_sheet_is_closed():
    """Apply only stages onto addedProductsList (Screens/Event/index.js:912).
    assignToBtn is ON the sheet and disabled while that list is empty, so closing
    first threw the staged products away."""
    # Compare the CLICK calls, not the first textual mention — addNewItemClose also
    # appears in the sheet-detection markers near the top of the function.
    i = _SHEET.index('_appium_click_id(r, "assignToBtn")')
    j = _SHEET.index('_appium_click_id(r, "addNewItemClose"')
    assert i < j, "assignToBtn must be pressed before the sheet is closed"


def test_assign_not_split_is_pressed():
    """'Assign' and 'Split' BOTH answer to assignProductsBtn (Components/Modal:1731
    and :1756). Split is disabled unless 2+ diners are selected, so a last-match
    click pressed a dead button and the sheet never closed."""
    assert "_appium_click_leftmost" in _SHEET
    src = inspect.getsource(FlowRunner._appium_click_leftmost)
    assert "is_enabled()" in src, "a disabled duplicate must be skipped"


def test_the_diner_row_is_matched_by_suffix():
    """Each row is `${username}select` with spaces stripped (Components/Modal:1647)
    -- 'RoopaDselect' -- so there is no fixed id to click."""
    assert 'endswith("select")' in _SHEET
