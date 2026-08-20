"""Guard: every serve/settle segment must NAVIGATE to the order before acting on it.

Segments that serve run after the kitchen segment, which shares the single Business iPad
and so logs the waiter out and back in — landing on the bookings board, not the order
screen. A serve segment that opens with 'click selectAllItemsBtn' therefore hunts a button
that isn't on screen and burns the full 150s step timeout before failing.

Run: PYTHONPATH=. .venv/bin/python automation/scenarios/test_serve_navigation.py
"""
from automation.scenarios.cross_app_flows import FLOWS, _W_SERVE_NOTIFY

ACT_ON_ORDER = ("selectAllItemsBtn", "serveItemsBtn", "notifyPaymentBtn", "closeTableBtn")
NAVIGATES = ("@open_order", "@open_reservation")


def test_serve_list_navigates_first():
    assert _W_SERVE_NOTIFY[0] in NAVIGATES, (
        f"_W_SERVE_NOTIFY must open the order first, starts with {_W_SERVE_NOTIFY[0]!r}")


def test_no_segment_touches_an_order_after_a_role_switch():
    """The real invariant, across every flow — not just the list we happened to fix.

    Waiter and kitchen SHARE one Business device, so a waiter->kitchen->waiter hand-off
    logs out and back in and resets the screen; a consumer segment in between runs on the
    other device and leaves the Business screen untouched. So a segment only has to
    re-navigate when the previous BUSINESS-role segment had a different role.
    """
    bad = []
    for flow in FLOWS.values():
        prev_biz_role = None
        for seg in flow["segments"]:
            if seg["role"] == "consumer":
                continue                                  # other device — screen survives
            switched = prev_biz_role is not None and seg["role"] != prev_biz_role
            opened = False
            for step in seg["steps"]:
                if step in NAVIGATES:
                    opened = True
                elif any(b in step for b in ACT_ON_ORDER) and not opened and switched:
                    bad.append(f"{flow['id']} seg{seg['num']} ({seg['role']}, after "
                               f"{prev_biz_role}): {step!r} with no @open_* first")
                    break
            prev_biz_role = seg["role"]
    assert not bad, "segments acting on an order the role switch just closed:\n  " + "\n  ".join(bad)


def test_kitchen_ready_only_passes_when_it_actually_marked_ready():
    """Closing a leftover prepared ticket must NOT be enough to pass — that let a run go
    green having never marked anything Ready, the one thing the step is named after."""
    from automation.scenarios.cross_app_flows import FlowRunner

    class FakeEl:
        def click(self): pass

    class FakeDriver:
        def __init__(self, present): self.present = present
        def find_elements(self, _by, value):
            return [FakeEl()] if value in self.present else []

    class FakeRunner:
        def __init__(self, present): self.d = FakeDriver(present)

    def ready_result(present):
        notes = []
        runner = FlowRunner.__new__(FlowRunner)          # no __init__: no devices needed
        return FlowRunner._kitchen_ready(runner, FakeRunner(present), notes), notes

    ok, _ = ready_result({"orderReadyBtn", "orderCloseBtn"})
    assert ok, "readied (and closed) must pass"

    ok, _ = ready_result({"orderReadyBtn"})
    assert ok, "readied alone must pass"

    ok, notes = ready_result({"orderCloseBtn"})
    assert not ok, f"close-only must FAIL, it never marked anything ready; notes={notes}"

    ok, notes = ready_result(set())
    assert not ok, f"empty queue must FAIL; notes={notes}"




def test_multiword_click_reaches_the_fast_path():
    """'click order later' must resolve to the id `orderLater`.

    Written with spaces it missed the single-word regex in _smart_click, skipped the
    idb allow-list, and fell to the fuzzy resolver — which hangs the full 150s on the
    Booking Confirmed modal even though ORDER LATER is plainly on screen."""
    import re
    from automation.scenarios.cross_app_flows import _IDB_CLICK_IDS

    def ident_for(step):
        m = re.match(r'^\s*click\s+([A-Za-z][\w]*)\s*$', step)
        if m:
            return m.group(1)
        mw = re.match(r'^\s*click\s+([A-Za-z][A-Za-z0-9 ]*?)\s*$', step)
        if mw:
            parts = mw.group(1).split()
            if len(parts) > 1:
                return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
        return None

    assert ident_for("click order later") == "orderLater"
    assert ident_for("click pre order booking") == "preOrderBooking"
    assert ident_for("click orderLater") == "orderLater"          # already exact
    assert ident_for("click NylaiKitchen2") == "NylaiKitchen2"    # single word untouched
    for s in ("click order later", "click pre order booking"):
        assert ident_for(s) in _IDB_CLICK_IDS, f"{s} must reach the idb fast path"


if __name__ == "__main__":
    test_serve_list_navigates_first()
    test_no_segment_touches_an_order_after_a_role_switch()
    test_kitchen_ready_only_passes_when_it_actually_marked_ready()
    test_multiword_click_reaches_the_fast_path()
    print("ok — navigation, kitchen-ready assertion, and multi-word click ids")
