"""The calculation tables of the run report, read from what each step wrote down.

The run's HTML report (scenario_report.py) shows, per segment, only the CALCULATIONS
it checked -- the user, 2026-11-04: "just calculation part you do" -- in the layout of
the sample pre-order / split / void reports: Calculated vs Displayed for a payment, the
amount a waiter's add put on the order and each person's share, a split's line before
and after, a comp's unit price, comp amount and the line it should leave, the
outstanding balance, the invoice sums. Steps that check nothing in euros (a quantity
change, a move, a void, the kitchen, send / serve) get no table: a segment of only those
is listed at the end of the report as passed.

Steps that check money themselves leave their table ready-made as a '[calc] {json}'
note (automation/scenarios/invoice_check.py). The others leave only their
'[ok] <step> — <result> (12.3s)' line, which already carries what they saw ('split …:
166.80 € → 83.40 € each', 'comped …; 69.50 € → 0.00 €'): those lines are read here into
the same {"t": title, "h": head, "r": rows} tables, so runs made before a step learnt
to write its own table get them too.

A row's last cell is True / False / None: checked and right, checked and wrong (drawn
red), or information. A line of a step read here that does not have the expected words
still gets a row (its words, as information), so nothing the run saw is dropped.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from automation.scenarios.invoice_check import MISMATCH_PREFIX, calc_of

Table = Dict[str, Any]

_OK = re.compile(r"^\[ok\] (?P<step>.+?) — (?P<text>.*?)(?: \((?P<secs>\d+(?:\.\d+)?)s\))?$")
_ITEM = re.compile(r"^(?P<name>.+?)(?:\*(?P<qty>\d+))?(?:\[(?P<opts>[^\]]*)\])?$")
_EURO = r"(\d+(?:[.,]\d{1,2})?)\s*€"


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def _eur(value: Optional[float]) -> str:
    return "—" if value is None else f"€{value:.2f}"


def _diff(value: float) -> str:
    return f"{round(value, 2) + 0.0:+.2f}"


def _item(spec: str) -> Tuple[str, int, str]:
    """'Pizza Margheritas*2[large, veg]' -> ('Pizza Margheritas', 2, 'large, veg')."""
    m = _ITEM.match(spec.strip())
    if not m:
        return spec.strip(), 1, ""
    return m.group("name").strip(), int(m.group("qty") or 1), (m.group("opts") or "").strip()


def _dish_at(spec: str) -> Tuple[str, str, str]:
    """'Pizza Salamees@Guest 5 > Guest 4' -> ('Pizza Salamees', 'Guest 5', 'Guest 4')."""
    left, _, right = spec.partition(">")
    dish, _, who = left.partition("@")
    return dish.strip(), re.sub(r"[*=].*$", "", who).strip(), right.strip()


# ── One reader per kind of step: (step, text) -> a row ─────────────────────────────
def _pay_stripe(step, text):
    m = re.search(r"Pay\s*€\s*(\d+(?:[.,]\d{1,2})?).*?\(bill €(\d+(?:[.,]\d{1,2})?)\)", text)
    if not m:
        return ["Pre-order payment", "—", "—", "—", text, None]
    shown, bill = _num(m.group(1)), _num(m.group(2))
    return ["Pre-order payment", _eur(bill), _eur(shown), _diff(shown - bill),
            "Calculated = the checkout's bill; Displayed = Stripe's Pay button",
            abs(shown - bill) < 0.006]


def _w_add(step, text):
    spec = step.split(":", 1)[1]
    left, _, right = spec.partition(">")
    name, qty, opts = _item(left)
    mode, _, who = right.partition(":")
    people = [p.strip() for p in who.split(",") if p.strip()]
    m = re.search(r"\+" + _EURO + r" on the order", text)
    if not m:
        return [name, qty, opts or "—", mode.strip().title(), ", ".join(people), "—", "—",
                text, None]
    added = _num(m.group(1))
    each = added / len(people) if people else None
    return [name, qty, opts or "—", mode.strip().title(), ", ".join(people), _eur(added),
            f"{_eur(each)} × {len(people)}" if each is not None else "—", "", added > 0]


def _w_split(step, text):
    dish, who, others = _dish_at(step.split(":", 1)[1])
    people = 1 + len([p for p in others.split(",") if p.strip()])
    m = re.search(_EURO + r" → " + _EURO + r" each", text)
    if not m:
        return [dish, who, others, "—", "—", "—", "—", text, None]
    before, each = _num(m.group(1)), _num(m.group(2))
    total = round(each * people, 2)
    return [dish, who, others, _eur(before), f"{_eur(each)} × {people}", _eur(total),
            _diff(total - before), "", abs(total - before) <= 0.01 * people + 0.001]


def _w_comp(step, text):
    """A comp of the WHOLE line ('*all:<pct>') is taken off in place: the line should
    read before × (1 − pct). A comp of k units moves those k units to a comped line of
    their own (MEASURED run 1107222a: Guest 4's 3 Pizza Margheritas 33.99 € → 22.66 €;
    Guest 1's Coca-Cola share 6.10 € → 4.88 €, 1 unit of 5): the unit price is what the
    line lost / k, the line must have held a whole number of such units, and the comped
    line should read k × unit × (1 − pct) -- the invoice prints it (the host's 'comps
    0.18' = 1.22 € × 15 %)."""
    spec = step.split(":", 1)[1]
    dish, who, _ = _dish_at(spec)
    m_spec = re.search(r"\*(all|\d+):(\d+(?:\.\d+)?)\s*$", spec)
    m = re.search(r"comped (\d+) unit\(s\) .*? at (\d+(?:\.\d+)?)%.*?; " + _EURO + r" → " + _EURO,
                  text)
    item = f"{dish} ({who})" if who else dish
    if not m:
        return [item, "—", "—", "—", "—", "—", "—", "—", "—", text, None]
    units, pct = int(m.group(1)), float(m.group(2))
    before, after = _num(m.group(3)), _num(m.group(4))
    rate = pct / 100
    if m_spec and m_spec.group(1) == "all":
        comp = round(before * rate, 2)
        expected = round(before - comp, 2)
        return [item, f"{units} (whole line)", f"{pct:g}%",
                _eur(before / units if units else None), _eur(comp), _eur(expected),
                _eur(after), _diff(after - expected), "— (comped in place)", "",
                abs(after - expected) < 0.011]
    unit = (before - after) / units if units else 0.0
    held = before / unit if unit > 0 else 0.0
    whole = held > 0 and abs(held - round(held)) < 0.02
    comp = round(units * unit * rate, 2)
    expected = round(before - units * unit, 2)
    return [item, f"{units} of {round(held) if whole else f'{held:.2f}'}", f"{pct:g}%",
            _eur(unit), _eur(comp), _eur(expected), _eur(after),
            _diff(after - expected), _eur(round(units * unit - comp, 2)),
            "" if whole else "the line did not lose a whole number of units", whole]


def _close_table(step, text):
    return ["Outstanding balance", "€0.00", text, True if "fully paid" in text else None]


# step prefix -> (table key, title, head, reader). The key groups a segment's rows of
# one kind into one table, placed where the first of them was.
_KINDS: List[Tuple[str, str, str, List[str], Any]] = [
    ("@pay_stripe", "pay_stripe", "Pre-payment Total",
     ["Check", "Calculated", "Displayed", "Diff", "How"], _pay_stripe),
    ("@w_add:", "w_add", "Added by the waiter — amount and each person's share",
     ["Item", "Qty", "Options", "Assign / Split", "To", "Added to the order", "Each person",
      "Note"], _w_add),
    ("@w_split:", "w_split", "Split — Grand Total of the line before and after",
     ["Item", "From", "With", "Before split", "Each after split", "Sum after split", "Diff",
      "Note"], _w_split),
    ("@w_comp:", "w_comp", "Comps",
     # No 'Line before' column (the user, 2026-11-04): the row's price before the comp
     # is still what the expected price after is worked out from.
     ["Item (guest)", "Units comped", "Comp", "Unit price", "Comp amount",
      "Expected line after", "Displayed line after", "Diff", "Comped line should read",
      "Note"], _w_comp),
    ("@close_table", "close", "Outstanding Balance Check",
     ["Check", "Expected", "Found"], _close_table),
]


def _kind_of(step: str):
    for prefix, key, title, head, reader in _KINDS:
        if step.startswith(prefix):
            return key, title, head, reader
    return None


def _verdict(notes: List[str]) -> Optional[Table]:
    """'@invoice_verdict' at the end of the flow: every sum of the run that did not
    match, red, or one green row when they all did."""
    if not any(n.startswith("[ok] @invoice_verdict") for n in notes):
        return None
    rows: List[list] = []
    for n in notes:
        if n.startswith(MISMATCH_PREFIX + "@invoice_verdict"):
            where, _, what = n[len(MISMATCH_PREFIX + "@invoice_verdict — "):].partition(" — ")
            rows.append([len(rows) + 1, where, what, False])
    if not rows:
        rows.append(["—", "every invoice", "every sum matched", True])
    return {"t": "Calculation checks of the whole run", "h": ["#", "Where", "What"], "r": rows}


def _comp_summary(comps: Table) -> Optional[Table]:
    """'Comps — Final Summary' (the void sample's 'Void Products — Final Summary')."""
    head = comps["h"]
    if "Comp amount" not in head:
        return None
    item, at = head.index("Item (guest)"), head.index("Comp amount")
    rows, total = [], 0.0
    for r in comps["r"]:
        if isinstance(r[at], str) and r[at].startswith("€"):
            total += float(r[at][1:])
            rows.append([len(rows) + 1, r[item], r[at], None])
    if len(rows) < 2:
        return None
    rows.append(["", "Total comped", _eur(total), None])
    return {"t": "Comps — Final Summary", "h": ["#", "Item (guest)", "Comp amount"], "r": rows}


def tables_of(notes: List[str]) -> List[Table]:
    """The calculation tables of one segment, in the order its steps ran: the '[calc]'
    tables the steps wrote, and one table per kind of step read from the '[ok]' lines.
    A payment step that wrote its own table is not read again."""
    notes = [str(n) for n in (notes or [])]
    placed: List[Tuple[float, Table]] = []
    grouped: Dict[str, Table] = {}
    own = [calc_of(n) for n in notes]
    own_titles = [t.get("t", "") for t in own if t]
    for i, n in enumerate(notes):
        if own[i]:
            placed.append((i, own[i]))
            continue
        m = _OK.match(n)
        if not m:
            continue
        step, text = m.group("step"), m.group("text")
        if step.startswith("@invoice_verdict"):
            v = _verdict(notes)
            if v:
                placed.append((i, v))
            continue
        kind = _kind_of(step)
        if kind is None:
            continue
        key, title, head, reader = kind
        if key == "pay_stripe" and any(t.startswith("Pre-order payment") for t in own_titles):
            continue
        try:
            row = reader(step, text)
        except Exception:                    # an unexpected wording: keep its words
            row = [step, text] + [""] * (len(head) - 2) + [None]
        if key not in grouped:
            grouped[key] = {"t": title, "h": head, "r": []}
            placed.append((i, grouped[key]))
        grouped[key]["r"].append(row)
    for t in grouped.values():
        # A column no row filled ('Note' when every line read cleanly) is left out.
        width = len(t["h"])
        keep = [c for c in range(width)
                if any(len(r) > width and r[c] not in ("", None) for r in t["r"])]
        if len(keep) < width:
            t["h"] = [t["h"][c] for c in keep]
            t["r"] = [[r[c] for c in keep] + r[width:] for r in t["r"]]
        if len(t["r"]) > 1:                   # number the rows of a multi-row table
            t["h"] = ["#"] + t["h"]
            t["r"] = [[k] + r for k, r in enumerate(t["r"], 1)]
    if "w_comp" in grouped:
        at = next(i for i, t in placed if t is grouped["w_comp"])
        summary = _comp_summary(grouped["w_comp"])
        if summary:
            placed.append((at + 0.5, summary))
    return [t for _, t in sorted(placed, key=lambda p: p[0])]
