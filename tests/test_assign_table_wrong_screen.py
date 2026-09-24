r"""The @assign_table legacy path must not tap on a screen that has no table sheet.

Regression: the "reservation is no longer open" guard used `break`, which fell
through to a legacy scan for r'[IOio]\d{1,2}'. On the order-summary screen that
pattern matches 'I1', so the run tapped a random element and then reported
"opened the table modal but found no Apply/Confirm" — sending every past
investigation after the table sheet's testIDs when no sheet was ever up.
"""
import re

# The order-summary screen that actually replaced the reservation (measured,
# from the failing run's own note: "↳ on screen: [...]").
ORDER_SUMMARY = ["modifyTable", "homeBtn", "historyBtn", "monticard", "Pre-Order",
                 "TagliatellealSalmonecard", "menuBtn", "addItemsBtn", "Total", "I1"]

LEGACY_TABLE_RE = re.compile(r"[IOio]\d{1,2}")


def test_legacy_pattern_matches_on_the_wrong_screen():
    """Why the gate is required: the pattern is not self-limiting."""
    hits = [l for l in ORDER_SUMMARY if LEGACY_TABLE_RE.fullmatch(l.strip())]
    assert hits == ["I1"], (
        "the legacy table pattern matches 'I1' on the order summary — an "
        "ungated scan taps it and misreports the failure")


def test_gate_blocks_the_tap_when_no_sheet_is_up():
    """_table_modal_up() is the gate; it is text-based, so it works even when
    the sheet flattens and exposes no testIDs."""
    def table_modal_in(labels):
        return any("select a table" in l.lower() for l in labels)

    assert not table_modal_in(ORDER_SUMMARY), \
        "no sheet on the order summary -> legacy scan must be skipped"
    assert table_modal_in(["Select A Table I1 I2 O1 O2", "homeBtn"]), \
        "a real (flattened) sheet must still be detected"


if __name__ == "__main__":
    test_legacy_pattern_matches_on_the_wrong_screen()
    test_gate_blocks_the_tap_when_no_sheet_is_up()
    print("ok")
