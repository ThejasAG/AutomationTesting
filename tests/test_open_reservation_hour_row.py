"""Find the booked hour's row first, then the card under it.

My Bookings is a time-of-day calendar: one row per hour, each row its own horizontal
strip. The board opens on the current hour, so a booking an hour later sits in a row
that is off-screen DOWNWARDS. The card search only ever scrolled HORIZONTALLY, so it
located the target at x=2668 on a ~1200px viewport and tried to swipe across five
intervening cards to reach it — slow, unreliable (the swipe surface is itself
off-screen), and the step that hung for 240s.

Scrolling to the hour first is how a person reads the board: find the time, then the
event under that time.
"""
import inspect
import re

from automation.scenarios.cross_app_flows import FlowRunner

_SRC = inspect.getsource(FlowRunner)


def _hour_row(slot: str) -> str:
    """The mapping @open_reservation uses to turn a booked slot into its row."""
    ms = re.match(r"^(\d{1,2})", slot)
    return f"{int(ms.group(1)):02d}:00" if ms else ""


def test_a_slot_maps_to_its_hour_row():
    """18:15 and 18:30 both live under the 18:00 row on this board."""
    assert _hour_row("18:15") == "18:00"
    assert _hour_row("18:30") == "18:00"
    assert _hour_row("17:30") == "17:00"
    assert _hour_row("20:20") == "20:00"


def test_a_single_digit_hour_is_zero_padded():
    """'9:05' must match the label '09:00', not '9:00'."""
    assert _hour_row("9:05") == "09:00"


def test_the_calendar_is_scrolled_to_the_hour_before_choosing_a_card():
    assert "def scroll_to_hour" in _SRC
    assert "scrolled the calendar to the" in _SRC


def test_the_scroll_happens_before_the_card_is_picked():
    """Picking first would choose a card from whatever row happens to be visible."""
    assert _SRC.index("def scroll_to_hour") < _SRC.index("lbl = pick_label")


def test_the_scroll_waits_for_the_board_to_render():
    """Scrolling an empty list does nothing and burns the attempt."""
    assert "bookings_loaded(els_now)" in _SRC


def test_the_scroll_runs_only_once_per_search():
    """Re-scrolling on every poll would walk the calendar away from the target row."""
    assert "if not scrolled and hour_lbl" in _SRC


def test_the_horizontal_search_remains_as_a_fallback():
    """Some rows genuinely hold more cards than fit; that path still has to work."""
    assert "_scroll_card_into_view_h" in _SRC


def test_the_scroll_direction_follows_the_row_position():
    """Later hours are below the fold; earlier ones above.

    MEASURED on the iPad: 'up' scrolls the calendar TOWARDS LATER HOURS — one swipe
    moved the 13:00 row from y=1679 to y=1089. The inverted form was a silent no-op,
    which is why a run reported "scrolled the calendar to the 13:00 row" while 13:00
    never left y=1658 on an 834pt screen, then tapped a stale coordinate.
    """
    src = _SRC[_SRC.index("def scroll_to_hour"):]
    assert '"up" if y >' in src and '"down"' in src


def test_a_missing_hour_label_does_not_scroll_blindly():
    """With no booked slot there is no row to aim at — scrolling would be guessing."""
    src = _SRC[_SRC.index("def scroll_to_hour"):]
    assert "if not hour_lbl:" in src


# ── opening the table sheet before looking for its chips ─────────────────────

_ASSIGN = inspect.getsource(FlowRunner._assign_table)


def test_assign_table_opens_the_table_sheet_first():
    """Opening a reservation shows the EVENT sheet; the table chips live in a
    separate sheet that only renders after onModifyTableClick. Without opening it the
    chip search finds nothing and the run reports "no Apply/Confirm to commit" —
    true, but only because the sheet holding them was never opened."""
    assert "def open_table_sheet" in _ASSIGN
    assert "modifyTable" in _ASSIGN


def test_opening_the_sheet_is_a_no_op_when_chips_are_already_there():
    """Some builds open straight onto the chips; tapping again would close them."""
    src = _ASSIGN[_ASSIGN.index("def open_table_sheet"):]
    body = src[src.index('"""', src.index('"""') + 3) + 3:]   # past the docstring
    assert body.lstrip().startswith("if chips():"), \
        "the early return must be the first thing the opener does"


def test_the_sheet_is_given_time_to_fetch_its_tables():
    """The list arrives from the backend, so the chips are not present on the frame
    the sheet opens."""
    src = _ASSIGN[_ASSIGN.index("def open_table_sheet"):]
    assert "for _ in range(8)" in src


def test_every_event_opening_flow_shares_one_implementation():
    """@open_order (serve) and @open_reservation (assign) must not drift apart — a
    fix to how an event is located has to apply to both."""
    src = inspect.getsource(FlowRunner)
    assert 'self._open_reservation(r, notes, statuses=("inprogress",)' in src


# ── no horizontal sliding: open the hour's events list instead ───────────────

def test_the_hour_events_badge_is_opened():
    """An hour's bookings are laid out SIDEWAYS, so the 4th or 5th is off-screen and
    could only be reached by swiping the row. The badge beside each hour lists that
    hour's events vertically — every one reachable without a horizontal gesture."""
    assert "def open_events_for_hour" in _SRC
    assert "no horizontal scrolling" in _SRC


def test_the_badge_is_matched_by_its_own_nearest_hour():
    """The badge renders BELOW its hour label (measured +50px on the iPad), outside
    the row tolerance used for cards — so matching it to the label directly fails."""
    src = _SRC[_SRC.index("def open_events_for_hour"):]
    assert "nearest[0] == hour_lbl" in src


def test_the_events_list_is_opened_after_reaching_the_row():
    """Tapping a badge before the row is on screen taps the wrong hour."""
    assert _SRC.index("def scroll_to_hour") < _SRC.index("open_events_for_hour(self._idb_els())")


def test_horizontal_scrolling_is_a_last_resort():
    """It must not run when the card is already reachable — it is the slowest thing
    this step does and it hung a run for 240s."""
    assert "_reachable" in _SRC
    assert "if not _reachable and not self._scroll_card_into_view_h(" in _SRC


def test_a_missing_badge_does_not_fail_the_step():
    """An hour with a single booking may render no badge at all."""
    src = _SRC[_SRC.index("def open_events_for_hour"):]
    assert "if b is None:" in src and "return False" in src


# ── the run that hung at @assign_table for 240s ──────────────────────────────

def test_the_board_filter_tab_is_not_treated_as_a_sheet_control():
    """'tableBtn' is the BOARD's booking-type filter tab (Screens/Home/index.js,
    beside allBtn/pickupBtn, measured live at x=598,y=55) — not a control on the
    reservation sheet. Tapping it navigated AWAY from the opened reservation, so the
    next pass found no chips and re-opened the sheet, alternating
    modifyTable/tableBtn six times until the 240s step timeout killed the segment."""
    opener = _ASSIGN[_ASSIGN.index("def open_table_sheet"):]
    code = "\n".join(l for l in opener.splitlines()
                     if not l.lstrip().startswith("#"))
    openers = re.search(r"for ident in \(([^)]*)\)", code).group(1)
    assert "tableBtn" not in openers, \
        f"tableBtn navigates off the reservation; openers were ({openers})"
    assert "modifyTable" in openers


def test_the_sheet_is_not_reopened_once_the_reservation_has_gone():
    """Without this the retry loop taps whatever the current screen offers and burns
    the whole step budget instead of reporting where it actually ended up."""
    assert "still_on_sheet" in _ASSIGN
    assert "the reservation closed before the table" in _ASSIGN
    # RETURN, not break: breaking falls through to the legacy coordinate path,
    # which scans the CURRENT screen for [IOio]\d and matched 'I1' on the order
    # summary -- tapping it, finding no Apply, and blaming the table sheet.
    i = _ASSIGN.index("still_on_sheet:")
    assert "return False" in _ASSIGN[i:i + 1400]


# ── the badge is a toggle, and the sidebar is vertical ───────────────────────

def test_the_badge_tap_is_confirmed_to_have_opened_the_list():
    """addCountfunc is `selectEventTime === el ? null : el` — a TOGGLE. With another
    hour's list already open, the tap CLOSES it and the board returns to its
    horizontal strips while the note still claims the list was opened."""
    src = _SRC[_SRC.index("def open_events_for_hour"):]
    assert "_events_sidebar_up" in src


def test_the_sidebar_is_detected_by_its_own_controls():
    """closeEventModal is AddCountModal's close button; 'Orders Not Found !!' is its
    empty state — the list IS up, it just has nothing to show for that hour."""
    src = inspect.getsource(FlowRunner._events_sidebar_up)
    assert "closeEventModal" in src
    assert "orders not found" in src.lower()


def test_no_horizontal_swipe_while_the_events_list_is_open():
    """The list is a VERTICAL sidebar. A horizontal swipe reaches nothing on it and
    lands on the board behind it — which is why one run logged three identical
    'new visible cards' lines and then clicked anyway 220s later."""
    assert "events list is open; selecting from it" in _SRC
    i = _SRC.index("_sidebar = self._events_sidebar_up")
    j = _SRC.index("_scroll_card_into_view_h(\n", i)
    assert "elif" in _SRC[i:j], "the horizontal scroll must be the else-branch"


# ── the sidebar is the only place a booking can be NAMED ─────────────────────

from automation.scenarios.cross_app_flows import _SIDEBAR_ROW_RE, _OPENED_VIA_SIDEBAR

_ROW = "4657  RESERVED 13:45 - 14:45 \U000f0a70 13:38"


def test_a_sidebar_row_is_recognised_by_its_booking_window():
    """OrderCard has no accessibilityLabel, so idb reports its children's text. The
    'HH:MM - HH:MM' window is the part the board's cards never render."""
    assert _SIDEBAR_ROW_RE.search(_ROW).group(0) == "13:45 - 14:45"


def test_a_board_card_is_not_mistaken_for_a_sidebar_row():
    """'RoopaDcardReserved' carries no time at all — that is the whole problem."""
    assert not _SIDEBAR_ROW_RE.search("RoopaDcardReserved")


def test_the_populated_sidebar_is_detected():
    """The common case exposes neither closeEventModal nor the empty-state text, so
    detecting only those reported "sidebar up: False" against a list of nine rows."""
    assert FlowRunner._events_sidebar_up([{"label": _ROW, "id": ""}])


def test_the_booking_is_chosen_by_its_start_time():
    """Nine bookings in the 13:00 row all render 'RoopaDcardReserved' on the board;
    only the sidebar states which one starts at 13:45."""
    src = inspect.getsource(FlowRunner._click_sidebar_row)
    assert "want_hhmm" in src and "_SIDEBAR_ROW_RE" in src


def test_the_status_is_checked_too():
    """An EXPIRED row at the same start time must not be opened instead."""
    src = inspect.getsource(FlowRunner._click_sidebar_row)
    assert "statuses" in src and "_norm(lbl)" in src


def test_opening_from_the_sidebar_is_not_read_as_a_missing_card():
    """A falsy sentinel would send the runner down the "card not found" path and
    relaunch the app on top of a reservation that had just opened."""
    assert _OPENED_VIA_SIDEBAR is not None and _OPENED_VIA_SIDEBAR != ""


def test_the_sidebar_close_button_does_not_count_as_an_opened_reservation():
    """'closeEventModal' is in OPENED, but it is the EVENTS LIST's own close button —
    so verifying with it would report success the instant the list appeared."""
    src = inspect.getsource(FlowRunner._open_reservation)
    i = src.index("SIDEBAR_SAFE")
    assert "closeEventModal" not in src[i:src.index("for _ in range(12)", i)]


def test_the_calendar_swipe_is_element_anchored():
    """The elementless form is a measured no-op: ten swipes left the 13:00 badge at
    y=1730 exactly."""
    src = inspect.getsource(FlowRunner._swipe_calendar)
    assert '"element"' in src


def test_the_swipe_anchor_must_itself_be_on_screen():
    """An hour label resolves to TWO elements and the first is not always visible;
    swiping an off-screen anchor silently does nothing."""
    src = inspect.getsource(FlowRunner._swipe_calendar)
    assert "anchor" in src and "0.1 * h" in src


def test_the_badge_is_scrolled_into_view_before_it_is_tapped():
    """The badge sits BELOW its hour label — measured 13:00 at y=1658 with its badge
    at y=1706 — so scrolling the LABEL on screen does not make the badge tappable."""
    src = _SRC[_SRC.index("def open_events_for_hour"):]
    assert "off-screen" in src and "_swipe_calendar" in src


# ── the run that opened the wrong booking and hung ──────────────────────────

def test_the_events_list_waits_for_its_ROWS_not_just_the_panel():
    """MEASURED: the rows land ~3.9s after the badge tap, past the old flat 2.0s
    wait. Returning early made _click_sidebar_row scan an empty list, find nothing,
    and fall back to the board — where two bookings share one 'RoopaDcardReserved'
    label and the wrong one gets opened."""
    src = _SRC[_SRC.index("def open_events_for_hour"):]
    assert "_SIDEBAR_ROW_RE" in src
    assert "for _ in range(16)" in src


def test_an_hour_with_no_bookings_is_not_treated_as_a_slow_render():
    """Otherwise every genuinely empty hour costs the full row-wait."""
    src = _SRC[_SRC.index("def open_events_for_hour"):]
    assert "orders not found" in src.lower()


def test_a_sidebar_miss_does_not_silently_fall_back_to_the_board():
    """The board cannot tell two same-labelled bookings apart, so falling through is
    a guess. Say what the list actually held instead."""
    assert "no {slot} booking in the" in _SRC or "no {slot} booking" in _SRC
    assert "it holds:" in _SRC


def test_the_card_search_stops_before_the_step_watchdog_does():
    """Being killed as 'step hung' discards every note explaining what was tried —
    which is exactly what the 240s failure reported."""
    assert "_deadline" in _SRC
    assert "STEP_TIMEOUT" in _SRC


def test_the_calendar_is_reached_in_one_drag_where_possible():
    """'mobile: swipe' costs a measured 9.3s and moves a fixed ~590pt, so walking
    00:00 -> 15:00 is six swipes and ~82s of a 240s budget. A direct drag covers the
    known distance in one call (measured 6.1s, 1923 -> 1248)."""
    src = inspect.getsource(FlowRunner._swipe_calendar)
    assert "dragFromToForDuration" in src
    assert "_scroll_target_hour" in src


def test_the_drag_falls_back_to_swiping_when_it_does_not_move():
    """The drag is an optimisation, not a replacement — if the list does not move,
    the verified swipe loop still has to run."""
    src = inspect.getsource(FlowRunner._swipe_calendar)
    i = src.index("dragFromToForDuration")
    assert "for lbl in on_screen" in src[i:]


def test_the_target_hour_is_published_before_scrolling():
    """Without it the drag has no distance to cover and silently does nothing."""
    src = _SRC[_SRC.index("def scroll_to_hour"):]
    assert "self._scroll_target_hour = hour_lbl" in src


# ── an open reservation must not be relaunched away ──────────────────────────

class _FakeDriver:
    def __init__(self): self.relaunched = False
    def find_elements(self, *a, **k): return []
    def terminate_app(self, *a, **k): self.relaunched = True
    def activate_app(self, *a, **k): pass


class _FakeRunner:
    def __init__(self, d): self.d = d


def _dismiss_with(els):
    fr = FlowRunner.__new__(FlowRunner)
    fr.business_bundle = "org.example.app"
    fr._idb_els = lambda *a, **k: els
    d = _FakeDriver()
    ok = fr._dismiss_table_modal(_FakeRunner(d), [])
    return ok, d.relaunched


def test_an_open_reservation_is_not_relaunched_away():
    """A reservation opens STRAIGHT ONTO its assign-a-table screen, which shows the
    same 'AssignTableBtn' as a leftover sheet — so _table_modal_in cannot tell them
    apart. Relaunching threw the booking away, put the app back on the bookings
    board, and @assign_table then ran with no reservation open. That is the reported
    "it goes back and then tries to assign the table"."""
    ok, relaunched = _dismiss_with([
        {"label": "AssignTableBtn", "id": ""},
        {"label": "closeEventModal", "id": ""},
    ])
    assert not relaunched, "the open reservation was relaunched away"
    assert ok


def test_a_leftover_sheet_over_the_board_is_still_cleared():
    """The guard must not disable the real cleanup: a stale sheet hides the board and
    the card scan then reports '0 card element(s) seen'."""
    ok, relaunched = _dismiss_with([
        {"label": "applyTableBtn", "id": ""},
        {"label": "addNewEvent", "id": ""},
        {"label": "qrScaner", "id": ""},
    ])
    assert relaunched, "a leftover sheet over the board must still be cleared"


def test_the_board_behind_the_sheet_is_what_tells_them_apart():
    """A leftover sheet sits OVER the board, so the board's own controls are still in
    the tree; a reservation replaces the board entirely."""
    src = inspect.getsource(FlowRunner._dismiss_table_modal)
    assert "board_behind" in src and "on_reservation" in src


# ── the form reported a time it never selected ───────────────────────────────

_SLOT = inspect.getsource(FlowRunner._first_time_slot)


def test_only_the_forms_own_chips_count_as_time_slots():
    """A bare 'HH:00' in the BOARD's gutter is an hour row, not a bookable slot.
    Matching those picked '17:00' when the real chips started at 17:35.

    But the suffix cannot be REQUIRED: the consumer's chips have none. The rule is
    the gutter, not the suffix -- see the measured-tree tests below."""
    assert "hour_gutter_x" in _SLOT
    assert r"(Btn)?$" in _SLOT, "both label forms must be accepted"


def test_the_chip_is_looked_up_by_its_real_id():
    """The chip's accessibilityLabel is `${el}Btn` (AddNewEventModal/index.js:1181).
    MEASURED: '17:35' matched 0 elements by id AND by predicate; '17:35Btn' matched
    3. The bare-time lookup always failed, so the run fell through to a coordinate
    tap that could not work either."""
    assert 'f"{safe}Btn"' in _SLOT


def test_an_offscreen_chip_is_not_tapped_by_coordinate():
    """idb reports CONTENT-space x for this horizontally scrolled row -- slots run
    out to x=3680 on a 1210pt screen -- so a coordinate tap only lands for a chip
    genuinely in the viewport."""
    # The message is wrapped across two source lines, so match its halves.
    assert "is outside the " in _SLOT and "visible strip" in _SLOT


def test_the_selection_is_verified_not_assumed():
    """A click that lands on nothing raises no error, so 'tapped' only ever meant
    'the gesture was dispatched'. That is how the run reported "selected slot
    '17:00'" against a form showing no time at all."""
    assert "_time_slot_committed" in _SLOT
    i = _SLOT.index("_time_slot_committed")
    assert "return False" in _SLOT[i:i + 400]


def test_the_commit_check_uses_visibility_not_the_selected_attribute():
    """The chip shows selection only as a background colour, which accessibility
    does not expose -- measured: `selected` stays "false" and the idb tree is
    byte-identical before and after a real selection. What does change is the
    chip's position: Appium's .click() auto-scrolls it clear of the clipped region
    (measured 17:35Btn y=793 -> y=645, above saveBtn at y=710)."""
    src = inspect.getsource(FlowRunner._time_slot_committed)
    assert "is_displayed()" in src
    assert "saveBtn" in src


def test_the_logbox_toasts_are_not_dismissed_or_avoided():
    """Both were tried and both were worse. Dropping covered chips threw away the
    EARLIEST slots -- the only ones inside the waiter's 30-minute open window.
    Tapping a strip's close control expands it into the full stack-trace viewer
    about as often as it closes it (measured: 2 strips -> 'Log 11 of 11', 9 strips).
    The Appium click already scrolls the chip clear of them."""
    assert "skipped" not in _SLOT or "LogBox toast" not in _SLOT.split("skipped")[1][:80]
    assert "_dismiss_logbox_strips" not in _SLOT


# ── the two apps label their time slots differently ─────────────────────────

def _slots_from(els):
    """The selection _read_slots performs, over (label, x) pairs."""
    xs = {}
    for lb, x in els:
        if re.match(r"^\d{1,2}:00$", lb):
            xs[x] = xs.get(x, 0) + 1
    gutter = next((x for x, n in xs.items() if n >= 3), None)
    out = []
    for lb, x in els:
        m = re.match(r"^(\d{1,2})[:.](\d{2})(Btn)?$", lb)
        if not m:
            continue
        if not m.group(3) and m.group(2) == "00" and gutter is not None \
                and abs(x - gutter) < 8:
            continue
        out.append(lb[:-3] if m.group(3) else lb)
    return gutter, out


# Both trees below are MEASURED, not invented.
_BUSINESS = [("%02d:00" % h, 147) for h in range(24)] + \
            [("17:35Btn", 850), ("17:40Btn", 972), ("17:45Btn", 1096)]
_CONSUMER = [("17:45", 21), ("17:50", 142), ("17:55", 263),
             ("18:00", 382), ("18:05", 506), ("21:00", 1956)]


def test_the_consumers_bare_time_chips_are_still_slots():
    """The diner's form labels its chips '17:45', with NO 'Btn' suffix (measured on
    the iPhone at x=21). Requiring the suffix matched every business chip and no
    consumer chip, so the diner's booking failed with "no bookable time chip on this
    screen" against a form showing eighteen of them."""
    _, slots = _slots_from(_CONSUMER)
    assert "17:45" in slots and "18:05" in slots


def test_a_consumer_full_hour_slot_is_not_mistaken_for_a_board_label():
    """'18:00' and '21:00' are REAL bookable chips on the diner's form. There is no
    bookings board behind it, so nothing may be excluded."""
    gutter, slots = _slots_from(_CONSUMER)
    assert gutter is None
    assert "18:00" in slots and "21:00" in slots


def test_the_waiters_board_hour_labels_are_still_excluded():
    """They sit in the board's gutter BEHIND the modal, and matching them picked
    '17:00' -- an hour row, not a bookable slot -- when the chips started at 17:35."""
    gutter, slots = _slots_from(_BUSINESS)
    assert gutter == 147
    assert slots == ["17:35", "17:40", "17:45"]


def test_the_gutter_needs_several_hours_stacked_at_one_x():
    """One or two full-hour chips in a row are ordinary slots; a gutter is a COLUMN
    of them. Requiring three keeps the consumer's 18:00/21:00 safe."""
    assert _slots_from([("18:00", 382), ("21:00", 1956)])[0] is None


def test_the_nav_rail_does_not_count_as_the_board_behind_the_sheet():
    """'historyBtn' and 'menuBtn' are the PERSISTENT LEFT NAV RAIL — present on every
    screen, including an open reservation. Including them made board_behind always
    True, so the "don't relaunch a real reservation" guard never fired and the app
    reloaded from scratch the moment @assign_table touched the table sheet. Manual
    testing worked precisely because a person never triggers that relaunch."""
    src = inspect.getsource(FlowRunner._dismiss_table_modal)
    i = src.index("board_behind = ")
    line = src[i:src.index("\n", i)]
    assert "historyBtn" not in line and "menuBtn" not in line, \
        f"nav-rail ids must not mark the board as present: {line}"
    assert "addNewEvent" in line


def test_an_open_reservation_survives_the_sheet_check():
    ok, relaunched = _dismiss_with([
        {"label": "AssignTableBtn", "id": ""},
        {"label": "closeEventModal", "id": ""},
        {"label": "historyBtn", "id": ""},      # the nav rail is always there
        {"label": "menuBtn", "id": ""},
    ])
    assert not relaunched, "the open reservation was relaunched away"
    assert ok


def test_a_failed_scroll_is_retried_not_latched():
    """The table sheet can render a beat AFTER the card search starts, so iteration 1
    scrolls a clean board that is then covered — achieving nothing. Latching on the
    ATTEMPT made that one wasted try the only try: the run reported "could not bring
    the 20:00 row into view" and fell back to the horizontal search, though 20:00 was
    reachable (measured y=518 on an 834pt screen). The give-up path is the deadline,
    not a single attempt."""
    i = _SRC.index("if not scrolled and hour_lbl")
    block = _SRC[i:i + 1600]
    assert "if scroll_to_hour(els_now):\n                        scrolled = True" in block, \
        "scrolled must latch only after a SUCCESSFUL scroll"
    assert "continue" in block


def test_a_transient_label_miss_is_retried():
    """The label came from an idb snapshot; the board re-renders on its own. Measured:
    a card idb had just listed reported "no element matched that exact label", and a
    query seconds later found sixteen."""
    src = _SRC[_SRC.index("def appium_click"):]
    head = src[:src.index("return \"no element matched")]
    assert head.count("find_elements") >= 2, "one retry must precede the failure"


# ── the table sheet closes itself when re-opened ────────────────────────────

def _sheet_open(els):
    fr = FlowRunner.__new__(FlowRunner)
    fr._idb_els = lambda *a, **k: els
    return fr._table_sheet_open()


def test_an_open_sheet_is_recognised_before_its_chips_load():
    """EventTableSelect gates its chips on tablesLoading, so an OPEN sheet has no
    chips for the first beat — indistinguishable, by chips alone, from a sheet that
    was never opened."""
    assert _sheet_open([{"label": "Select a table", "id": ""},
                        {"label": "AssignTableBtn", "id": ""}])


def test_the_modify_heading_needs_the_sheets_own_commit_control():
    """'modifyTable' is ALSO the id of the BUTTON that opens the sheet, so on its own
    it proves nothing — treating it as the heading would report the sheet open while
    sitting on the reservation."""
    assert not _sheet_open([{"label": "modifyTable", "id": ""},
                            {"label": "closeEventModal", "id": ""}])
    assert _sheet_open([{"label": "Modify Table", "id": ""},
                        {"label": "AssignTableBtn", "id": ""}])


def test_the_board_is_not_a_table_sheet():
    assert not _sheet_open([{"label": "addNewEvent", "id": ""},
                            {"label": "homeBtn", "id": ""}])


def test_an_already_open_sheet_is_waited_on_not_retapped():
    """The sheet is a Modal with onBackdropPress={handleTableClose}. Re-tapping
    'modifyTable' to "open" an already-open sheet hits the BACKDROP covering that
    control and dismisses the sheet — the reported "it clicks the event and then
    simply comes back", with I1/I2/O1/O2 visible on screen as it closed."""
    src = inspect.getsource(FlowRunner._assign_table)
    opener = src[src.index("def open_table_sheet"):]
    i = opener.index("_table_sheet_open()")
    j = opener.index('for ident in (')
    assert i < j, "the already-open check must precede any tap"
    assert "onBackdropPress" in opener or "backdrop" in opener.lower()
