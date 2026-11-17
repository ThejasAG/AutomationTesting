"""Every money formula the tests use, in one place.

The same rules hold for every scenario. Other files only read the figures (screen,
invoice, run notes) and draw the report; for any calculation or pass/fail on money they
call this file.

Exact to the cent: a 0.01 difference fails. TOL (half a cent) only absorbs float noise
(0.1 + 0.2 = 0.30000000000000004). Prices include VAT (ATI = all taxes included).
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

TOL = 0.005


# ── compare ─────────────────────────────────────────────────────────────────

def same(a: Optional[float], b: Optional[float]) -> Optional[bool]:
    """Same amount to the cent. None when either was not read."""
    return None if a is None or b is None else abs(a - b) <= TOL


def diff(calc: float, shown: float) -> float:
    """shown − calc, to the cent (+ 0.0 avoids printing '-0.00')."""
    return round(shown - calc, 2) + 0.0


# ── VAT ─────────────────────────────────────────────────────────────────────

def vat_in(ati: float, rate: float) -> float:
    """VAT inside a price: ATI × rate / (100 + rate). 125.56 at 14 % → 15.42."""
    return ati * rate / (100 + rate)


def vat_row_adds(excl: float, vat: float, ati: float) -> bool:
    """Excl. Tax + VAT = ATI."""
    return abs(excl + vat - ati) <= TOL


def vat_within_rate(vat: float, ati: float, rate: float) -> bool:
    """VAT is not more than the rate allows on that ATI."""
    return vat <= vat_in(ati, rate) + TOL


# ── invoice sums ────────────────────────────────────────────────────────────

def vat_rows_total(atis: Iterable[float], tip: float = 0.0) -> float:
    """Total = the VAT rows' ATI + tip."""
    return round(sum(atis) + tip, 2)


def dishes_total(dishes: Iterable[float], comps: Iterable[float] = (),
                 tip: float = 0.0) -> float:
    """Total = dishes − comps + tip."""
    return round(sum(dishes) - sum(comps) + tip, 2)


def after_discount(before: float, discount: float) -> float:
    """Total = before − discount."""
    return round(before - discount, 2)


def payments_match(tendered: float, total: float, due: float = 0.0,
                   overpaid: float = 0.0) -> bool:
    """Payments = Total, or differ by exactly the Due (change owed back) or what the run
    overpaid on purpose."""
    gap = tendered - total
    return any(abs(abs(gap) - x) <= TOL for x in (0.0, due)) or \
        (gap > 0 and abs(gap - overpaid) <= TOL)


# ── coupon / discount / tip / change ────────────────────────────────────────

def after_offer(before: float, pct: Optional[float] = None,
                fixed: Optional[float] = None) -> Optional[float]:
    """Coupon or discount: before × (1 − pct/100), or before − fixed."""
    if pct is not None:
        return round(before * (1 - pct / 100), 2)
    if fixed is not None:
        return round(before - fixed, 2)
    return None


def with_tip(before: float, tip: float) -> float:
    """Total = before + tip."""
    return round(before + tip, 2)


def change(tendered: float, bill: float) -> float:
    """Change = tendered − bill."""
    return round(tendered - bill, 2)


# ── split ───────────────────────────────────────────────────────────────────

def fair_shares(total: float, n: int) -> List[float]:
    """Split to the cent: 10.20 / 2 → 5.10, 5.10; 10.21 / 2 → 5.11, 5.10."""
    base, extra = divmod(int(round(total * 100)), n)
    return [(base + (1 if i < extra else 0)) / 100 for i in range(n)]


def shares_ok(prices: Iterable[float], total: float) -> bool:
    """The shares are exactly the fair shares of total (any order)."""
    got = sorted(int(round(p * 100)) for p in prices)
    return bool(got) and got == sorted(int(round(p * 100))
                                       for p in fair_shares(total, len(got)))


def even_share(total: float, n: int) -> float:
    """total / n — an estimate only, where the app's split was not read."""
    return round(total / n, 2)


# ── void / comp ─────────────────────────────────────────────────────────────

def void_expected(line_before: float, units_before: int, units_after: int) -> float:
    """Line after a void = line before ÷ units before × units after (33.99/3×2 = 22.66)."""
    return round(line_before / units_before * units_after, 2)


def void_value(line_before: float, line_after: float) -> float:
    """What a void took off: line before − line after."""
    return round(line_before - line_after, 2)


def comp_whole_line(before: float, pct: float) -> Tuple[float, float]:
    """Comp of a whole line: (comp = before × pct/100, line after = before − comp)."""
    comp = round(before * pct / 100, 2)
    return comp, round(before - comp, 2)


def comp_units(before: float, after: float, units: int,
               pct: float) -> Tuple[float, float, bool, float, float]:
    """Comp of some units (they move to a comped line of their own):
    unit = (before − after) / units; held = before / unit (must be whole);
    comp = units × unit × pct/100; line after = before − units × unit.
    Returns (unit, held, whole, comp, line after)."""
    unit = (before - after) / units if units else 0.0
    held = before / unit if unit > 0 else 0.0
    whole = held > 0 and abs(held - round(held)) < 0.02      # 0.02 of a unit, not €
    return (unit, held, whole, round(units * unit * pct / 100, 2),
            round(before - units * unit, 2))


def comp_value(line_before: float, line_after: float, pct: float,
               whole_line: bool) -> float:
    """What a comp takes off the grand Total: what the line lost (whole line), or
    what it lost × pct/100 (some units)."""
    lost = line_before - line_after
    return round(lost if whole_line else lost * pct / 100, 2)


# ── grand Total around a split / void / comp ───────────────────────────────

def grand_total_expected(kind: str, before: Optional[float],
                         value: Optional[float]) -> Optional[float]:
    """Split: unchanged. Void / comp: before − the voided / comped value."""
    taken = 0.0 if kind == "split" else value
    return None if before is None or taken is None else round(before - taken, 2)
