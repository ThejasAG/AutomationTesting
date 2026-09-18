"""The ADD NEW ITEM sheet must never be mistaken for the opened order summary.

`addItemsBtn` is a legitimate marker that the order screen opened — but it ALSO
sits on the ADD NEW ITEM sheet, a different screen that happens to share it. So a
run that mis-tapped its way onto that sheet satisfied opened() and reported the
reservation open; every following step then ran against the sheet. The ids in
NOT_ORDER_SCREEN exist only while the sheet is up, which is what tells the two
apart.

Screen dump from the real failure, with the sheet up and 'addItemsBtn' present:
    ['STG-VyaBusiness', 'homeBtn', 'ADD NEW ITEM', 'addNewItemClose', 'Summary',
     'addNewItemInput', 'historyBtn', 'addNewItemAll', 'PASTA Item', ...]
"""
from automation.scenarios.cross_app_flows import FlowRunner


# The check under test, kept in one place so the test states the rule rather than
# re-implementing it: sheet ids veto, otherwise any opened-marker counts.
OPENED = ("selectAllItemsBtn", "addItemsBtn", "assignToBtn",
          "closeEventModal", "sendToKitchenBtn", "AssignTableBtn", "closeModal")
NOT_ORDER_SCREEN = ("addNewItemClose", "addNewItemInput", "addNewItemAll")


def _opened(ids) -> bool:
    seen = set(ids)
    if any(m in seen for m in NOT_ORDER_SCREEN):
        return False
    return any(m in seen for m in OPENED)


def test_the_add_item_sheet_is_not_an_opened_order():
    # The real dump. 'addItemsBtn' is present, and it must not be believed.
    assert not _opened([
        "STG-VyaBusiness", "homeBtn", "ADD NEW ITEM", "addNewItemClose", "Summary",
        "addNewItemInput", "historyBtn", "addNewItemAll", "addItemsBtn",
        "PASTA Item", "PIZZA Item", "DRINKS Item",
    ])


def test_a_real_order_summary_still_counts_as_opened():
    assert _opened(["selectAllItemsBtn", "addItemsBtn", "sendToKitchenBtn", "Summary"])


def test_the_phone_assign_table_screen_still_counts_as_opened():
    assert _opened(["Please assign a Table", "AssignTableBtn", "closeModal"])


def test_the_bookings_board_is_not_an_opened_order():
    assert not _opened(["RoopaDcardReserved", "homeBtn", "historyBtn"])


def test_one_sheet_id_alone_is_enough_to_veto():
    # The sheet renders progressively; any of its ids means it is up.
    for sheet_id in NOT_ORDER_SCREEN:
        assert not _opened(["addItemsBtn", sheet_id]), sheet_id


def test_the_runner_still_carries_the_sheet_ids_it_dismisses():
    # _dismiss_add_item_sheet keys off the same ids; if one list is edited and the
    # other is not, the recovery silently stops matching the screen it recovers from.
    assert set(FlowRunner._ADD_ITEM_SHEET) == set(NOT_ORDER_SCREEN)
