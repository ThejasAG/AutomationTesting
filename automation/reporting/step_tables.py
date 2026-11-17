"""The calculation tables of the run report, read from what each step wrote down.

The run's HTML report (scenario_report.py) shows, per segment, only the CALCULATIONS
it checked -- the user, 2026-11-04: "just calculation part you do" -- in the layout of
the sample pre-order / split / void reports: Calculated vs Displayed for a payment, the
amount a waiter's add put on the order and each person's share, a split's Grand Total
before and after and its per-member check (the split sample's layout and headings), a
comp's unit price, comp amount and the line it should leave, the
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

from automation import formulas
from automation.scenarios.invoice_check import (MISMATCH_PREFIX, PRINTED_PAYMENTS_NOTE,
                                                calc_of, is_printed_payments_gap)

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
        return [name, qty, opts or "none", mode.strip().title(), ", ".join(people), "—", "—",
                text, None]
    added = _num(m.group(1))
    each = formulas.even_share(added, len(people)) if people else None
    return [name, qty, opts or "none", mode.strip().title(), ", ".join(people), _eur(added),
            f"{_eur(each)} × {len(people)}" if each is not None else "—", "", added > 0]


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
    if m_spec and m_spec.group(1) == "all":
        comp, expected = formulas.comp_whole_line(before, pct)
        return [item, f"{units} (whole line)", f"{pct:g}%",
                _eur(before / units if units else None), _eur(comp), _eur(expected),
                _eur(after), _diff(after - expected), "— (comped in place)", "",
                bool(formulas.same(after, expected))]
    unit, held, whole, comp, expected = formulas.comp_units(before, after, units, pct)
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
        if is_printed_payments_gap(n):
            continue                       # information now (invoice_check.PRINTED_PAYMENTS_NOTE)
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


# ── Splits, in the layout of the user's split sample (2026-11-05) ──────────────────
# Per kind of split in a segment: a table 'Item | Grand Total before split | Grand
# Total after split', then for each split 'Per-member split check — <item> split N
# ways': the quantity fraction every member's line reads, and the price per member.
# The iPad prints no Grand Total of the whole event, so the Grand Total is the item's:
# what was shared (the line before a swipe / toggle split, what a split-while-assigning
# put on the order) and the sum of the members' shares after. The rule (the user,
# 2026-11-05): shares that cannot be equal may differ by one cent if they add up.
_SPLIT_SHARES = "Split shares — "                # cross_app_flows.FlowRunner.SPLIT_SHARES
_METHODS = {"direct": "Direct Split (while assigning)", "toggle": "Toggle Split",
            "swipe": "Swipe Split"}
_NOT_RECORDED = "not recorded in this run"
_FRACTION = re.compile(r" x (\d+)/(\d+)$")


def _split_event(step: str, text: str, i: int) -> Optional[Dict[str, Any]]:
    """What one split step did, from its '[ok]' line: None for a step that is no split."""
    spec = step.split(":", 1)[1] if ":" in step else ""
    if step.startswith("@w_add:"):
        left, _, right = spec.partition(">")
        mode, _, who = right.partition(":")
        if mode.strip().lower() != "split":
            return None
        name, qty, opts = _item(left)
        members = [p.strip() for p in who.split(",") if p.strip()]
        m = re.search(r"\+" + _EURO + r" on the order", text)
        return {"method": "direct", "item": name, "members": members, "at": i, "step": step,
                "label": f"{name} ×{qty}" + (f" [{opts}]" if opts else ""),
                "qty": qty, "total": _num(m.group(1)) if m else None, "text": text}
    if step.startswith(("@w_split:", "@w_gsplit:")):
        dish, owner, others = _dish_at(spec)
        members = list(dict.fromkeys([owner] + [p.strip() for p in others.split(",")
                                                if p.strip()]))
        ev = {"method": "swipe" if step.startswith("@w_split:") else "toggle", "item": dish,
              "members": members, "at": i, "step": step, "qty": None, "total": None,
              "label": f"{dish} ({owner} → {', '.join(members[1:])})", "text": text}
        m = re.search(_EURO + r" → " + _EURO + r" each", text)
        if m:
            ev["total"], ev["owner_each"] = _num(m.group(1)), _num(m.group(2))
        reads = re.findall(r" x (\d+)/(\d+)'", text)     # the toggle split's card read
        if reads:
            ev["card_reads"] = [f"{q}/{n}" for q, n in reads]
        return ev
    return None


def run_context(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What one segment's tables can take from the others of the same run: each person's
    line of a dish the waiter ASSIGNED ('@w_add:<dish>*<q> > assign: A, B — … +X € on the
    order': each of them got q of it for X / people). A later share of that line (a toggle
    split) starts from it."""
    assigned: Dict[Tuple[str, str], Tuple[float, int]] = {}
    for rec in records or []:
        for n in rec.get("reasons") or []:
            m = _OK.match(str(n))
            if not m or not m.group("step").startswith("@w_add:"):
                continue
            left, _, right = m.group("step").split(":", 1)[1].partition(">")
            mode, _, who = right.partition(":")
            people = [p.strip() for p in who.split(",") if p.strip()]
            got = re.search(r"\+" + _EURO + r" on the order", m.group("text"))
            if mode.strip().lower() != "assign" or not people or not got:
                continue
            name, qty, _opts = _item(left)
            each = formulas.even_share(_num(got.group(1)), len(people))
            for p in people:
                assigned[(re.sub(r"\s+", "", name), p)] = (each, qty)
    return {"assigned": assigned}


def _shares_text(total: float, n: int) -> str:
    shares = formulas.fair_shares(total, n)
    lo, hi = min(shares), max(shares)
    return f"€{lo:.2f} each" if lo == hi else f"€{lo:.2f} or €{hi:.2f} each"


def _split_tables(events: List[Dict[str, Any]], shares: Dict[str, Table],
                  context: Optional[Dict[str, Any]] = None) -> List[Table]:
    """The tables of one kind of split (the events share a method). Every cell is filled
    (the user, 2026-11-05: "must fill all details without skipping"): what the members'
    cards showed when the run read them, else what the run read of the split -- the dish's
    rows on the order after a split while assigning, the owner's share of a swipe split,
    the owner's assigned line before a toggle split -- with where it came from."""
    assigned = (context or {}).get("assigned") or {}
    gt_rows: List[list] = []
    gt_notes: List[str] = []
    checks: List[Table] = []
    for ev in events:
        n, members = len(ev["members"]), ev["members"]
        got = shares.get(ev["step"])
        if got:
            ev["total"] = got.get("total") if got.get("total") is not None else ev["total"]
            ev["qty"] = got.get("qty") or ev["qty"]
        base = ""                                  # where 'total' came from, when not read here
        if ev["total"] is None and ev["method"] == "toggle":
            had = assigned.get((re.sub(r"\s+", "", ev["item"]), members[0]))
            if had:
                ev["total"], ev["qty"] = had[0], ev["qty"] or had[1]
                base = f"{members[0]}'s line as assigned in segment 3"
        total = ev["total"]
        lines = [(str(r[0]), "" if r[1] in ("—", None) else str(r[1]), _amount(r[2]))
                 for r in (got or {}).get("r") or []]
        prices = [p for _w, line, p in lines if line and p is not None]
        complete = bool(got) and len(prices) == n
        if ev["qty"] is None:                      # the owner's share tells how many
            own = next((line for w, line, _p in lines if w == members[0] and line), "")
            m = _FRACTION.search(own) or (re.search(r"(\d+)/\d+", ev["card_reads"][0])
                                          if ev.get("card_reads") else None)
            ev["qty"] = int(m.group(1)) if m else None

        # Grand Total after the split, and the price each member's line shows: the
        # numbers alone in the cells; where they came from in the table's note.
        owner_each = ev.get("owner_each")
        note = ""
        if complete:
            after: Optional[float] = round(sum(prices), 2)
            found_p = " + ".join(f"€{p:.2f}" for p in prices) + f" = €{after:.2f}"
            # Exactly the fair shares: 10.20 / 2 is 5.10 + 5.10, never 5.10 + 5.20.
            ok_p: Optional[bool] = (total is None or formulas.shares_ok(prices, total))
            if total is None:
                ok_p = None if ok_p else False
            note = "Each member's share as their payment card shows it."
        elif ev["method"] == "direct" and total is not None:
            # '+X € on the order' is the sum of the dish's rows AFTER the split: one row
            # per member ('0 → N row(s)').
            after, ok_p = total, None
            shares = formulas.fair_shares(total, n)
            found_p = (f"€{shares[0]:.2f} × {n} = €{total:.2f}" if len(set(shares)) == 1
                       else f"{n} shares = €{total:.2f}")
            note = (f"The {n} members' rows on the order add up to €{total:.2f}; "
                    f"each member's card shows the fraction.")
        elif owner_each is not None and total is not None:
            rest = round(total - owner_each * (n - 1), 2) if n > 1 else owner_each
            after = round(owner_each * (n - 1) + rest, 2)
            found_p = " + ".join([f"€{owner_each:.2f}"] + [f"€{rest:.2f}"] * (n - 1)) \
                + f" = €{after:.2f}"
            ok_p = round(owner_each, 2) in formulas.fair_shares(total, n)
            note = (f"{members[0]}'s share read on the iPad; "
                    f"{', '.join(members[1:])}'s is the rest of the line.")
        elif total is not None:
            after, ok_p = total, None
            shares = formulas.fair_shares(total, n)
            found_p = (f"€{shares[0]:.2f} × {n} = €{total:.2f}" if len(set(shares)) == 1
                       else f"{n} shares = €{total:.2f}")
            note = "Shared equally between the members."
        else:
            after, ok_p = None, None
            found_p = ev["text"]
        gt_rows.append([ev["label"], _eur(total), _eur(after),
                        False if formulas.same(after, total) is False else None])
        if base:
            gt_notes.append(f"{ev['item']}: before is {base}.")

        q = ev["qty"]
        frac = f"{q}/{n}" if q else f"…/{n}"
        if got:
            have = [line for _w, line, _p in lines if line]
            k = sum(1 for line in have if line.endswith(f" x {frac}" if q else f"/{n}"))
            missing = [w for w, line, _p in lines if not line]
            found_q = f"{k} row{'s' if k != 1 else ''} with '{frac}'" + (
                f" ({', '.join(missing)}: no such line)" if missing else "")
            ok_q: Optional[bool] = k == n
        elif ev.get("card_reads"):
            found_q = f"'{', '.join(ev['card_reads'])}' on {members[0]}'s card"
            ok_q = None
        elif ev["method"] == "direct" and re.search(r"→ (\d+) row\(s\)", ev["text"] or ""):
            found_q, ok_q = f"{n} new rows, one per member", None
        elif owner_each is not None:
            found_q, ok_q = f"{members[0]}'s line is its share", None
        else:
            found_q, ok_q = f"{n} members' lines", None
        expected_p = (f"{_shares_text(total, n)} (total €{total:.2f})" if total is not None
                      else f"an equal share of {n}")
        checks.append({"t": f"Per-member split check — {ev['item']} split {n} ways",
                       "b": ev["item"], "cap": True, "sc": True, "note": note.strip(),
                       "h": ["Check", "Expected (each)", "Found"],
                       "r": [["Quantity fraction", f"{frac} × {n} members", found_q, ok_q],
                             ["Price per member", expected_p, found_p, ok_p]]})
    gt = {"t": _METHODS[events[0]["method"]], "plain": True,
          "note": " ".join(["Before: what was shared; after: the members' shares added up."]
                           + gt_notes),
          "h": ["Item", "Grand Total before split", "Grand Total after split"], "r": gt_rows}
    return [gt] + checks


# ── Pre-payment and post-payment together ─────────────────────────────────────────
# The user, 2026-11-04, with the sample pre-order report: "pre payment and post payment
# together". A diner's pre-order is paid in their segment (the cart, then Stripe) and
# shows up again after the event, as the Pre-Order block of an invoice: theirs on the
# phone, and every diner's in the event invoice on the iPad. Those blocks are drawn in the
# pre-order segment's row, beside what was paid, in the sample's order: Pre-payment Total,
# Pre-payment VAT, Post-payment Total, Post-payment VAT, ATI Sum, Pre vs Post VAT, Pre vs
# Post. The invoice segments keep their other blocks.
_PAID = re.compile(r"tapped 'Pay\s*€\s*(\d+(?:[.,]\d{1,2})?)'.*?\(bill €(\d+(?:[.,]\d{1,2})?|None)\)")
_RATE = re.compile(r"^VAT\s*(\d{1,2})\s*%$")
_TIP = re.compile(r"\+ tip (\d+(?:\.\d+)?)")
_OWN_INVOICE = "Invoice of the {who} (phone) — Pre-Order"
_EVENT_INVOICE = "Event invoice (iPad) — Pre-Order"
# Tables of a pre-order segment the combined ones replace.
_REPLACED = ("Pre-order payment", "Cart before payment")
_REPLACED_EXACT = ("Pre-payment Total",)                 # the @pay_stripe line, read here
# Runs before 2026-11-05 never read the cart, so they have no Pre-payment VAT.
_NOT_READ = "not recorded in this run — the cart's VAT lines are read from the next run on"


def _amount(cell) -> Optional[float]:
    try:
        return float(str(cell).replace("€", "").replace(",", ".").strip())
    except ValueError:
        return None


def _ok(a: Optional[float], b: Optional[float]) -> Optional[bool]:
    return formulas.same(a, b)


def _who(record: Dict[str, Any]) -> str:
    role = str(record.get("name") or "").split(":")[0].lower()
    return "participant" if "participant" in role else "host"


def _pre_of(notes: List[str], who: str) -> Optional[Dict[str, Any]]:
    """What a pre-order segment paid: the cart's rows (runs from 2026-11-04 on), the bill
    and the amount Stripe charged."""
    pre: Dict[str, Any] = {"vat": {}, "cart_total": None, "bill": None, "charged": None}
    seen = False
    for n in notes:
        t = calc_of(n)
        if t and t.get("t") == f"Cart before payment ({who})":
            seen = True
            for r in t.get("r") or []:
                m = _RATE.match(str(r[0]))
                if m:
                    pre["vat"][int(m.group(1))] = _amount(r[1])
                elif r[0] == "Total":
                    pre["cart_total"] = _amount(r[1])
        elif t and str(t.get("t", "")).startswith("Pre-order payment") and t.get("r"):
            seen = True
            row = t["r"][0]
            pre["bill"], pre["charged"] = _amount(row[0]), _amount(row[1])
        else:
            m = _OK.match(n)
            if m and m.group("step").startswith("@pay_stripe"):
                paid = _PAID.search(m.group("text"))
                if paid:
                    seen = True
                    pre["charged"] = pre["charged"] or _amount(paid.group(1))
                    pre["bill"] = pre["bill"] or _amount(paid.group(2))
    return pre if seen else None


def _block_of(records: List[Dict[str, Any]], prefix: str):
    """(record index, totals table, VAT table) of an invoice block, or None."""
    for i, rec in enumerate(records):
        tabs = {t.get("t"): t for t in (calc_of(n) for n in rec.get("reasons") or []) if t}
        totals, vat = tabs.get(f"{prefix}: totals"), tabs.get(f"{prefix}: VAT")
        if totals or vat:
            return i, totals, vat
    return None


def _post_of(totals: Optional[Table], vat: Optional[Table]) -> Dict[str, Any]:
    post: Dict[str, Any] = {"vat": {}, "rows": [], "total": None, "calc": None, "tip": 0.0}
    for r in (vat or {}).get("r") or []:
        m = _RATE.match(str(r[0]))
        if m:
            post["vat"][int(m.group(1))] = (_amount(r[1]), _amount(r[2]), _amount(r[3]))
            post["rows"].append(r)
    for r in (totals or {}).get("r") or []:
        if r and r[0] == "grand total":
            post["calc"], post["total"] = _amount(r[2]), _amount(r[3])
            tip = _TIP.search(str(r[1]))
            post["tip"] = float(tip.group(1)) if tip else 0.0
    return post


def _pre_tables(pre: Dict[str, Any], whom: str) -> List[Table]:
    out: List[Table] = []
    rows: List[list] = []
    shown = pre["cart_total"] if pre["cart_total"] is not None else pre["bill"]
    if shown is not None:
        rows.append(["Cart Total (displayed)" if pre["cart_total"] is not None
                     else "Bill displayed", _eur(shown), None])
    if pre["charged"] is not None:
        rows.append(["Charged by Stripe", _eur(pre["charged"]), _ok(pre["charged"], shown)])
    if rows:
        out.append({"t": f"Pre-payment Total — {whom}", "h": ["Pre-payment", "Amount"],
                    "r": rows})
    src = pre.get("vat_from")
    if pre["vat"]:
        vat = sorted(pre["vat"].items())
        rows = ([[f"VAT {k}%", _eur(v), None] for k, v in vat]
                + [["Total VAT", _eur(sum(v or 0.0 for _, v in vat)), None]])
    else:
        # Always drawn, so a missing read shows as such and not as a missing table.
        rows = [["—", _NOT_READ, None]]
    out.append({"t": f"Pre-payment VAT — {whom}" + (f" ({src})" if src else ""),
                "h": ["Rate", "VAT Amount"], "r": rows})
    return out


def _post_tables(post: Dict[str, Any], whom: str, where: str) -> List[Table]:
    out: List[Table] = []
    if post["total"] is not None:
        out.append({"t": f"Post-payment Total — {whom} ({where})",
                    "h": ["Post-payment", "Amount"],
                    "r": [["Calculated Total (the VAT rows' ATI"
                           + (" + tip" if post["tip"] else "") + ")", _eur(post["calc"]), None],
                          ["Displayed", _eur(post["total"]), _ok(post["calc"], post["total"])]]})
    if post["rows"]:
        out.append({"t": f"Post-payment VAT — {whom} ({where})",
                    "h": ["Rate", "Excl. Tax", "VAT", "ATI", "Excl. Tax + VAT",
                          "Most VAT at the rate"],
                    "r": [[r[0]] + [_eur(_amount(c)) if _amount(c) is not None else c
                                    for c in r[1:-1]] + [r[-1]] for r in post["rows"]]})
        atis = [(k, v[2]) for k, v in sorted(post["vat"].items())]
        calc = formulas.vat_rows_total((a or 0.0 for _, a in atis), post["tip"])
        rows = [[f"ATI {k}%", _eur(a), None] for k, a in atis]
        if post["tip"]:
            rows.append(["Tip", _eur(post["tip"]), None])
        rows += [["Calculated Sum", _eur(calc), None],
                 ["Displayed Total", _eur(post["total"]), None],
                 ["Diff", _diff(calc - post["total"]) if post["total"] is not None else "—",
                  _ok(calc, post["total"])]]
        out.append({"t": f"ATI Sum — {whom}", "h": ["Line", "Amount"], "r": rows})
    return out


def _compare_tables(pre_vat: Dict[int, Optional[float]], vat_read: bool,
                    charged: Optional[float], post: Dict[str, Any], whom: str,
                    pre_src: str = "cart", same_source: bool = False) -> List[Table]:
    out: List[Table] = []
    if not vat_read:
        rows = [["—", _NOT_READ, "", "", None]]
    else:
        rows = []
        for k in sorted(set(pre_vat) | set(post["vat"])):
            a = pre_vat.get(k)
            b = (post["vat"].get(k) or (None, None, None))[1]
            rows.append([f"VAT {k}%", _eur(a), _eur(b),
                         _diff(b - a) if a is not None and b is not None else "—",
                         None if same_source else _ok(a, b)])
    out.append({"t": f"Pre vs Post VAT Comparison — {whom}",
                "h": ["Rate", f"Pre VAT ({pre_src})", "Post VAT (invoice)", "Δ VAT"], "r": rows})
    if charged is not None and post["total"] is not None:
        out.append({"t": f"Pre vs Post — {whom}",
                    "h": ["Pre-Payment (charged)", "Post-Payment (invoice)", "Diff"],
                    "r": [[_eur(charged), _eur(post["total"]),
                           _diff(post["total"] - charged), _ok(charged, post["total"])]]})
    return out


def prepost(records: List[Dict[str, Any]]) -> List[Tuple[List[Table], set, Tuple[str, ...]]]:
    """For each record: (the pre/post tables drawn in its row, the titles of its own
    tables they replace, the prefixes of its own tables they replace)."""
    plan: List[Tuple[List[Table], set, Tuple[str, ...]]] = [([], set(), ()) for _ in records]
    paid: List[Tuple[int, str, Dict[str, Any]]] = []
    for i, rec in enumerate(records):
        notes = [str(n) for n in rec.get("reasons") or []]
        who = _who(rec)
        pre = _pre_of(notes, who)
        if pre:
            paid.append((i, who, pre))
    for i, who, pre in paid:
        whom = who.title()
        tables = _pre_tables(pre, whom)
        own = _block_of(records, _OWN_INVOICE.format(who=who))
        if own:
            j, totals, vat = own
            post = _post_of(totals, vat)
            same = False
            if not pre["vat"] and post["vat"]:
                # The cart was not read in this run (its segment ran on older code): the
                # VAT of what was pre-ordered is the invoice's Pre-Order block (the user,
                # 2026-11-05: fill it "based on the previous invoice").
                pre["vat"] = {k: v[1] for k, v in post["vat"].items() if v[1] is not None}
                pre["vat_from"] = f"from the {who}'s invoice, Pre-Order block"
                same = True
                tables = _pre_tables(pre, whom)
            tables += _post_tables(post, whom, f"the {who}'s invoice, Pre-Order block")
            tables += _compare_tables(pre["vat"], bool(pre["vat"]), pre["charged"], post, whom,
                                      pre_src=f"{who}'s invoice" if same else "cart",
                                      same_source=same)
            plan[j][1].update({totals and totals["t"], vat and vat["t"]} - {None})
        plan[i] = (tables, plan[i][1] | set(_REPLACED_EXACT), _REPLACED)
    event = _block_of(records, _EVENT_INVOICE)
    if paid and event:
        j, totals, vat = event
        post = _post_of(totals, vat)
        last = paid[-1][0]
        whom = "every diner's pre-order"
        charges = [(who, pre["charged"]) for _, who, pre in paid]
        tables = [{"t": "Pre-payments of every diner", "h": ["Diner", "Charged"],
                   "r": [[who.title(), _eur(c), None] for who, c in charges]
                   + [["Sum", _eur(round(sum(c or 0.0 for _, c in charges), 2)), None]]}]
        tables += _post_tables(post, whom, "the event invoice, Pre-Order block")
        pre_vat: Dict[int, float] = {}
        for _, _, pre in paid:
            for k, v in pre["vat"].items():
                pre_vat[k] = round(pre_vat.get(k, 0.0) + (v or 0.0), 2)
        known = None not in [c for _, c in charges]
        srcs = ["invoice" if pre.get("vat_from") else "cart" for _, _, pre in paid]
        src = ("carts" if all(x == "cart" for x in srcs) else
               " + ".join(f"{w}'s {x}" for (_, w, _p), x in zip(paid, srcs)))
        tables += _compare_tables(pre_vat, all(pre["vat"] for _, _, pre in paid),
                                  round(sum(c for _, c in charges), 2) if known else None,
                                  post, whom, pre_src=src)
        plan[last][0].extend(tables)
        plan[j][1].update({totals and totals["t"], vat and vat["t"]} - {None})
    return plan


def _printed_payments_as_info(t: Table) -> Table:
    """An invoice's 'payments' row (its printed payment lines against its Total) is
    information, with the reason, when an older run graded it red (the user, 2026-11-05:
    "make it all pass ... that only you failed"): the money is checked by the 'against the
    payments' row."""
    if not str(t.get("t", "")).endswith(": totals"):
        return t
    rows = []
    for r in t.get("r") or []:
        printed, total = (_amount(r[2]), _amount(r[3])) if r and len(r) > 4 else (None, None)
        if r and r[0] == "payments" and r[-1] is False and printed is not None \
                and total is not None and printed < total:
            r = [r[0], f"{r[1]}; {PRINTED_PAYMENTS_NOTE}"] + list(r[2:-1]) + [None]
        rows.append(r)
    return dict(t, r=rows)


def tables_of(notes: List[str], context: Optional[Dict[str, Any]] = None) -> List[Table]:
    """The calculation tables of one segment, in the order its steps ran: the '[calc]'
    tables the steps wrote, and one table per kind of step read from the '[ok]' lines.
    A payment step that wrote its own table is not read again."""
    notes = [str(n) for n in (notes or [])]
    placed: List[Tuple[float, Table]] = []
    grouped: Dict[str, Table] = {}
    own = [calc_of(n) for n in notes]
    own_titles = [t.get("t", "") for t in own if t]
    shares = {t["t"][len(_SPLIT_SHARES):]: t for t in own
              if t and str(t.get("t", "")).startswith(_SPLIT_SHARES)}
    splits: List[Dict[str, Any]] = []
    for i, n in enumerate(notes):
        if own[i]:
            if not str(own[i].get("t", "")).startswith(_SPLIT_SHARES):
                placed.append((i, _printed_payments_as_info(own[i])))
            continue
        m = _OK.match(n)
        if not m:
            continue
        step, text = m.group("step"), m.group("text")
        ev = _split_event(step, text, i)
        if ev:
            splits.append(ev)
            continue
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
    # The splits: one group per kind, where its first split ran.
    for method in dict.fromkeys(ev["method"] for ev in splits):
        group = [ev for ev in splits if ev["method"] == method]
        for k, t in enumerate(_split_tables(group, shares, context)):
            placed.append((group[0]["at"] + k / 1000, t))
    return [t for _, t in sorted(placed, key=lambda p: p[0])]


# Steps that only keep the run going (no check of the app) are left out of a segment's
# steps table: a session, a sign-in, a segment carried over from an earlier run.
_NOT_A_CHECK = ("session ready", "carried over", "logged in", "already logged in")
_STEP_NAMES = {"@open_order": "Open the order", "@open_event": "Open the event",
               "@kitchen_ready": "Kitchen: mark ready", "@kitchen_ready_if_sent":
               "Kitchen: ready what was sent", "@serve_if_any": "Serve what is ready",
               "@send_if_any": "Send what is unsent", "@select_all_items": "Select all items",
               "@send_to_kitchen": "Send to the kitchen", "@notify_payment": "Notify payment",
               "@c_confirm_order": "Confirm the order", "@consumer_home": "C-App home",
               "@to_checkout": "Checkout", "@got_it": "Close the confirmation",
               # Its sums are in the report's one invoice (invoice_section.py).
               "@c_invoice": "Open the diner's own invoice",
               "@w_invoice": "Open the event invoice", "@close_table": "Close the table"}


def _step_label(step: str) -> str:
    """'@w_qty:Guest 5/Pizza Salamees =6' -> 'Quantity: Guest 5/Pizza Salamees =6'."""
    head, _, rest = step.partition(":")
    pretty = {"@w_qty": "Quantity", "@c_add": "C-App add", "@w_move": "Move a line",
              "@w_void": "Void", "@w_gsplit": "Share a line", "@w_split": "Swipe split",
              "@w_add": "Waiter add", "@c_rate": "Rate the restaurant",
              "@c_skip_review": "Skip the rating", "@w_payer": "Next payer"}
    if step in _STEP_NAMES:
        return _STEP_NAMES[step]
    if head in pretty and rest:
        return f"{pretty[head]}: {rest.strip()}"
    return step


def steps_table(notes: List[str]) -> Optional[Table]:
    """'Steps checked': each step a segment passed and what the app showed for it, for a
    segment that checks no sums (a quantity change, the kitchen, send / serve)."""
    rows = []
    for n in (str(x) for x in notes or []):
        m = _OK.match(n)
        if not m or m.group("step").startswith(_NOT_A_CHECK):
            continue
        # A long list of the app's ids ('(PennePollo, TagliatelledelloChef, …)') is for
        # the engineers' log, not the report's readers: the count before it says enough.
        text = re.sub(r"\s*\((?:[^()]*,){2,}[^()]*\)", "", m.group("text"))
        rows.append([_step_label(m.group("step")), text, True])
    if not rows:
        return None
    return {"t": "Steps checked", "h": ["Step", "What the app showed"], "r": rows}
