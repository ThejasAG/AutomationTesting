"""Horizontal card search on the waiter bookings board.

Every hour row is its own <ScrollView horizontal={true}> (Business
App/Screens/Home/index.js:1190) with flexShrink:0 cards side by side. Measured live
on the iPad (viewport 1210 wide), one row at y=434:

    RoopaDcardCompleted      x=208     RoopaDcardCompleted      x=700
    tanishcardReserved       x=1192    tanishcardInProgress     x=1684
    NooluNagacardInProgress  x=2176

idb and Appium both report live, scroll-adjusted coordinates, so an off-screen card is
DISCOVERED but not clickable. Two measured gesture facts drive these tests:
  * dragFromToForDuration moves this row 0pt; 'mobile: swipe' on an element moved it 680pt.
  * a swipe on an OFF-SCREEN element is a silent no-op in both directions — it must never
    be mistaken for the end of the row.
"""
import pytest

from automation.scenarios import cross_app_flows as F

VW = 1210.0          # measured iPad viewport width
ROW_Y = 434


def card(label, x, y=ROW_Y, w=492, h=144):
    return {"id": "", "label": label, "type": "GenericElement",
            "x": x, "y": y, "w": w, "h": h,
            "cx": int(x + w / 2), "cy": int(y + h / 2)}


# The live board, verbatim.
BOARD = [card("RoopaDcardCompleted", 208), card("RoopaDcardCompleted", 700),
         card("tanishcardReserved", 1192), card("tanishcardInProgress", 1684),
         card("NooluNagacardInProgress", 2176)]


def runner(frames, viewport=VW):
    """frames: list of board states; each dispatched swipe advances to the next."""
    fr = object.__new__(F.FlowRunner)
    state = {"i": 0, "swipes": []}

    class D:
        def get_window_size(self):
            return {"width": viewport, "height": 834}

    class R:
        d = D()

    fr._cards_on_board = lambda els=None: [dict(c) for c in frames[min(state["i"], len(frames) - 1)]]

    def swipe(r, surface, direction):
        state["swipes"].append((surface["label"], surface["x"], direction))
        state["i"] += 1
        return True

    fr._swipe_element = swipe
    return fr, R(), state


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda *_: None)


# ── pure geometry ────────────────────────────────────────────────────────────
def test_visible_px_measures_the_on_screen_portion():
    assert F.FlowRunner._card_visible_px(card("a", 208), VW) == 492      # fully on
    assert F.FlowRunner._card_visible_px(card("a", 2176), VW) == 0       # fully off
    assert F.FlowRunner._card_visible_px(card("a", 1004), VW) == 206     # clipped
    assert F.FlowRunner._card_visible_px(card("a", -472), VW) == 20      # off to the left


def test_only_a_fully_visible_card_counts_as_in_the_viewport():
    assert F.FlowRunner._target_in_viewport(card("a", 208), VW) is True
    assert F.FlowRunner._target_in_viewport(card("a", 1004), VW) is False   # half-clipped
    assert F.FlowRunner._target_in_viewport(card("a", -10), VW) is False


def test_direction_follows_the_finger_not_the_content():
    # XCUITest: 'left' = finger travels left = reveals what is off to the RIGHT.
    assert F.FlowRunner._swipe_direction(card("a", 2176), VW) == "left"
    assert F.FlowRunner._swipe_direction(card("a", -472), VW) == "right"


def test_direction_uses_the_clipped_edge_not_x_alone():
    """A card at x=1004 (spans 1004..1496) has its left edge on screen but its right
    half off it, and must keep moving LEFT. Deciding on x >= viewport sent this case
    back the other way and the loop oscillated instead of converging."""
    assert F.FlowRunner._swipe_direction(card("a", 1004), VW) == "left"
    assert F.FlowRunner._swipe_direction(card("a", -10), VW) == "right"
    assert F.FlowRunner._swipe_direction(card("a", 208), VW) == "right"   # fully visible


def test_the_row_is_isolated_by_y():
    other = card("OtherRowcardReserved", 208, y=900)
    row = F.FlowRunner._row_cards(BOARD + [other], BOARD[0])
    assert other not in row and len(row) == len(BOARD)


# ── swipe surface selection ──────────────────────────────────────────────────
def test_picks_the_most_visible_card_as_the_swipe_surface():
    s = F.FlowRunner._pick_swipe_surface(BOARD, BOARD[-1], VW)
    assert (s["label"], s["x"]) == ("RoopaDcardCompleted", 208)


def test_an_off_screen_card_is_never_used_as_the_swipe_surface():
    """The measured trap: swiping the RoopaDcardCompleted instance at x=-472 was a
    no-op in BOTH directions and looked exactly like the end of the row."""
    scrolled = [card("RoopaDcardCompleted", -472), card("RoopaDcardCompleted", 20),
                card("tanishcardReserved", 512), card("tanishcardInProgress", 1004),
                card("NooluNagacardInProgress", 1496)]
    s = F.FlowRunner._pick_swipe_surface(scrolled, scrolled[-1], VW)
    assert s["x"] == 20                                  # not the x=-472 twin
    assert F.FlowRunner._card_visible_px(s, VW) >= F.FlowRunner._MIN_SWIPE_SURFACE_PX


def test_the_target_itself_is_not_used_when_it_is_the_off_screen_one():
    target = BOARD[-1]                                   # x=2176, 0px visible
    s = F.FlowRunner._pick_swipe_surface(BOARD, target, VW)
    assert s is not target and s["x"] == 208


def test_no_surface_when_nothing_is_far_enough_inside_the_viewport():
    barely = [card("acardReserved", 1150), card("bcardReserved", 1642)]   # 60px, 0px
    assert F.FlowRunner._pick_swipe_surface(barely, barely[1], VW) is None


# ── identity vs status ───────────────────────────────────────────────────────
@pytest.mark.parametrize("label", ["RoopaDcardReserved", "RoopaDcardInProgress",
                                   "RoopaDcardCompleted", "RoopaDConfirmationPendingCard",
                                   "RoopaDReservedCard"])
def test_the_diner_is_matched_through_any_status_suffix(label):
    fr = object.__new__(F.FlowRunner)
    found = fr._find_target_card([card(label, 208)], "RoopaDcardReserved", "roopa")
    assert found is not None and found["label"] == label


def test_a_different_diner_is_never_substituted_for_the_target():
    fr = object.__new__(F.FlowRunner)
    board = [card("tanishcardReserved", 208), card("NooluNagacardInProgress", 700)]
    assert fr._find_target_card(board, "RoopaDcardReserved", "roopa") is None


def test_an_exact_label_wins_over_a_same_diner_sibling():
    fr = object.__new__(F.FlowRunner)
    board = [card("RoopaDcardCompleted", 208), card("RoopaDcardReserved", 700)]
    got = fr._find_target_card(board, "RoopaDcardReserved", "roopa")
    assert got["x"] == 700


# ── the search loop ──────────────────────────────────────────────────────────
def test_an_already_visible_target_needs_no_scrolling():
    fr, r, state = runner([BOARD])
    notes = []
    assert fr._scroll_card_into_view_h(r, "RoopaDcardCompleted", notes) is True
    assert state["swipes"] == []
    assert any("already in the viewport" in n for n in notes)


def test_an_off_screen_target_is_scrolled_into_the_viewport():
    # One swipe shifts the row 680pt (the measured amount), bringing x=1684 to x=1004,
    # then a second brings it to 324 — fully visible.
    def shift(board, dx):
        return [card(c["label"], c["x"] + dx) for c in board]
    frames = [BOARD, shift(BOARD, -680), shift(BOARD, -1360)]
    fr, r, state = runner(frames)
    notes = []
    assert fr._scroll_card_into_view_h(r, "tanishcardInProgress", notes) is True
    assert len(state["swipes"]) == 2
    assert all(s[1] >= 0 for s in state["swipes"])            # every surface was on screen
    assert all(d == "left" for _, _, d in state["swipes"])    # reveal content to the right
    assert any("horizontal scroll required" in n for n in notes)
    assert any("in the viewport" in n for n in notes)


def test_the_target_is_still_the_target_after_scrolling():
    """Guards against 'found a card' being satisfied by whichever card happens to be
    visible after a swipe."""
    def shift(board, dx):
        return [card(c["label"], c["x"] + dx) for c in board]
    frames = [BOARD, shift(BOARD, -680), shift(BOARD, -1360)]
    fr, r, state = runner(frames)
    notes = []
    assert fr._scroll_card_into_view_h(r, "tanishcardInProgress", notes) is True
    found = [n for n in notes if "found" in n]
    assert found and "tanishcardInProgress" in found[-1]


def test_a_stalled_row_on_a_valid_surface_reports_end_of_row():
    fr, r, state = runner([BOARD, BOARD, BOARD, BOARD])     # nothing ever moves
    notes = []
    assert fr._scroll_card_into_view_h(r, "NooluNagacardInProgress", notes) is False
    assert any("end of row reached" in n for n in notes)
    assert len(state["swipes"]) == 2                        # stops, does not spin


def test_a_no_op_swipe_is_only_believed_from_a_confirmed_visible_surface():
    """Requirement 10: the stall counter must never be driven by a gesture we could not
    place on a visible element — that case returns a distinct refusal instead."""
    off = [card("acardReserved", 1300), card("bcardReserved", 1792)]
    fr, r, state = runner([off, off, off])
    notes = []
    assert fr._scroll_card_into_view_h(r, "bcardReserved", notes) is False
    assert state["swipes"] == []                            # never gestured blindly
    assert any("refusing to gesture on an off-screen element" in n for n in notes)
    assert not any("end of row" in n for n in notes)        # NOT reported as a boundary


def test_the_loop_is_bounded_even_if_the_row_keeps_moving():
    def shift(board, dx):
        return [card(c["label"], c["x"] + dx) for c in board]
    frames = [shift(BOARD, -40 * i) for i in range(30)]     # creeps, never arrives
    fr, r, state = runner(frames)
    notes = []
    assert fr._scroll_card_into_view_h(r, "NooluNagacardInProgress", notes, tries=5) is False
    assert len(state["swipes"]) == 5
    assert any("still off-screen after 5 scrolls" in n for n in notes)


def test_a_target_absent_from_the_board_is_reported_not_scrolled_for():
    fr, r, state = runner([BOARD])
    notes = []
    assert fr._scroll_card_into_view_h(r, "SomeoneElsecardReserved", notes, "someoneelse") is False
    assert state["swipes"] == []
    assert any("not on the board" in n for n in notes)


def test_the_search_reports_the_slot_and_the_cards_it_saw():
    fr, r, state = runner([BOARD])
    notes = []
    fr._scroll_card_into_view_h(r, "RoopaDcardCompleted", notes, slot="15:30")
    joined = " ".join(notes)
    assert "[card search] slot='15:30'" in joined
    assert "visible cards:" in joined and "tanishcardReserved@x=1192" in joined
