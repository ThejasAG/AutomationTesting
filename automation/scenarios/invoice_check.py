"""Does an open invoice add up?

An invoice's rows carry no ids, so they are read from the screen (screen_text). This is
the part that needs no device: it puts the words of each screen back into one list of
rows, splits them into the invoice's blocks and recalculates what the invoice prints,
against itself and against what the run paid.

What the two invoices print (the source on preprod-2-May18):
- B-App event invoice (Components/Modal/index.js InvoiceModal): Pre-Order (dishes, Total,
  EC-Card, VAT table), then on-Permises [sic] (dishes, Tip, Due, Total, EC-Card, Cash, Food
  Voucher, VAT table). Its on-premises dishes show the price BEFORE any comp and the comp
  is not printed, so those rows cannot be added up to the Total.
- C-App View PDF (Screens/Wallet/CardData.js ViewPdfModal): the same two blocks for one
  diner. Dishes (with '+' when paid for someone else) and '• Discount n%' rows for comps,
  Tip, Paid by, Total, EC-Card, Cash, Food Voucher, Balance, VAT table.

A block's VAT rows hold every line's real price: Total = the VAT rows' ATI + Tip, which
InvoiceModal and ViewPdfModal both compute that way. Prices include VAT but size prices
carry none, so a VAT row can be below its rate and never above it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

Word = Tuple[str, float, float, float, float]       # text, x, y, w, h (screen_text.read_text)

ROW_TOL = 0.011         # one printed figure against another: both rounded to the cent
SUM_TOL = 0.021         # a sum of the three VAT rows, each rounded on its own
SAME_ROW = 7.0          # pt between two words' centres on one row; rows are 18+ apart
EDGE = 12.0             # pt: a row cut by the screen's edge reads higher than it really is

# Whole euros too: the phone prints an unset balance as '0€' (CardData.js:3551).
_EURO_RE = re.compile(r"(?<![\d.,])(\d{1,6}(?:[.,]\d{1,2})?)\s*€")
_BARE_RE = re.compile(r"[+\-−–]?\s*(\d{1,6}[.,]\d{2})")
_BULLET_RE = re.compile(r"^[\s•·.,*\-–]+")
_VAT_RE = re.compile(r"^vat\s*(\d{1,2})\s*%", re.I)
_PRE_ORDER_RE = re.compile(r"^(pre-?\s?order|pick-?\s?up)$", re.I)
_ON_PREMISES_RE = re.compile(r"^on[\s-]*(prem|perm)", re.I)
_COMP_RE = re.compile(r"^discount\s*\d+(\.\d+)?\s*%", re.I)
_LABELS = (("total", re.compile(r"^total\b(?!\s*vat)", re.I)),
           ("discount", re.compile(r"^discount\b", re.I)),
           ("tip", re.compile(r"^tip\b", re.I)),
           ("due", re.compile(r"^(due|balance)\b", re.I)),
           ("card", re.compile(r"^(ec-?\s?card|bank card)", re.I)),
           ("cash", re.compile(r"^cash\b", re.I)),
           ("voucher", re.compile(r"^food\s*voucher", re.I)),
           ("paid_by", re.compile(r"^paid by\b", re.I)))


@dataclass
class Row:
    y: float
    text: str
    money: List[float]


@dataclass
class Block:
    name: str = ""
    items: List[float] = field(default_factory=list)
    comps: List[float] = field(default_factory=list)
    totals: List[float] = field(default_factory=list)
    discount: Optional[float] = None
    tip: Optional[float] = None
    due: Optional[float] = None
    card: Optional[float] = None
    cash: Optional[float] = None
    voucher: Optional[float] = None
    vat: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    unread: List[str] = field(default_factory=list)
    texts: Dict[str, str] = field(default_factory=dict)      # 'VAT 3', 'Total' -> the row read


@dataclass
class Report:
    lines: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)      # calc_note() lines, for the report
    sums: int = 0


def _money_in(word: str) -> List[float]:
    """The euro amounts in one word. A bare number counts only as a whole word: a dish
    can be called 'Chianti Riserva 0,75lj', but '0,75' alone is never a dish."""
    found = _EURO_RE.findall(word)
    if not found:
        whole = _BARE_RE.fullmatch(word.strip())
        found = [whole.group(1)] if whole else []
    return [float(f.replace(",", ".")) for f in found]


def rows_of(words: List[Word]) -> List[Row]:
    """A screen's words as rows, top to bottom. The reader lists them column by column."""
    rows: List[Tuple[float, List[Word]]] = []
    for w in sorted(words, key=lambda t: t[2] + t[4] / 2):
        centre = w[2] + w[4] / 2
        if rows and abs(rows[-1][0] - centre) <= SAME_ROW:
            rows[-1][1].append(w)
        else:
            rows.append((centre, [w]))
    out = []
    for centre, group in rows:
        group.sort(key=lambda t: t[1])
        out.append(Row(centre, " ".join(t[0] for t in group),
                       [m for t in group for m in _money_in(t[0])]))
    return out


def _key(row: Row) -> str:
    return re.sub(r"\s+", "", row.text).lower()


def _shift(before: List[Row], after: List[Row]) -> Optional[float]:
    """How far the content moved up between two screens, from the rows both show. Every
    row read on both votes; repeated rows ('Customization') scatter their votes, the real
    distance collects them all. None when fewer than two rows agree."""
    votes: Dict[int, List[float]] = {}
    for b in after:
        for a in before:
            if _key(a) == _key(b) and a.y - b.y > -3:
                votes.setdefault(round((a.y - b.y) / 4), []).append(a.y - b.y)
    if not votes:
        return None
    best = max(votes, key=lambda k: len(votes[k]) + len(votes.get(k + 1, ()))
               + len(votes.get(k - 1, ())))
    near = votes[best] + votes.get(best + 1, []) + votes.get(best - 1, [])
    return sum(near) / len(near) if len(near) >= 2 else None


def stitch(pages: List[List[Word]]) -> Tuple[List[Row], int]:
    """One invoice from its overlapping screens: the rows top to bottom, each once, and
    how many times two screens shared nothing (rows may be missing there)."""
    out: List[Row] = []
    before: List[Row] = []
    offset, gaps = 0.0, 0
    for words in pages:
        rows = rows_of(words)
        if not rows:
            continue
        if not out:
            out, before = list(rows), rows
            continue
        moved = _shift(before, rows)
        if moved is None:
            gaps += 1
            offset = out[-1].y + 30 - rows[0].y
        else:
            offset += moved
        placed = [Row(r.y + offset, r.text, r.money) for r in rows]
        edge = out[-1].y
        fresh = [r for r in placed if r.y > edge - EDGE]
        # The old screen's last row may be cut by its edge: the new one reads it whole.
        if moved is not None and fresh and abs(fresh[0].y - edge) <= EDGE:
            out.pop()
        out.extend(fresh)
        before = rows
    return out, gaps


def parse(rows: List[Row]) -> List[Block]:
    """The invoice's blocks. Each ends with its VAT table: money after one means the next
    block even if its heading was misread."""
    blocks = [Block()]
    for row in rows:
        cur = blocks[-1]
        label = _BULLET_RE.sub("", row.text).strip()
        name = ("Pre-Order" if _PRE_ORDER_RE.match(label) else
                "On Premises" if _ON_PREMISES_RE.match(label) else "")
        if name:
            if cur.name or cur.totals or cur.items:
                blocks.append(Block(name=name))
            else:
                cur.name = name
            continue
        vat = _VAT_RE.match(label)
        if vat:
            rate = int(vat.group(1))
            cur.texts[f"VAT {rate}"] = row.text[:80]
            if len(row.money) >= 3:
                excl, tax, ati = row.money[-3:]
                cur.vat[rate] = (excl, tax, ati)
            else:
                cur.unread.append(row.text[:60])
            continue
        kind = next((k for k, rx in _LABELS if rx.match(label)), None)
        if not row.money:
            if kind not in (None, "paid_by", "discount"):
                cur.unread.append(row.text[:60])
            continue
        if cur.vat:
            blocks.append(Block(name="On Premises" if cur.name == "Pre-Order" else ""))
            cur = blocks[-1]
        amount = row.money[-1]
        if _COMP_RE.match(label):
            cur.comps.append(amount)
        elif kind == "total":
            cur.totals.append(amount)
            cur.texts["Total"] = row.text[:80]
        elif kind and kind != "paid_by":
            setattr(cur, kind, (getattr(cur, kind) or 0.0) + amount)
        elif kind is None:
            cur.items.append(amount)
    return [b for b in blocks if b.name or b.totals or b.items or b.vat]


def seen_of(blocks: List[Block]) -> Dict[str, str]:
    """The rows a step reports: the VAT rows and Total of the last block that has one."""
    for b in reversed(blocks):
        if "Total" in b.texts:
            return {**{k: v for k, v in sorted(b.texts.items()) if k != "Total"},
                    "Total": b.texts["Total"]}
    return {}


def _adds(calc: float, shown: float, tol: float = SUM_TOL) -> bool:
    return abs(calc - shown) <= tol


def _plus(values: Iterable[float]) -> str:
    return " + ".join(f"{v:.2f}" for v in values)


def eur(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f}"


def _diff(calc: Optional[float], shown: Optional[float]) -> str:
    # + 0.0 turns the -0.0 that rounding a tiny negative leaves into 0.0: '+0.00', not '-0.00'.
    return "—" if calc is None or shown is None else f"{round(shown - calc, 2) + 0.0:+.2f}"


# ── The report's tables ──────────────────────────────────────────────────────
# Every money check also leaves one '[calc] {json}' note: a titled table of what the
# test calculated against what the app showed, a row per sum whose last cell is True
# (matches), False (does not) or None (information). Notes outlive a resume; the run's
# HTML report renders them (automation/reporting/scenario_report.py), step lists hide
# them. A sum that does not match also leaves a '[mismatch]' note and the step goes on.
CALC_PREFIX = "[calc] "
MISMATCH_PREFIX = "[mismatch] "


def calc_note(title: str, head: List[str], rows: List[list]) -> str:
    return CALC_PREFIX + json.dumps({"t": title, "h": head, "r": rows}, ensure_ascii=False,
                                    separators=(",", ":"))


def calc_of(note) -> Optional[Dict]:
    """The table a '[calc]' note holds, or None."""
    note = str(note)
    if not note.startswith(CALC_PREFIX):
        return None
    try:
        table = json.loads(note[len(CALC_PREFIX):])
    except ValueError:
        return None
    return table if isinstance(table, dict) and table.get("t") else None


def _sum_mismatch(what: str, before: Optional[float], calc: Optional[float],
                  shown: Optional[float]) -> Optional[str]:
    """None when `shown` is what was calculated, else the line the report prints red."""
    if shown is None:
        return f"{what}: could not read the Total after it"
    if before is None:
        return f"{what}: could not read the Total before it"
    if calc is not None:
        if _adds(calc, shown):
            return None
        return (f"{what}: before €{before:.2f}, calculated €{calc:.2f} but displayed "
                f"€{shown:.2f} (diff {shown - calc:+.2f})")
    if shown < before - 0.005:
        return None                     # it fell; by how much is not known
    return f"{what}: before €{before:.2f}, the Total did not fall (displayed €{shown:.2f})"


_OFFER_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_OFFER_EUR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*€")


def offer_of(text: str) -> Tuple[Optional[float], Optional[float]]:
    """A coupon's or discount tile's value: '31.5% OFFER …' -> (31.5, None), '2€ OFFER'
    -> (None, 2.0), nothing readable -> (None, None)."""
    m = _OFFER_PCT_RE.search(text or "")
    if m:
        return float(m.group(1).replace(",", ".")), None
    m = _OFFER_EUR_RE.search(text or "")
    return (None, float(m.group(1).replace(",", "."))) if m else (None, None)


def offer_check(kind: str, before: Optional[float], shown: Optional[float], offer: str,
                who: str = "") -> Tuple[str, Optional[str]]:
    """A coupon or discount: the Total it should leave against the one shown. Returns
    the report table and the mismatch, if any."""
    pct, fixed = offer_of(offer)
    label = (f"{pct:g}%" if pct is not None else f"{fixed:.2f} €" if fixed is not None
             else repr((offer or "")[:30]))
    calc = None
    if before is not None:
        calc = (round(before * (1 - pct / 100), 2) if pct is not None
                else round(before - fixed, 2) if fixed is not None else None)
    what = f"{kind} {label}" + (f" ({who})" if who else "")
    problem = _sum_mismatch(what, before, calc, shown)
    head = ["Total before", kind, f"Total after {kind.lower()}", "Displayed", "Diff"]
    row = [eur(before), f"-{label}", eur(calc), eur(shown), _diff(calc, shown), problem is None]
    return calc_note(what, head, [row]), problem


def tip_check(who: str, before: Optional[float], tip: float,
              shown: Optional[float]) -> Tuple[str, Optional[str]]:
    """A tip: the Total before it plus the tip, against the Total shown."""
    calc = None if before is None else round(before + tip, 2)
    what = f"Tip {tip:.2f} ({who})"
    problem = _sum_mismatch(what, before, calc, shown)
    if problem is None and calc is None:
        problem = f"{what}: could not read the Total before it"
    row = [eur(before), f"+{tip:.2f}", eur(calc), eur(shown), _diff(calc, shown), problem is None]
    return (calc_note(what, ["Total before", "Tip", "Total with tip", "Displayed", "Diff"], [row]),
            problem)


def charge_check(what: str, bill: Optional[float], charged: Optional[float],
                 charged_as: str = "Charged") -> Tuple[str, Optional[str]]:
    """A payment: the bill the app showed against the amount actually charged."""
    if bill is None or charged is None:
        row = [eur(bill), eur(charged), "—", None]
        return calc_note(what, ["Bill displayed", charged_as, "Diff"], [row]), None
    problem = (None if _adds(bill, charged, ROW_TOL) else
               f"{what}: bill displayed €{bill:.2f} but {charged_as.lower()} €{charged:.2f} "
               f"(diff {charged - bill:+.2f})")
    row = [eur(bill), eur(charged), _diff(bill, charged), problem is None]
    return calc_note(what, ["Bill displayed", charged_as, "Diff"], [row]), problem


def euro_after_sign(text: str) -> Optional[float]:
    """'Pay €273.57' -> 273.57."""
    m = re.search(r"€\s*(\d{1,6}(?:[.,]\d{1,2})?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


_TOTALS_HEAD = ["Check", "How", "Calculated", "Displayed", "Diff"]
_VAT_HEAD = ["Rate", "Excl. Tax", "VAT", "ATI", "Excl. Tax + VAT", "Most VAT at the rate"]


def check(blocks: List[Block], gaps: int = 0, cut: bool = False, comps_printed: bool = True,
          paid: Optional[Dict] = None, title: str = "Invoice") -> Report:
    """Recalculate the invoice. `comps_printed`: its on-premises rows show comps, so they
    can be added up. `paid`: paid_from_notes(), or None when the run's payments are
    unknown. `title` names the report's tables ('Event invoice (iPad)')."""
    rep = Report()
    totals: Dict[str, List[list]] = {}
    vat_rows: Dict[str, List[list]] = {}

    def compare(name: str, what: str, working: str, calc: float, shown: float,
                tol: float = SUM_TOL) -> None:
        rep.sums += 1
        ok = _adds(calc, shown, tol)
        line = f"{name} {what}: {working} = {calc:.2f}, invoice shows {shown:.2f}"
        if ok:
            rep.lines.append(line + " — matches")
        else:
            rep.problems.append(line + f" — off by {abs(calc - shown):.2f}")
        totals.setdefault(name, []).append([what, working, eur(calc), eur(shown),
                                            _diff(calc, shown), ok])

    if gaps:
        rep.problems.append(f"could not follow the scroll between {gaps} pair(s) of screens; "
                            f"rows may be missing")
    if cut:
        rep.problems.append("the end of the invoice was not reached; the rest is not checked")
    if not blocks:
        rep.problems.append("no block of the invoice could be read")
    for b in blocks:
        name = b.name or "Invoice"
        for row in b.unread:
            rep.problems.append(f"{name}: could not read the amounts on {row!r}")
        if not b.totals:
            rep.problems.append(f"{name}: no Total could be read")
            continue
        total, tip = b.totals[-1], b.tip or 0.0
        for rate, (excl, tax, ati) in sorted(b.vat.items()):
            rep.sums += 2
            adds = _adds(excl + tax, ati, ROW_TOL)
            line = (f"{name} VAT {rate}%: {excl:.2f} + {tax:.2f} = {excl + tax:.2f}, invoice "
                    f"shows {ati:.2f}")
            if adds:
                rep.lines.append(line + " — matches")
            else:
                rep.problems.append(line + f" — off by {abs(excl + tax - ati):.2f}")
            ceiling = ati * rate / (100 + rate)
            under = tax <= ceiling + ROW_TOL
            if not under:
                rep.problems.append(f"{name} VAT {rate}%: VAT {tax:.2f} on {ati:.2f} is above "
                                    f"the rate (at most {ceiling:.2f})")
            vat_rows.setdefault(name, []).append([f"VAT {rate}%", eur(excl), eur(tax), eur(ati),
                                                  eur(excl + tax), eur(ceiling), adds and under])
        if b.vat:
            atis = [v[2] for _, v in sorted(b.vat.items())]
            compare(name, "grand total", f"VAT rows {_plus(atis)}"
                    + (f" + tip {tip:.2f}" if tip else ""), sum(atis) + tip, total)
        else:
            rep.problems.append(f"{name}: no VAT row could be read")
        if b.discount is not None and len(b.totals) > 1:
            compare(name, "discount", f"{b.totals[0]:.2f} - {b.discount:.2f}",
                    b.totals[0] - b.discount, total)
        if not b.items:
            rep.problems.append(f"{name}: no dish row could be read")
        elif b.name == "Pre-Order" or comps_printed:
            # Each row is rounded on its own: half a cent per row can add up.
            tol = 0.01 + 0.005 * (len(b.items) + len(b.comps))
            working = f"{len(b.items)} dishes {sum(b.items):.2f}"
            if b.comps:
                working += f" - {len(b.comps)} comps {sum(b.comps):.2f}"
            if tip:
                working += f" + tip {tip:.2f}"
            compare(name, "dishes", working, sum(b.items) - sum(b.comps) + tip, total, tol)
        else:
            rep.lines.append(f"{name} dishes: {len(b.items)} rows list {sum(b.items):.2f} before "
                             f"comps; this invoice does not print comps, so they are not "
                             f"added up here")
            totals.setdefault(name, []).append(
                ["dishes", f"{len(b.items)} rows, before comps (this invoice prints no comps)",
                 eur(sum(b.items)), "—", "—", None])
        tendered = [v for v in (b.card, b.cash, b.voucher) if v is not None]
        if tendered:
            diff = sum(tendered) - total
            due = b.due or 0.0
            over = (paid or {}).get("overpaid", 0.0) if b.name != "Pre-Order" else 0.0
            rep.sums += 1
            how = (f"{_plus(tendered)} (card + cash + voucher)" + (f", Due {due:.2f}" if due else "")
                   + (f", overpaid on purpose {over:.2f}" if over else ""))
            line = (f"{name} payments: {_plus(tendered)} = {sum(tendered):.2f} against Total "
                    f"{total:.2f}" + (f", Due {due:.2f}" if due else "")
                    + (f", the run overpaid {over:.2f}" if over else ""))
            ok = any(_adds(abs(diff), x) for x in (0.0, due)) or (diff > 0 and _adds(diff, over))
            if ok:
                rep.lines.append(line + " — matches")
            else:
                rep.problems.append(line + f" — {abs(diff):.2f} unexplained")
            totals.setdefault(name, []).append(["payments", how, eur(sum(tendered)), eur(total),
                                                _diff(total, sum(tendered)), ok])
    if paid is None:
        rep.lines.append("Not compared with the payments: this run's are not known")
    elif paid.get("incomplete"):
        rep.lines.append("Not compared with the payments: "
                         "a payment was skipped or repeated in this run")
    else:
        _against_paid(blocks, paid, compare, rep)
    for b in blocks:
        name = b.name or "Invoice"
        if name in totals:
            rep.tables.append(calc_note(f"{title} — {name}: totals", _TOTALS_HEAD,
                                        totals.pop(name)))
        if name in vat_rows:
            rep.tables.append(calc_note(f"{title} — {name}: VAT", _VAT_HEAD, vat_rows.pop(name)))
    grand = [(b.name or "Invoice", b.totals[-1]) for b in blocks if b.totals]
    if len(grand) > 1:
        rep.lines.append("Whole invoice: " + " + ".join(f"{n} {v:.2f}" for n, v in grand)
                         + f" = {sum(v for _, v in grand):.2f}")
        rep.tables.append(calc_note(f"{title} — whole invoice", ["Block", "Total"],
                                    [[n, eur(v), None] for n, v in grand]
                                    + [["All blocks", eur(sum(v for _, v in grand)), None]]))
    rep.lines.append(f"invoice checked: {rep.sums} sums, {len(rep.problems)} do not match")
    return rep


def _against_paid(blocks: List[Block], paid: Dict, compare, rep: Report) -> None:
    """The invoice against the money this run saw taken: figures the invoice did not print."""
    by_name = {b.name: b for b in blocks}
    for name, spent in (("Pre-Order", paid.get("preorder") or []),
                        ("On Premises", paid.get("table") or [])):
        block = by_name.get(name)
        if not spent:
            continue
        if block is None or not block.totals:
            rep.problems.append(f"{name}: the run paid {_plus(spent)} but the invoice shows no "
                                f"{name} total")
            continue
        compare(name, "against the payments", _plus(spent), sum(spent), block.totals[-1])
    tips = paid.get("tips") or []
    block = by_name.get("On Premises")
    if tips and block is not None:
        compare("On Premises", "Tip against the tips given", _plus(tips), sum(tips),
                block.tip or 0.0)


_PREORDER_PAID_RE = re.compile(r"@pay_stripe — tapped 'Pay €\s*([\d.,]+)'")
_C_PAID_RE = re.compile(r"@consumer_epay — paid €\s*([\d.,]+)")
_BILL_RE = re.compile(r"Bill check: total €\s*([\d.,]+), paid €\s*([\d.,]+)")
_TIP_RE = re.compile(r"^\[ok\] @[cw]_tip:.*\+ tip ([\d.,]+) €")
_NOT_ONCE_RE = re.compile(r"^\[skip\] @(consumer_epay|pay:).*nothing left to pay"
                          r"|paying by E-Payment again")


def _euro(text: str) -> float:
    return float(text.rstrip(".,").replace(",", "."))


def paid_from_notes(lines: Iterable[Tuple[str, str]], role: Optional[str] = None) -> Dict:
    """What the run's own notes say was paid. `lines` are (segment role, note); `role`
    keeps one diner's payments (the host = 'consumer'), None keeps everyone's. The notes
    survive a resume, which is why they are read rather than kept in memory.

    `overpaid` is what the run tendered on purpose above a bill (PAY_OVER_BY,
    PAY_FOR_OTHERS_OVER_BY). `incomplete`: a payment step did not run exactly once."""
    out: Dict = {"preorder": [], "table": [], "tips": [], "overpaid": 0.0, "incomplete": False}
    for who, line in lines:
        if role is not None and who != role:
            continue
        if _NOT_ONCE_RE.search(line):
            out["incomplete"] = True
        m = _PREORDER_PAID_RE.search(line)
        if m:
            out["preorder"].append(_euro(m.group(1)))
        m = _C_PAID_RE.search(line)
        if m:
            out["table"].append(_euro(m.group(1)))
        m = _BILL_RE.search(line)
        if m:
            bill, tendered = _euro(m.group(1)), _euro(m.group(2))
            out["table"].append(bill)
            out["overpaid"] = round(out["overpaid"] + max(0.0, tendered - bill), 2)
        m = _TIP_RE.search(line)
        if m:
            out["tips"].append(_euro(m.group(1)))
    return out
