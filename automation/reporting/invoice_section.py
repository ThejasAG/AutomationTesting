"""ONE invoice in the report, in the layout of the user's invoice sample.

The user, 2026-11-05, with the 'PDF Invoice Report' sample ('Event PDF vs Stored
Payments'): "for invoice keep only one whole invoice like this ... dont give like host
invoice, participant invoice, waiter invoice keep only one that entire invoice must be
like this".

So the report draws one invoice: the event invoice (the B-App's, every diner's Pre-Order
and On Premises sections) when the run read it, else the one invoice it read. It is drawn
in the row of the segment that read it, element for element as the sample:

  Invoice Total vs Stored Total     the invoice's Total against what the run paid
  Paid for (Invoice)                Expected N: who was paid for / Invoice shows: ...
  VAT (Invoice vs Calculated)       Rate | Invoice / Calc Excl, VAT, ATI | Status
  Invoice lines (Invoice vs Stored) each section's dishes against what was paid for it
  Invoice adds up (VAT rows + tip)  ✓ €calculated vs total €Total
  Tip, Outstanding                  Invoice = | Stored = | diff
  Invoice vs Stored Payments        Field | Invoice | Stored | Status

Another invoice the run read (a diner's own, in the C-App) is not drawn when all its
sums matched; one that did not keeps its tables in its own row. The figures are the
run's own notes (its invoices' '[calc]' tables and its paying segments' tables):
nothing is read again from a device.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from automation import formulas
from automation.reporting.step_tables import tables_of

_INVOICE = re.compile(r"^(?P<inv>Event invoice \(iPad\)|Invoice of the (?P<who>\w+) \(phone\)) — "
                      r"(?P<part>Pre-Order|On Premises|whole invoice)(?:: (?P<kind>totals|VAT))?$")
EVENT = "Event invoice (iPad)"
PARTS = ("Pre-Order", "On Premises")
CLOSE = "Outstanding Balance Check"         # step_tables._KINDS '@close_table'
_RATE = re.compile(r"^VAT\s*(\d{1,2})\s*%$")
_DUE = re.compile(r"Due (\d+(?:\.\d{1,2})?)")
_GUEST = re.compile(r"^Guest \d+$")
# A payment of the run, by the title of the table its step wrote: (section, method,
# column of the amount booked, column of what was handed over or None).
_PAYMENTS = [
    (re.compile(r"^Pre-order payment \((?P<who>.+)\)$"), "Pre-Order", "Stripe (C-App)", 1, None),
    (re.compile(r"^E-Payment \((?P<who>.+)\)$"), "On Premises", "Stripe (C-App)", 1, None),
    (re.compile(r"^Payment by (?P<how>E-Payment|Cash|Food Voucher) \((?P<who>.+)\)$"),
     "On Premises", None, 0, 1),
]
_PAY_FOR = re.compile(r"^Pay for others \((?P<who>.+)\)$")
_TIP = re.compile(r"^Tip [\d.]+ \((?P<who>.+)\)$")
_COUPON = re.compile(r"^Coupon (?P<pct>[\d.]+%) \((?P<who>.+)\)$")

Table = Dict[str, Any]


def _amount(cell) -> Optional[float]:
    try:
        return float(str(cell).replace("€", "").replace(",", ".").replace("+", "").strip())
    except ValueError:
        return None


def eur(v: Optional[float]) -> str:
    return "—" if v is None else f"€{v:.2f}"


def _same(a: Optional[float], b: Optional[float]) -> Optional[bool]:
    return formulas.same(a, b)


def _reasons(rec: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("consumer_reasons", "business_reasons", "reasons"):
        out += [str(n) for n in rec.get(key) or []]
    return out


def _rows(t: Optional[Table]) -> List[list]:
    return [r for r in (t or {}).get("r") or [] if isinstance(r, list) and r]


def _row(t: Optional[Table], check: str) -> Optional[list]:
    return next((r for r in _rows(t) if r[0] == check), None)


def _diner(who: str, segment: str) -> str:
    """'host' -> 'Host'; the participant's own name, in a participant's segment, says so."""
    w = who.strip()
    if w.lower() in ("host", "participant"):
        return w.title()
    if not _GUEST.match(w) and "participant" in segment.lower():
        return f"{w} (participant)"
    return w


def _invoices(per: List[List[Table]]) -> Dict[str, Dict[str, Any]]:
    """Every invoice the run read: name -> where, whose, its sections' tables, its titles."""
    found: Dict[str, Dict[str, Any]] = {}
    for i, tables in enumerate(per):
        for t in tables:
            m = _INVOICE.match(str(t.get("t") or ""))
            if not m:
                continue
            inv = found.setdefault(m.group("inv"), {"at": i, "who": m.group("who"),
                                                    "parts": {}, "titles": {}})
            inv["titles"].setdefault(i, set()).add(t["t"])
            if m.group("kind"):
                inv["parts"].setdefault(m.group("part"), {})[m.group("kind")] = t
    return found


def _payments(per: List[List[Table]], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the run paid, from its paying segments' own tables."""
    pays: List[Dict[str, Any]] = []
    pays_for: Dict[str, str] = {}
    tips: Dict[str, float] = {}
    coupons: Dict[str, str] = {}
    for i, tables in enumerate(per):
        segment = str(records[i].get("name") or "")
        for t in tables:
            title = str(t.get("t") or "")
            rows = _rows(t)
            m = _PAY_FOR.match(title)
            if m and rows:
                pays_for[m.group("who")] = str(rows[0][0])
                continue
            m = _TIP.match(title)
            if m and rows and len(rows[0]) > 1:
                tips[m.group("who")] = _amount(rows[0][1]) or 0.0
                continue
            m = _COUPON.match(title)
            if m:
                coupons[m.group("who")] = m.group("pct")
                continue
            for pat, section, method, at, handed in _PAYMENTS:
                m = pat.match(title)
                if not (m and rows and len(rows[0]) > at):
                    continue
                r = rows[0]
                how = method or f"{m.group('how')} (waiter's iPad)"
                given = _amount(r[handed]) if handed is not None and len(r) > handed else None
                pays.append({"who": m.group("who"), "diner": _diner(m.group("who"), segment),
                             "section": section, "method": how, "amount": _amount(r[at]),
                             "handed": given, "e_payment": how.startswith("E-Payment"),
                             "ok": r[-1] if isinstance(r[-1], bool) else None})
                break
    # The participant pre-orders as 'participant' and pays at the table under their own
    # name: one diner, so one name (when the run had one participant).
    named = {p["diner"] for p in pays if p["diner"].endswith("(participant)")}
    if len(named) == 1:
        for p in pays:
            if p["who"].lower() == "participant":
                p["diner"] = next(iter(named))
    for p in pays:
        p.setdefault("extras", "")
        if p["section"] == "Pre-Order":
            p["for"] = "own pre-order"
            continue
        extras = [f"coupon {coupons[p['who']]}"] if p["who"] in coupons else []
        if p["who"] in tips:
            extras.append(f"tip {eur(tips[p['who']])}")
        others = pays_for.get(p["who"])
        p["for"] = ("own share + " + others) if others else "own share"
        p["extras"] = ", ".join(extras)
    return {"pays": pays, "pays_for": pays_for, "tips": tips}


def _part_figures(parts: Dict[str, Dict[str, Table]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name in PARTS:
        tabs = parts.get(name)
        if not tabs:
            continue
        totals, vat = tabs.get("totals"), tabs.get("VAT")
        grand = _row(totals, "grand total")
        against = _row(totals, "against the payments")
        printed = _row(totals, "payments")
        tip = _row(totals, "Tip against the tips given")
        due = _DUE.search(str(printed[1])) if printed and len(printed) > 1 else None
        out[name] = {
            "total": _amount(grand[3]) if grand and len(grand) > 3 else None,
            "stored": _amount(against[2]) if against and len(against) > 2 else None,
            "stored_how": str(against[1]) if against else "",
            "stored_ok": against[-1] if against and isinstance(against[-1], bool) else None,
            "printed": _amount(printed[2]) if printed and len(printed) > 2 else None,
            "printed_ok": printed[-1] if printed and isinstance(printed[-1], bool) else None,
            "tip": tip, "due": float(due.group(1)) if due else 0.0,
            "adds_up": [r for r in _rows(totals) if r[0] in ("grand total", "dishes", "discount")],
            "vat": [r for r in _rows(vat) if _RATE.match(str(r[0]))],
            "failed": [r for r in _rows(totals) + _rows(vat) if r[-1] is False],
        }
    return out


def _failed(parts: Dict[str, Dict[str, Table]]) -> List[list]:
    return [r for f in _part_figures(parts).values() for r in f["failed"]]


_QTY = re.compile(r"(\d+) (?:dishes|rows)")


def build(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The run's one invoice section, or None when it read no invoice.

    Returns {"at": the record it is drawn in, "drop": {record index: titles it replaces},
    "section": what scenario_report._render_invoice draws}."""
    per = [tables_of(_reasons(rec)) for rec in records]
    found = _invoices(per)
    if not found:
        return None
    name = EVENT if EVENT in found else next(iter(found))
    inv = found[name]
    figs = _part_figures(inv["parts"])
    if not figs:
        return None
    drop: Dict[int, set] = {}
    for other_name, other in found.items():
        # Another invoice leaves its row only when all its sums matched: a red one stays
        # where it was read, never hidden.
        if other_name != name and _failed(other["parts"]):
            continue
        for i, titles in other["titles"].items():
            drop.setdefault(i, set()).update(titles)
    paid = _payments(per, records)
    close = None
    for i, tables in enumerate(per):
        for t in tables:
            if t.get("t") == CLOSE and _rows(t):
                close = _rows(t)[0]
                drop.setdefault(i, set()).add(CLOSE)

    # Invoice Total vs Stored Total: the invoice's sections' Totals against what the run
    # paid for each ('against the payments').
    total = round(sum(f["total"] or 0.0 for f in figs.values()), 2)
    stored_known = all(f["stored"] is not None for f in figs.values())
    stored = round(sum(f["stored"] for f in figs.values()), 2) if stored_known else None
    total_ok = ((_same(total, stored) and all(f["stored_ok"] for f in figs.values()))
                if stored_known else None)

    # Paid for: who paid for whom in the run; what the invoice's payment lines print.
    label = {x["who"]: x["diner"] for x in paid["pays"]}
    paid_for = [(others, label.get(who, _diner(who, "")))
                for who, others in paid["pays_for"].items()]
    guests = sum(len([g for g in others.split(",") if g.strip()]) for others, _ in paid_for)
    printed = [f for f in figs.values() if f["printed"] is not None]
    printed_total = round(sum(f["printed"] for f in printed), 2) if printed else None
    printed_of = round(sum(f["total"] or 0.0 for f in printed), 2) if printed else None
    over = any(f["printed_ok"] is not True and f["printed"] > (f["total"] or 0) + formulas.TOL
               for f in printed)
    short = any(f["printed_ok"] is not True and f["printed"] < (f["total"] or 0) - formulas.TOL
                for f in printed)

    # VAT, section by section, as the invoice prints it.
    vat = [(p, f["vat"]) for p, f in figs.items() if f["vat"]]

    # Invoice lines: each section's dishes (and tip) against what the run paid for it.
    lines = []
    for p, f in figs.items():
        dishes = next((r for r in f["adds_up"] if r[0] == "dishes"), None)
        qty = _QTY.search(str(dishes[1])) if dishes else None
        tip_row = f["tip"]
        tip_v = _amount(tip_row[3]) if tip_row else 0.0
        item = f"{p} dishes" + (f" + tip {eur(tip_v)}" if tip_v else "")
        lines.append([item, qty.group(1) if qty else "—", f["total"], f["stored"],
                      _same(f["total"], f["stored"]) if f["stored"] is not None else None])

    # Invoice adds up: every section's VAT rows (+ tip) give its Total.
    grand = [r for f in figs.values() for r in f["adds_up"] if r[0] == "grand total"]
    adds = {"calc": round(sum(_amount(r[2]) or 0.0 for r in grand), 2),
            "total": round(sum(_amount(r[3]) or 0.0 for r in grand), 2),
            "ok": (False if any(r[-1] is False for r in grand) else True) if grand else None}

    # Tip: the invoice's against the tips the run gave.
    on = figs.get("On Premises") or {}
    tip_row = on.get("tip")
    tip = {"invoice": _amount(tip_row[3]) if tip_row else 0.0,
           "stored": _amount(tip_row[2]) if tip_row else round(sum(paid["tips"].values()), 2)}
    tip["ok"] = (tip_row[-1] if tip_row and isinstance(tip_row[-1], bool)
                 else _same(tip["invoice"], tip["stored"]))

    # Outstanding: the invoice's 'Due' is change owed back (known_issues.md), from paying
    # a little above the bill; the table closed in the run.
    due = round(sum(f["due"] for f in figs.values()), 2)
    e_change = [x for x in paid["pays"] if x["e_payment"] and x["handed"] is not None
                and x["amount"] is not None and formulas.change(x["handed"], x["amount"]) >= formulas.TOL]
    change = round(sum(formulas.change(x["handed"], x["amount"]) for x in e_change), 2)
    closed = bool(close) and "closed" in str(close[2] if len(close) > 2 else "")
    explained = not due or _same(change, due)
    outstanding = {"due": due, "explained": bool(explained), "closed": closed,
                   "ok": (close[-1] if close and isinstance(close[-1], bool)
                          else True if closed and explained else None)}

    return {
        "at": inv["at"], "drop": drop,
        "section": {
            "title": ("Event Invoice vs Stored Payments" if name == EVENT
                      else f"{(inv.get('who') or 'Diner').title()}'s Invoice vs Stored Payments"),
            "total": {"invoice": total, "stored": stored, "ok": total_ok},
            "paid_for": paid_for, "guests": guests,
            "printed": {"total": printed_total, "of": printed_of, "over": over, "short": short},
            "vat": vat, "lines": lines, "adds": adds, "tip": tip, "outstanding": outstanding,
        },
    }
