"""Rich scenario report renderer (VAT / bill validation).

Ported from the Vya bot's ``vya_payments_report.py`` but made a *pure*,
side-effect-free renderer for the platform:

  * No file I/O, no Windows paths, no cross-run history/dedupe — the platform
    already scopes results to a single run (``ScenarioResult`` rows).
  * Input is a list of plain dicts (see ``records_from_rows``) shaped like the
    bot's ``scenario_history.json`` records, so the exact same regex-driven VAT
    table rendering works unchanged.

The rich pre/post-payment VAT tables are built by parsing the ``reasons``
strings the validator emits during a run, e.g.::

    "Bill check ... VAT 3% mismatch: expected 0.03/100 × €6.96 = €0.21,
     displayed = €0.20, diff = €0.01"

so this module renders *real* numbers whenever those strings are present, and
degrades gracefully (plain status) when they are not.

Flows that check money themselves (flow_full_event) leave their tables ready-made as
'[calc] {json}' notes and every sum that did not match as a '[mismatch]' note
(automation/scenarios/invoice_check.py); every other step's '[ok]' line is read into a
table of the same shape (automation/reporting/step_tables.py). Each table is drawn with
only its failing rows red, and the mismatches are listed red under Failed At; the
segment keeps the status the run recorded.

What a run saw that did NOT fail it -- the app crashed and was relaunched, a spinner
stayed up, an error toast, a slow screen or step, a known app bug worked around -- is
listed in yellow under Failed At, after any red (automation/scenarios/run_issues.py),
and counted in the header's Warnings.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from automation import formulas
from automation.reporting import invoice_section
from automation.reporting.step_tables import prepost, run_context, steps_table, tables_of
from automation.scenarios.invoice_check import (MISMATCH_PREFIX, PRINTED_PAYMENTS_NOTE,
                                                is_printed_payments_gap)
from automation.scenarios.run_issues import issues_of

# ── HTML shell ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__TITLE__</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f0f2f5; padding: 30px; color: #333;
    }
    .card {
      max-width: 1500px; margin: 0 auto; background: #fff;
      border-radius: 12px; box-shadow: 0 2px 16px rgba(0,0,0,.08); overflow: hidden;
    }
    .header { background: #1a202c; color: #fff; padding: 24px 32px; position: relative; }
    .header h1 { font-size: 24px; font-weight: 700; }
    .header p  { font-size: 15px; opacity: .8; margin-top: 4px; }
    .printbtn {
      position: absolute; top: 24px; right: 32px; background: #805ad5; color: #fff;
      border: 0; border-radius: 8px; padding: 10px 18px; font-size: 13px;
      font-weight: 600; cursor: pointer;
    }
    .printbtn:hover { background: #6b46c1; }
    .stats { display: flex; border-bottom: 2px solid #e2e8f0; }
    .stat { flex: 1; padding: 18px; text-align: center; border-right: 1px solid #e2e8f0; }
    .stat:last-child { border-right: none; }
    .stat .val { font-size: 28px; font-weight: 800; }
    .stat .lbl { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }
    table { width: 100%; border-collapse: collapse; }
    /* Fixed columns, as in the sample reports: a wide calculation table wraps inside its
       column instead of pushing 'Failed At' off the page. */
    table.main { table-layout: fixed; }
    .td-bill table td, .td-bill table th { overflow-wrap: break-word; }
    thead th {
      background: #f7fafc; padding: 16px 20px; font-size: 16px; font-weight: 700;
      color: #333; border-bottom: 2px solid #e2e8f0; text-align: center;
    }
    thead th:first-child { text-align: left; }
    td { padding: 14px 20px; border-bottom: 1px solid #edf2f7; font-size: 14px; vertical-align: top; }
    .td-c { text-align: center; }
    .td-bill { padding: 16px 20px; font-size: 15px; line-height: 2.0; text-align: center; }
    .muted { color: #999; }
    tr:hover td { background: #f7fafc; }
    /* Long words ('@invoice_verdict', amounts) wrap inside their column, never over the
       next one (the user's PDF, 2026-11-05: Failed At ran over the Status column). */
    td { overflow-wrap: break-word; }
    td.failed-at { overflow-wrap: anywhere; }
    @page { size: A4 landscape; margin: 10mm; }
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    @media print {
      body { background: #fff; padding: 0; }
      .card { box-shadow: none; border-radius: 0; max-width: none; }
      .printbtn { display: none; }
      tr:hover td { background: transparent; }
      thead th { padding: 8px 6px; font-size: 12px; }
      td { padding: 8px 6px; font-size: 11px; }
      .td-bill { padding: 8px 6px; font-size: 11px; line-height: 1.5; }
      .td-bill table td, .td-bill table th { padding: 3px 4px !important; font-size: 10px !important; }
      .stat .val { font-size: 20px; }
      tr { page-break-inside: auto; }
      .td-bill table { page-break-inside: avoid; }
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <button class="printbtn" onclick="window.print()">Print / Save PDF</button>
      <h1>__TITLE__</h1>
      <p>__SUBTITLE__</p>
    </div>

    <div class="stats">
      <div class="stat"><div class="val">__TOTAL__</div><div class="lbl">Total</div></div>
      <div class="stat"><div class="val" style="color:#27ae60">__PASSED__</div><div class="lbl">Passed</div></div>
      <div class="stat"><div class="val" style="color:#e74c3c">__FAILED__</div><div class="lbl">Failed</div></div>
      <div class="stat"><div class="val" style="color:#b7791f">__WARNINGS__</div><div class="lbl">Warnings</div></div>
      <div class="stat"><div class="val" style="color:#3182ce">__DURATION__</div><div class="lbl">Duration</div></div>
      <div class="stat"><div class="val" style="color:#805ad5">__PASS_RATE__%</div><div class="lbl">Pass Rate</div></div>
    </div>

    __SUMMARY__
    <table class="main">
      <colgroup>
        <col style="width:12%"><col style="width:9%"><col style="width:9%">
        <col style="width:52%"><col style="width:18%">
      </colgroup>
      <thead>
        <tr>
          <th>Test Case</th>
          <th>App</th>
          <th>Status</th>
          <th>__VALIDATION_HEADER__</th>
          <th>Failed At</th>
        </tr>
      </thead>
      <tbody>
        __ROWS__
      </tbody>
    </table>
  </div>
</body>
</html>"""


# ── Small helpers ──────────────────────────────────────────────────────────
def _esc(text: Any) -> str:
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _status_badge(status: str) -> str:
    base = ("display:inline-block;padding:4px 10px;border-radius:20px;color:#fff;"
            "font-size:11px;font-weight:700;letter-spacing:.5px;white-space:nowrap")
    if status == "PASS":
        return f'<span style="background:#27ae60;{base}">PASSED</span>'
    if status == "FAIL":
        return f'<span style="background:#e74c3c;{base}">FAILED</span>'
    return f'<span style="background:#f39c12;{base}">SKIPPED</span>'


def _check_icon() -> str:
    return '<span style="color:#27ae60;font-size:16px;font-weight:bold">&#10003;</span>'


def _cross_icon() -> str:
    return '<span style="color:#e74c3c;font-size:16px;font-weight:bold">&#10007;</span>'


# ── Reason-string parsing → VAT table sections ─────────────────────────────
def _extract_bill_check_sections(reasons: List[str]) -> List[Dict[str, Any]]:
    """Split reasons into one section per 'Bill check' payload (Pre, Post, …)
    so each gets its own VAT table in the validation cell."""
    sections: List[Dict[str, Any]] = []
    seen_payloads = set()
    for r in (reasons or []):
        if not r or 'Bill check' not in r:
            continue
        payload = r.split('Bill check', 1)[1]
        if payload in seen_payloads:
            continue
        seen_payloads.add(payload)
        section: Dict[str, Any] = {'vat_rows': []}

        m = re.search(
            r'Items\s*sum\s*mismatch:\s*displayed\s*=\s*€?([\d.]+),\s*calculated\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            r, re.IGNORECASE)
        if m:
            section['items_sum'] = {'displayed': m.group(1), 'calculated': m.group(2),
                                    'diff': m.group(3), 'pass': False}

        # Failing VAT rows: "VAT 3% mismatch: expected 0.03/100 × €6.96 = €0.21, displayed = €0.20, diff = €0.01"
        for vm in re.finditer(
            r'VAT\s+([\d.]+)%\s*mismatch:\s*expected\s+[\d.]+/100\s*[×x*]\s*€?([\d.]+)\s*=\s*€?([\d.]+),\s*displayed\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            r):
            section['vat_rows'].append({
                'rate': vm.group(1), 'calc_excl': vm.group(2), 'disp_excl': vm.group(2),
                'calc_vat': vm.group(3), 'disp_vat': vm.group(4),
                'ati': f"{float(vm.group(2)) + float(vm.group(4)):.2f}",
                'pass': False, 'diff': vm.group(5),
            })
        # Passing VAT rows: "VAT 14% (0.14/100 × €46.50 = €6.51) = displayed €6.51 ✓"
        for vm in re.finditer(
            r'VAT\s+([\d.]+)%\s*\(\s*[\d.]+/100\s*[×x*]\s*€?([\d.]+)\s*=\s*€?([\d.]+)\s*\)\s*=\s*displayed\s*€?([\d.]+)\s*✓',
            r):
            rate = vm.group(1)
            if not any(vr['rate'] == rate for vr in section['vat_rows']):
                section['vat_rows'].append({
                    'rate': rate, 'calc_excl': vm.group(2), 'disp_excl': vm.group(2),
                    'calc_vat': vm.group(3), 'disp_vat': vm.group(4),
                    'ati': f"{float(vm.group(2)) + float(vm.group(4)):.2f}",
                    'pass': True, 'diff': '0.00',
                })

        m = re.search(
            r'Total\s*breakdown\s*mismatch:\s*sum\s*Excl\.?Tax\s*\(\s*€?([\d.]+)\s*\)\s*\+\s*sum\s*VAT\s*\(\s*€?([\d.]+)\s*\)\s*=\s*€?([\d.]+),\s*displayed\s*Total\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            r, re.IGNORECASE)
        if m:
            section['total_breakdown'] = {
                'sum_excl': m.group(1), 'sum_vat': m.group(2), 'calculated': m.group(3),
                'displayed': m.group(4), 'diff': m.group(5), 'pass': False,
            }
        # A plain '[ok] Bill check: total €…, paid €…' (the B-App payment step) carries
        # none of these: it is drawn from its '[calc]' table instead, not as an empty box.
        if section['vat_rows'] or section.get('items_sum') or section.get('total_breakdown'):
            sections.append(section)

    for i, s in enumerate(sections):
        s['label'] = ('Pre-Payment Bill Check' if i == 0 else
                      'Post-Payment Bill Check' if i == 1 else f'Bill Check #{i + 1}')
    return sections


def _render_vat_table(vat_rows: List[Dict[str, Any]]) -> str:
    if not vat_rows:
        return ''
    html = (
        '<table style="margin:6px auto;border-collapse:collapse;font-size:13px;text-align:right">'
        '<tr style="border-bottom:1px solid #ddd">'
        '<th style="text-align:left;padding:4px 10px">Rate</th>'
        '<th style="padding:4px 10px">Calc Excl</th>'
        '<th style="padding:4px 10px">Disp Excl</th>'
        '<th style="padding:4px 10px">Calc VAT</th>'
        '<th style="padding:4px 10px">Disp VAT</th>'
        '<th style="padding:4px 10px">ATI</th>'
        '<th style="padding:4px 10px">Status</th>'
        '</tr>')
    for vr in vat_rows:
        row_pass = vr.get('pass', True)
        row_color = '#333' if row_pass else '#e74c3c'
        diff_val = float(vr.get('diff', '0') or 0)
        disp_excl_html = f'&euro;{vr["disp_excl"]}'
        disp_vat_html = f'&euro;{vr["disp_vat"]}'
        if not row_pass and diff_val > 0:
            disp_vat_html += f'<br><span style="color:#e74c3c;font-size:11px">diff +{diff_val:.2f}</span>'
            disp_excl_html += f'<br><span style="color:#e74c3c;font-size:11px">diff -{diff_val:.2f}</span>'
        icon = _check_icon() if row_pass else _cross_icon()
        html += (
            f'<tr style="color:{row_color};border-bottom:1px solid #f0f0f0">'
            f'<td style="text-align:left;padding:4px 10px;font-weight:600">VAT {vr["rate"]}%</td>'
            f'<td style="padding:4px 10px">&euro;{vr["calc_excl"]}</td>'
            f'<td style="padding:4px 10px">{disp_excl_html}</td>'
            f'<td style="padding:4px 10px">&euro;{vr["calc_vat"]}</td>'
            f'<td style="padding:4px 10px">{disp_vat_html}</td>'
            f'<td style="padding:4px 10px">&euro;{vr["ati"]}</td>'
            f'<td style="padding:4px 10px">{icon}</td>'
            f'</tr>')
    return html + '</table>'


_CELL = 'border:1px solid #ccc;padding:4px 7px;text-align:center'
_NOWRAP = ';white-space:nowrap'


# ── How a table reads for the people the report goes to ─────────────────────────────
# The user, 2026-11-05, of segment 17's tables ('Coupon 10% (host)', 'Tip 2.00 (host)'):
# "this is a perfect report ... headings on middle ... names like this, understandable ...
# for higher officials". Every table is drawn that way: a short centred title 'Name (who)',
# short column names, plain row labels, the numbers alone in the cells; where a figure
# came from goes in one grey line under the table. Applied when drawn, so every run's
# report reads the same; the sample reports' own names stay (Pre-payment Total, ATI Sum,
# Direct Split, Per-member split check, …).
_TITLES = [
    (r"^Pre-payment (Total|VAT) — (\w+)( \(from the (\w+)'s invoice, Pre-Order block\))?$",
     lambda m: f"Pre-payment {m.group(1)} ({m.group(2).lower()})",
     lambda m: f"Source: the {m.group(4)}'s invoice, Pre-Order section." if m.group(3) else ""),
    (r"^Post-payment (Total|VAT) — (\w+) \(the (\w+)'s invoice, Pre-Order block\)$",
     lambda m: f"Post-payment {m.group(1)} ({m.group(2).lower()})",
     lambda m: f"Source: the {m.group(3)}'s invoice, Pre-Order section."),
    (r"^Post-payment (Total|VAT) — every diner's pre-order \(the event invoice, Pre-Order block\)$",
     lambda m: f"Post-payment {m.group(1)} (all diners)",
     lambda m: "Source: the event invoice, Pre-Order section."),
    (r"^ATI Sum — every diner's pre-order$", lambda m: "ATI Sum (all diners)", None),
    (r"^ATI Sum — (\w+)$", lambda m: f"ATI Sum ({m.group(1).lower()})", None),
    (r"^Pre vs Post VAT Comparison — every diner's pre-order$",
     lambda m: "Pre vs Post VAT (all diners)", None),
    (r"^Pre vs Post VAT Comparison — (\w+)$", lambda m: f"Pre vs Post VAT ({m.group(1).lower()})", None),
    (r"^Pre vs Post — every diner's pre-order$", lambda m: "Pre vs Post (all diners)", None),
    (r"^Pre vs Post — (\w+)$", lambda m: f"Pre vs Post ({m.group(1).lower()})", None),
    (r"^Pre-payments of every diner$", lambda m: "Pre-payments (all diners)", None),
    (r"^Added by the waiter — amount and each person's share$", lambda m: "Items Added (waiter)", None),
    (r"^Comps$", lambda m: "Comps (waiter)", None),
    (r"^Comps — Final Summary$", lambda m: "Comps Summary", None),
    (r"^Payment by E-Payment \((.+)\)$", lambda m: f"E-Payment ({m.group(1)})", None),
    (r"^Payment by Cash \((.+)\)$", lambda m: f"Cash Payment ({m.group(1)})", None),
    (r"^Payment by Food Voucher \((.+)\)$", lambda m: f"Food Voucher Payment ({m.group(1)})", None),
    (r"^Event invoice \(iPad\) — (Pre-Order|On Premises): (totals|VAT)$",
     lambda m: f"Event Invoice {'Totals' if m.group(2) == 'totals' else 'VAT'} "
               f"({m.group(1).lower().replace('-', ' ')})", None),
    (r"^Event invoice \(iPad\) — whole invoice$", lambda m: "Event Invoice Summary", None),
    (r"^Invoice of the (\w+) \(phone\) — (Pre-Order|On Premises): (totals|VAT)$",
     lambda m: f"{m.group(1).title()} Invoice {'Totals' if m.group(3) == 'totals' else 'VAT'} "
               f"({m.group(2).lower().replace('-', ' ')})", None),
    (r"^Invoice of the (\w+) \(phone\) — whole invoice$",
     lambda m: f"{m.group(1).title()} Invoice Summary", None),
    (r"^Calculation checks of the whole run$", lambda m: "All Calculations", None),
]
_COLUMNS = {
    "Excl. Tax + VAT": "Excl. + VAT", "Most VAT at the rate": "Max VAT",
    "Pre-Payment (charged)": "Pre-payment", "Post-Payment (invoice)": "Post-payment",
    "Post VAT (invoice)": "Post VAT", "Δ VAT": "Diff",
    "Assign / Split": "Type", "To": "Guests", "Added to the order": "Amount",
    "Each person": "Per guest", "Units comped": "Units", "Comp": "Comp %",
    "Expected line after": "Expected after", "Displayed line after": "Displayed after",
    "Comped line should read": "Comped line", "Tendered": "Paid",
    "Confirm Payment": "Result", "How": "Calculation", "Block": "Section",
    "Total displayed": "Total shown",
}
_ROWS = {
    "grand total": "Grand total", "dishes": "Dishes", "payments": "Payment lines",
    "against the payments": "Paid in the run", "Tip against the tips given": "Tips",
    "discount": "Discount", "All blocks": "Whole invoice", "Cart Total (displayed)": "Cart total",
    "Calculated Total (the VAT rows' ATI)": "Calculated total",
    "Calculated Total (the VAT rows' ATI + tip)": "Calculated total (with tip)",
    "settled": "Settled",
}
_NOTE_TAILS = [   # explanations moved out of a cell into the note under its table
    "; " + PRINTED_PAYMENTS_NOTE,
]


def _presentable(table: Dict[str, Any]) -> Dict[str, Any]:
    """*table* as the report shows it (see _TITLES): its title, columns and row labels in
    plain words, explanations in table['note']."""
    t = dict(table)
    notes = [table.get("note")] if table.get("note") else []
    title = str(t.get("t") or "")
    for pat, name, note in _TITLES:
        m = re.match(pat, title)
        if m:
            t["t"] = name(m)
            extra = note(m) if note else ""
            if extra:
                notes.append(extra)
            break
    pre_src = next((h for h in (t.get("h") or []) if str(h).startswith("Pre VAT (")), None)
    if pre_src:
        notes.append(f"Pre VAT: from the {pre_src[len('Pre VAT ('):-1]}.")
    t["h"] = ["Pre VAT" if h == pre_src else _COLUMNS.get(h, h) for h in (t.get("h") or [])]
    if "Max VAT" in t["h"]:
        notes.append("Max VAT: the most VAT the rate allows on that ATI (size prices carry "
                     "none, so a VAT row may be lower, never higher).")
    rows = []
    for r in t.get("r") or []:
        if not isinstance(r, list) or not r:
            rows.append(r)
            continue
        r = list(r)
        if isinstance(r[0], str):
            r[0] = _ROWS.get(r[0], r[0])
        for i, c in enumerate(r):
            if isinstance(c, str):
                for tail in _NOTE_TAILS:
                    if tail in c:
                        r[i] = c.replace(tail, "")
                        line = ("Payment lines: only the diners' own payments are printed, not "
                                "those booked on guests (paid for them by another diner, or "
                                "their own cash).")
                        if line not in notes:
                            notes.append(line)
                if i and r[i] == "settled":
                    r[i] = "Settled"
        rows.append(r)
    t["r"] = rows
    t["note"] = " ".join(n for n in notes if n)
    return t


def _render_calc_table(table: Dict[str, Any]) -> str:
    """One '[calc]' table: its title, its head, a row per sum. A row's last value is
    True / False / None (matches, does not, information); only the False rows are red."""
    table = _presentable(table)
    rows = [r for r in (table.get('r') or []) if isinstance(r, list) and r]
    # 'plain': no Status column (a wrong row is still red); 'sc': only the Status cell is
    # coloured; 'cap': the title is a caption with 'b' in bold -- the split sample's
    # 'Per-member split check — <b>Item</b> split 2 ways'.
    plain, status_only = bool(table.get('plain')), bool(table.get('sc'))
    graded = not plain and any(isinstance(r[-1], bool) for r in rows)
    head = list(table.get('h') or []) + (['Status'] if graded else [])
    title = _esc(table.get("t"))
    bold = _esc(table.get('b') or '')
    if bold and bold in title:
        title = title.replace(bold, f'<b>{bold}</b>', 1)
    html = ((f'<div style="font-size:13px;text-align:center;margin:14px 0 4px;'
             f'color:#334155">{title}</div>') if table.get('cap') else
            (f'<div style="font-weight:700;color:#1a202c;margin:14px 0 6px;'
             f'border-top:1px solid #cbd5e1;padding-top:10px">{title}</div>'))
    html += ('<table style="border-collapse:collapse;font-size:13px;margin:0 auto 16px">'
             '<tr>' + ''.join(f'<th style="{_CELL};background:#f7f7f7;font-weight:600;'
                              f'">{_esc(h)}</th>' for h in head) + '</tr>')
    head0 = list(table.get('h') or [])
    for r in rows:
        status = r[-1] if isinstance(r[-1], bool) or r[-1] is None else None
        cells = r[:-1] if (isinstance(r[-1], bool) or r[-1] is None) else r
        if "Displayed" in head0 and status is None:
            # 'dishes' on an invoice that prints no dish total: nothing to compare.
            at = head0.index("Displayed")
            cells = [("not printed on this invoice" if i == at else "no check")
                     if c in ("—", "-") and i >= at else c for i, c in enumerate(cells)]
        color = '#e74c3c' if status is False else '#15803d' if status else '#333'
        if status_only or (plain and status):
            color = '#333'
        weight = ';font-weight:600' if status is False and not status_only else ''
        html += f'<tr style="color:{color}{weight}">' + ''.join(
            f'<td style="{_CELL}{_NOWRAP if len(str(c)) <= 14 else ""}">{_esc(c)}</td>'
            for c in cells)
        if graded:
            mark = ('PASS &#10003;' if status else 'FAIL &#10007;' if status is False else '')
            mark_color = ('#15803d' if status else '#b91c1c' if status is False else '#333')
            html += (f'<td style="{_CELL}{_NOWRAP};color:{mark_color}">{mark}</td>'
                     if status_only else f'<td style="{_CELL}{_NOWRAP}">{mark}</td>')
        html += '</tr>'
    html += '</table>'
    if table.get('note'):
        html += (f'<div style="font-size:12px;color:#718096;text-align:center;margin:-10px auto '
                 f'14px;max-width:90%;line-height:1.5">{_esc(table["note"])}</div>')
    return html


# ── The one invoice, element for element as the user's invoice sample ─────────────
# (invoice_section.py; the sample's 'Event PDF vs Stored Payments' / 'Individual PDF vs
# RoopaD', 2026-11-05: "see how structure this pdf report is ... create a invoice like
# this structured"). Its styles are the sample's own.
_IN_HEAD = ('margin:10px 0 4px;font-size:15px;font-weight:700;color:#1a202c;'
            'border-bottom:2px solid #e2e8f0;padding-bottom:4px')
_IN_TABLE = ('margin:6px auto;border-collapse:collapse;font-size:13px;text-align:center;'
             'border:1px solid #ccc')
_IN_TH = 'padding:5px 10px;white-space:nowrap;border:1px solid #ccc'
_IN_TD = 'padding:5px 10px;border:1px solid #ccc'


def _in_mark(ok: Optional[bool]) -> str:
    """The sample's marks: a green tick, a red cross, a grey NA (here 'info')."""
    if ok is True:
        return '<span style="color:#27ae60;font-size:16px;font-weight:bold">&#10003;</span>'
    if ok is False:
        return '<span style="color:#e74c3c;font-size:16px;font-weight:bold">&#10007;</span>'
    return '<span style="color:#95a5a6">info</span>'


def _in_eur(v: Optional[float]) -> str:
    return '&mdash;' if v is None else f'&euro;{v:.2f}'


def _in_diff(a: Optional[float], b: Optional[float]) -> str:
    if a is None or b is None:
        return ''
    d = formulas.diff(b, a)
    color = '#27ae60' if formulas.same(a, b) else '#e74c3c'
    return f'<span style="color:{color};font-weight:600">diff &euro;{d:+.2f}</span>'


def _in_money(cell: Any) -> str:
    try:
        return f'&euro;{float(str(cell).replace("€", "")):.2f}'
    except ValueError:
        return _esc(cell)


def _in_calc(a: Any, b: Any, sign: int) -> str:
    """a + b (sign 1) or a - b (sign -1), from the invoice's own cells."""
    try:
        x = float(str(a).replace("€", "")) + sign * float(str(b).replace("€", ""))
    except ValueError:
        return '&mdash;'
    return f'&euro;{x:.2f}'


def _render_invoice(s: Dict[str, Any]) -> str:
    out = [f'<div style="{_IN_HEAD}">{_esc(s["title"])}</div>']
    t = s["total"]
    stored = (_in_eur(t["stored"]) if t["stored"] is not None
              else 'N/A (the run’s payments are not known)')
    out.append(f'<b>Invoice Total vs Stored Total:</b> {_in_mark(t["ok"])} Invoice = '
               f'{_in_eur(t["invoice"])} | Stored = {stored} | '
               f'{_in_diff(t["invoice"], t["stored"])}<br>')

    pr = s["printed"]
    out.append(f'<b>Paid for (Invoice):</b> '
               f'{_in_mark(False if pr["over"] else None if pr["short"] else True)}<br>')
    if s["paid_for"]:
        out.append(f'&nbsp;&nbsp;Expected {s["guests"]}: ' + ', '.join(
            f'{_esc(others)} (by {_esc(who.replace(" (participant)", ", participant"))})'
            for others, who in s["paid_for"]) + '<br>')
    else:
        out.append('&nbsp;&nbsp;Expected 0: none (paid own share only)<br>')
    if pr["total"] is None:
        out.append('&nbsp;&nbsp;Invoice shows: no payment lines<br>')
    elif pr["short"]:
        out.append(f'&nbsp;&nbsp;Invoice shows: payment lines {_in_eur(pr["total"])} of '
                   f'{_in_eur(pr["of"])} — not printed: {_in_eur(round(pr["of"] - pr["total"], 2))} '
                   f'booked on guests (paid for them by another diner, or their own cash)<br>')
    else:
        out.append(f'&nbsp;&nbsp;Invoice shows: payment lines {_in_eur(pr["total"])} of '
                   f'{_in_eur(pr["of"])}<br>')

    if s["vat"]:
        ok = False if any(r[-1] is False for _, rows in s["vat"] for r in rows) else True
        out.append(f'<b>VAT (Invoice vs Calculated):</b> {_in_mark(ok)}<br>')
        head = ["Rate", "Invoice Excl", "Calc Excl", "Invoice VAT", "Calc VAT", "Invoice ATI",
                "Calc ATI", "Status"]
        html = (f'<table style="{_IN_TABLE}"><tr style="background:#f7f7f7">'
                + ''.join(f'<th style="{_IN_TH}">{h}</th>' for h in head) + '</tr>')
        for part, rows in s["vat"]:
            if len(s["vat"]) > 1:
                html += (f'<tr><td colspan="{len(head)}" style="{_IN_TD};font-weight:700;'
                         f'background:#fbfbfb">{_esc(part)}</td></tr>')
            for r in rows:
                rate, excl, vat, ati = r[0], r[1], r[2], r[3]
                color = '#e74c3c' if r[-1] is False else '#333'
                cells = [_in_money(excl), _in_calc(ati, vat, -1), _in_money(vat),
                         _in_calc(ati, excl, -1), _in_money(ati), _in_calc(excl, vat, 1)]
                html += (f'<tr style="color:{color}"><td style="{_IN_TD};font-weight:600;'
                         f'white-space:nowrap">{_esc(rate)}</td>'
                         + ''.join(f'<td style="{_IN_TD}">{c}</td>' for c in cells)
                         + f'<td style="{_IN_TD}">{_in_mark(r[-1])}</td></tr>')
        out.append(html + '</table><br>')

    if s["lines"]:
        ok = (False if any(r[-1] is False for r in s["lines"])
              else True if all(r[-1] for r in s["lines"]) else None)
        out.append(f'<b>Invoice lines (Invoice vs Stored):</b> {_in_mark(ok)}<br>')
        head = ["Item", "Qty", "Invoice", "Stored", "Diff", "Status"]
        html = (f'<table style="{_IN_TABLE}"><tr style="background:#f7f7f7">'
                + ''.join(f'<th style="{_IN_TH}">{h}</th>' for h in head) + '</tr>')
        for item, qty, inv, paid, ok_row in s["lines"]:
            diff = (f'{round(inv - paid, 2) + 0.0:+.2f}' if inv is not None and paid is not None
                    else '&mdash;')
            html += (f'<tr style="color:{"#e74c3c" if ok_row is False else "#333"}">'
                     f'<td style="{_IN_TD};text-align:left">{_esc(item)}</td>'
                     f'<td style="{_IN_TD}">{_esc(qty)}</td><td style="{_IN_TD}">{_in_eur(inv)}</td>'
                     f'<td style="{_IN_TD}">{_in_eur(paid)}</td><td style="{_IN_TD}">{diff}</td>'
                     f'<td style="{_IN_TD}">{_in_mark(ok_row)}</td></tr>')
        out.append(html + '</table><br>')

    ad = s["adds"]
    out.append(f'<b>Invoice adds up (VAT rows + tip):</b> {_in_mark(ad["ok"])} '
               f'{_in_eur(ad["calc"])} vs total {_in_eur(ad["total"])}<br>')

    tip = s["tip"]
    out.append(f'<b>Tip:</b> {_in_mark(tip["ok"])} Invoice = {_in_eur(tip["invoice"])} | '
               f'Stored = {_in_eur(tip["stored"])} | {_in_diff(tip["invoice"], tip["stored"])}<br>')

    o = s["outstanding"]
    due = (f'{_in_eur(o["due"])} Due (change owed back)' if o["due"] else '&euro;0.00')
    out.append(f'<b>Outstanding:</b> {_in_mark(o["ok"])} Invoice = {due} | Stored = &euro;0.00 |'
               + (' the table closed' if o["closed"] else '') + '<br>')

    def status(ok: Optional[bool]) -> str:
        word, color = (('PASS', '#27ae60') if ok else ('FAIL', '#e74c3c') if ok is False
                       else ('INFO', '#95a5a6'))
        return f'<td style="{_IN_TD};color:{color};font-weight:700">{word}</td>'

    paid_ok = (None if pr["total"] is None or t["stored"] is None
               else True if formulas.same(pr["total"], t["stored"])
               else None if pr["total"] < t["stored"] else False)
    rows = [("Total", _in_eur(t["invoice"]), _in_eur(t["stored"]), t["ok"]),
            ("Tip", _in_eur(tip["invoice"]), _in_eur(tip["stored"]), tip["ok"]),
            ("Total Paid", _in_eur(pr["total"]) + (' (own payments only)' if pr["short"] else ''),
             _in_eur(t["stored"]), paid_ok),
            ("Outstanding", due, '&euro;0.00', o["ok"])]
    out.append(f'<b>Invoice vs Stored Payments:</b> '
               f'{_in_mark(False if any(r[-1] is False for r in rows) else True)}')
    out.append(f'<table style="{_IN_TABLE}"><tr>'
               + ''.join(f'<th style="{_IN_TH};background:#f7f7f7">{h}</th>'
                         for h in ("Field", "Invoice", "Stored", "Status")) + '</tr>'
               + ''.join(f'<tr><td style="{_IN_TD}">{f}</td><td style="{_IN_TD}">{a}</td>'
                         f'<td style="{_IN_TD}">{b}</td>{status(ok)}</tr>'
                         for f, a, b, ok in rows) + '</table>')
    return ''.join(out)


def _mismatches(record: Dict[str, Any]) -> List[str]:
    """The sums of this segment that did not match, each once, in the order found."""
    lines = [str(r)[len(MISMATCH_PREFIX):] for r in (record.get('reasons') or [])
             if str(r).startswith(MISMATCH_PREFIX) and not is_printed_payments_gap(r)]
    return list(dict.fromkeys(lines))


def _all_reasons(record: Dict[str, Any]) -> List[str]:
    return ((record.get('consumer_reasons') or []) + (record.get('business_reasons') or [])
            + (record.get('reasons') or []))


def _record_tables(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The row's tables: its pre-payment and post-payment ones together first (set by
    render_run_report from step_tables.prepost), then its own, less those they replace
    or that moved to the pre-order row."""
    extra, drop, prefixes = record.get("_prepost") or ([], set(), ())
    own = [t for t in tables_of(_all_reasons(record), record.get("_context"))
           if t.get("t") not in drop and not str(t.get("t", "")).startswith(prefixes)]
    return list(extra) + own


def _has_calculation(record: Dict[str, Any]) -> bool:
    return bool(record.get("_invoice") or _record_tables(record)
                or _extract_bill_check_sections(_all_reasons(record)))


def _build_validation_cell(record: Dict[str, Any]) -> str:
    all_reasons = _all_reasons(record)
    sections = _extract_bill_check_sections(all_reasons)
    tables = _record_tables(record)
    invoice = record.get("_invoice")
    if not sections and not tables and not invoice:
        # No sums in this segment: what each step checked, so no cell is left empty (the
        # user, 2026-11-05: "please must fill all details without skipping").
        steps = steps_table(all_reasons)
        return _render_calc_table(steps) if steps else '<span style="color:#999">&mdash;</span>'

    blocks = [''.join(_render_calc_table(t) for t in tables)] if tables else []
    if invoice:
        blocks.append(_render_invoice(invoice))
    for s in sections:
        lines = [f'<div style="font-weight:700;color:#1a202c;margin:10px 0 4px 0;font-size:14px">{s["label"]}</div>']
        if s.get('items_sum'):
            it = s['items_sum']
            lines.append(
                f'<b>Items Total:</b> {_cross_icon()} displayed &euro;{it["displayed"]} '
                f'&nbsp;|&nbsp; calculated &euro;{it["calculated"]} &nbsp;|&nbsp; diff &euro;{it["diff"]}')
        vat_rows = s.get('vat_rows') or []
        if vat_rows:
            ok = all(vr.get('pass') for vr in vat_rows)
            lines.append(f'<b>VAT:</b> {_check_icon() if ok else _cross_icon()}')
            lines.append(_render_vat_table(vat_rows))
        if s.get('total_breakdown'):
            tb = s['total_breakdown']
            lines.append(
                f'<b>Total Breakdown:</b> {_cross_icon()} calculated &euro;{tb["calculated"]} '
                f'&nbsp;|&nbsp; displayed &euro;{tb["displayed"]} &nbsp;|&nbsp; diff &euro;{tb["diff"]}')
        blocks.append('<br>'.join(lines))
    return '<hr style="border:0;border-top:1px dashed #cbd5e0;margin:10px 0">'.join(blocks)


def _format_failed_at_html(err: str) -> str:
    if not err:
        return ""
    lines: List[str] = []
    for idx, section in enumerate(re.split(r';\s*Bill check', err)):
        section_lines: List[str] = []
        label = ("Pre-Payment Bill Check" if idx == 0 and "Bill check" in section else
                 "Post-Payment Bill Check" if idx == 1 else
                 f"Bill Check #{idx + 1}" if idx > 1 else None)

        for m in re.finditer(
            r'Items\s*sum\s*mismatch:\s*displayed\s*=\s*€?([\d.]+),\s*calculated\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            section, re.IGNORECASE):
            section_lines.append(
                f'{_cross_icon()} Items sum mismatch: displayed &euro;{m.group(1)} '
                f'&nbsp;|&nbsp; calculated &euro;{m.group(2)} &nbsp;|&nbsp; diff &euro;{m.group(3)}')
        for m in re.finditer(
            r'VAT\s+([\d.]+)%\s*mismatch:\s*expected\s+[\d.]+/100\s*[×x*]\s*€?[\d.]+\s*=\s*€?([\d.]+),\s*displayed\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            section):
            section_lines.append(
                f'{_cross_icon()} VAT {m.group(1)}% mismatch: expected &euro;{m.group(2)} '
                f'&nbsp;|&nbsp; displayed &euro;{m.group(3)} &nbsp;|&nbsp; diff &euro;{m.group(4)}')
        for m in re.finditer(
            r'Total\s*breakdown\s*mismatch:\s*sum\s*Excl\.?Tax\s*\(\s*€?([\d.]+)\s*\)\s*\+\s*sum\s*VAT\s*\(\s*€?([\d.]+)\s*\)\s*=\s*€?([\d.]+),\s*displayed\s*Total\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            section, re.IGNORECASE):
            section_lines.append(
                f'{_cross_icon()} Total breakdown mismatch: calculated &euro;{m.group(3)} '
                f'&nbsp;|&nbsp; displayed &euro;{m.group(4)} &nbsp;|&nbsp; diff &euro;{m.group(5)}')
        for m in re.finditer(
            r'Bill\s*total\s*mismatch:\s*displayed\s*=\s*€?([\d.]+),\s*calculated\s*=\s*€?([\d.]+),\s*diff\s*=\s*€?([\d.]+)',
            section, re.IGNORECASE):
            section_lines.append(
                f'{_cross_icon()} Bill total mismatch: displayed &euro;{m.group(1)} '
                f'&nbsp;|&nbsp; calculated &euro;{m.group(2)} &nbsp;|&nbsp; diff &euro;{m.group(3)}')

        if section_lines:
            if label:
                lines.append(f'<div style="font-weight:700;color:#1a202c;margin-top:6px">{label}</div>')
            lines.extend(section_lines)

    if "Interrupted by user" in err and not lines:
        lines.append('<span style="color:#888">Interrupted by user</span>')
    if not lines:
        return ""
    return ('<div style="text-align:left;line-height:1.8;font-size:13px;color:#c0392b">'
            + '<br>'.join(lines) + '</div>')


def _warnings(record: Dict[str, Any]) -> List[str]:
    """What this segment saw that did not fail it, each once: 'App crash: …', 'Loading: …'
    (never slowness: run_issues.KINDS)."""
    return [f"{label}: {text}" for label, text in issues_of(record.get('reasons') or [])]


def _build_failed_at_cell(record: Dict[str, Any]) -> str:
    # The sums that did not match: red, one per line, whether or not a step failed too.
    mism = ''.join(f'<div style="color:#e74c3c;font-weight:600;margin-top:8px;line-height:1.5">'
                   f'{i}. {_esc(m)}</div>' for i, m in enumerate(_mismatches(record), 1))
    # Then what did not fail it: yellow, one per line.
    warn = ''.join(f'<div style="color:#b7791f;font-weight:500;margin-top:8px;line-height:1.5">'
                   f'{i}. {_esc(w)}</div>' for i, w in enumerate(_warnings(record), 1))
    if (record.get("status") or "").upper() == "PASS":
        return (mism + warn) or '<span style="color:#27ae60">No failures</span>'
    err = record.get("error", "") or ""
    if not err:
        return (mism + warn) or '<span style="color:#c0392b">Failure</span>'
    structured = _format_failed_at_html(err)
    if structured:
        return mism + structured + warn
    return mism + (f'<span style="font-size:13px">{_esc(err[:400])}'
                   f'{"…" if len(err) > 400 else ""}</span>') + warn


def _app_label(record: Dict[str, Any]) -> str:
    c = record.get("consumer_status", "N/A") not in ("N/A", None, "")
    b = record.get("business_status", "N/A") not in ("N/A", None, "")
    if c and b:
        return "Consumer + Business"
    if c:
        return "Consumer"
    if b:
        return "Business"
    return record.get("app") or "—"


def _screenshot_html(record: Dict[str, Any]) -> str:
    """The app's crash screen (data-URI), small and clickable, under the reason in the
    RICH web report. A run stores a screenshot only when the app crashed (the user,
    2026-11-17), so it is shown whatever the segment's status: a crash the run
    recovered from is evidence too. Callers gate this behind include_screenshots so
    the downloadable report can omit it."""
    shot = (record.get("screenshot") or "").strip()
    if not shot:
        return ""
    if not shot.startswith("data:image"):
        return ""
    return (
        '<div style="margin-top:10px">'
        '<div style="color:#888;font-size:12px;margin-bottom:4px">Screen when the app crashed:</div>'
        f'<a href="{shot}" target="_blank" rel="noreferrer">'
        f'<img src="{shot}" alt="screen when the app crashed" '
        'style="max-width:260px;max-height:360px;border:1px solid #e2e8f0;border-radius:8px;'
        'box-shadow:0 2px 8px rgba(0,0,0,0.12)" /></a>'
        '<div style="color:#a0aec0;font-size:11px;margin-top:3px">click to enlarge</div>'
        '</div>')


def _build_row(record: Dict[str, Any], index: int, include_screenshots: bool = True) -> str:
    bg = "#ffffff" if index % 2 == 0 else "#f8fafc"
    # 'Waiter: quantities +1 / -1, remove a product' -> the role in bold, what the
    # segment does in grey under it (the sample reports' test-case column).
    who, sep, what = (record.get("name") or "Unknown").partition(": ")
    name = _esc(who) + (f'<div style="font-weight:400;font-size:13px;color:#718096;'
                        f'margin-top:4px;line-height:1.5">{_esc(what)}</div>' if sep else "")
    num = _esc(record.get("num", ""))
    test_case = f"{num}. {name}" if num else name

    launch_time = record.get("launch_time")
    if isinstance(launch_time, (int, float)):
        test_case += f'<br><span style="color:#888;font-size:12px">Execution: {launch_time:.1f}s</span>'

    failed_cell = _build_failed_at_cell(record)
    if include_screenshots:
        failed_cell += _screenshot_html(record)

    return (
        f'<tr style="background:{bg}">'
        f'<td style="padding:14px 20px;border-bottom:1px solid #edf2f7;font-size:15px;'
        f'font-weight:500;vertical-align:top;line-height:1.8">{test_case}</td>'
        f'<td class="td-c" style="font-size:15px">{_esc(_app_label(record))}</td>'
        f'<td class="td-c">{_status_badge(record.get("status", "PASS"))}</td>'
        f'<td class="td-bill">{_build_validation_cell(record)}</td>'
        f'<td class="failed-at" style="padding:14px 20px;border-bottom:1px solid #edf2f7;'
        f'color:#c0392b;font-size:15px;font-weight:500;vertical-align:top">{failed_cell}</td>'
        f'</tr>')


# ── Public API ─────────────────────────────────────────────────────────────
def records_from_rows(rows: List[Any]) -> List[Dict[str, Any]]:
    """Map ORM ``ScenarioResult`` rows to the record dicts this renderer wants.

    The platform stores one merged ``reasons`` list (+ ``error``); the renderer's
    validation cell unions consumer/business/reasons, so putting everything in
    ``reasons`` is correct.
    """
    records = []
    for r in rows:
        records.append({
            "num": getattr(r, "scenario_num", "") or "",
            "name": getattr(r, "scenario_name", "") or "Unknown",
            "status": (getattr(r, "status", "") or "PASS").upper(),
            "consumer_status": getattr(r, "consumer_status", "N/A") or "N/A",
            "business_status": getattr(r, "business_status", "N/A") or "N/A",
            "reasons": list(getattr(r, "reasons", None) or []),
            "consumer_reasons": [],
            "business_reasons": [],
            "error": getattr(r, "error", "") or "",
            "launch_time": getattr(r, "launch_time", None),
            "screenshot": getattr(r, "screenshot", None) or "",
        })
    return records


_LOAD_RE = re.compile(r"^\[load\] (?P<screen>.+?) — (?:(?P<secs>\d+(?:\.\d+)?)s"
                      r"|(?P<never>loads indefinitely)\b.*)$")
LOAD_LIMIT_S = 1.0          # each screen should load in 1 s or less (the user, 2026-11-17)


def _loads_html(records: List[Dict[str, Any]]) -> str:
    """'Screen load times': every timed screen that took MORE than a second -- its PURE
    loading time, from the tap that opened it to the first screen read that showed it
    (the user, 2026-11-17: "dont include waits while calculating loading - just pure
    loading time"). Runs from before the timing have no such notes: nothing is drawn."""
    timed, slow = 0, []
    for rec in records:
        notes = (rec.get("reasons") or []) + (rec.get("consumer_reasons") or []) \
            + (rec.get("business_reasons") or [])
        for note in notes:
            m = _LOAD_RE.match(str(note).strip())
            if not m:
                continue
            timed += 1
            secs = None if m.group("never") else float(m.group("secs"))
            if secs is None or secs > LOAD_LIMIT_S:
                slow.append((rec.get("num") or "", rec.get("name") or "", m.group("screen"), secs))
    if not timed:
        return ""
    head = ('<div style="margin:18px 0 6px;font-size:15px;font-weight:700;color:#1a202c">'
            'Screen load times</div>')
    if not slow:
        return (head + f'<div style="color:#15803d;font-size:13px">All {timed} timed screens '
                f'loaded in {LOAD_LIMIT_S:g} s or less.</div>')
    def when(secs):
        if secs is None:
            return ('<span style="color:#b91c1c;font-weight:700">'
                    f'{_esc("loads indefinitely")}</span> (over 20 s)')
        return f'<span style="color:#b45309;font-weight:600">{secs:.1f} s</span>'
    rows = "".join(
        f'<tr><td style="{_CELL}">{_esc(screen)}</td><td style="{_CELL}">{_esc(num)}. '
        f'{_esc(name.split(":")[0])}</td><td style="{_CELL}">{when(secs)}</td></tr>'
        for num, name, screen, secs in slow)
    return (head + f'<div style="color:#718096;font-size:12px;margin-bottom:6px">Screens over '
            f'{LOAD_LIMIT_S:g} s ({len(slow)} of {timed} timed). Pure loading time: from the tap '
            f'to the first screen read that showed it.</div>'
            f'<table style="border-collapse:collapse;font-size:13px;margin:0 0 16px">'
            f'<tr><th style="{_CELL};background:#f7f7f7">Screen</th><th style="{_CELL};'
            f'background:#f7f7f7">Segment</th><th style="{_CELL};background:#f7f7f7">'
            f'Load time</th></tr>{rows}</table>')


def _summary_html(summary: str) -> str:
    """The run's AI summary (Reports page → Generate), above the table, when given: the
    exported report carries it (the user, 2026-11-05: "most of the areas is not filled")."""
    if not (summary or "").strip():
        return ""
    # The summary is Markdown (the dashboard renders it so): headings, **bold**, bullets.
    lines = []
    for line in _esc(summary.strip()).split("\n"):
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", line)
        line = re.sub(r"^\s*[-*]\s+", "&bull; ", line)
        lines.append(line)
    body = "<br>".join(lines).replace("<br><br>", "</p><p>")
    return ('<div style="padding:18px 32px;border-bottom:2px solid #e2e8f0;font-size:14px;'
            'line-height:1.6;color:#2d3748"><div style="font-weight:700;font-size:15px;'
            f'margin-bottom:6px">Summary</div><p>{body}</p></div>')


def render_run_report(records: List[Dict[str, Any]], *, title: str,
                      subtitle: str = "", duration: str = "—",
                      validation_header: str = "Bill &amp; VAT Validation",
                      include_screenshots: bool = True, summary: str = "") -> str:
    """Render the full standalone HTML report for one run's scenario records.

    include_screenshots: embed failure screenshots (data-URI) in the report. True for
    the rich (web) view; pass False for the downloadable/exported report so it stays
    lightweight and image-free.
    """
    # A diner's pre-payment and its post-payment (the invoice's Pre-Order block, read in a
    # later segment) are drawn together in the pre-order segment's row.
    ctx = run_context(records)             # what one segment's tables take from others
    # ONE invoice, drawn where it was read; every invoice's own tables (and the closing
    # balance, which it shows) leave their rows (invoice_section.py).
    inv = invoice_section.build(records)
    drop = inv["drop"] if inv else {}
    records = [dict(r, _prepost=(p[0], set(p[1]) | drop.get(i, set()), p[2]), _context=ctx,
                    _invoice=inv["section"] if inv and inv["at"] == i else None)
               for i, (r, p) in enumerate(zip(records, prepost(records)))]
    total = len(records)
    passed = sum(1 for r in records if (r.get("status") or "").upper() == "PASS")
    failed = sum(1 for r in records if (r.get("status") or "").upper() == "FAIL")
    warnings = sum(len(_warnings(r)) for r in records)
    pass_rate = round((passed / total) * 100) if total else 0

    # The segments that calculated something come first, in order; a passed segment with
    # no calculation (a quantity change, the kitchen, send / serve) is listed at the end,
    # just passed (the user, 2026-11-04: "these all keep it last like just passed").
    plain = [r for r in records if (r.get("status") or "").upper() == "PASS"
             and not _has_calculation(r)]
    first = [r for r in records if not any(r is p for p in plain)]
    rows = [_build_row(r, i, include_screenshots=include_screenshots)
            for i, r in enumerate(first)]
    if first and plain:
        # A heading of its own, as big as the table's (the user, 2026-11-05: "increase the
        # size of this font").
        rows.append('<tr><td colspan="5" style="padding:16px 20px;background:#edf2f7;'
                    'color:#2d3748;font-size:18px;font-weight:700">Passed — no calculation '
                    'in these segments</td></tr>')
    rows += [_build_row(r, len(first) + i, include_screenshots=include_screenshots)
             for i, r in enumerate(plain)]
    rows_html = "\n        ".join(rows)
    if not rows_html:
        rows_html = ('<tr><td colspan="5" style="text-align:center;padding:40px;color:#999">'
                     'No scenario results for this run yet</td></tr>')

    return (HTML_TEMPLATE
            .replace("__TITLE__", _esc(title))
            .replace("__SUMMARY__", _summary_html(summary) + _loads_html(records))
            .replace("__SUBTITLE__", subtitle or "")
            .replace("__VALIDATION_HEADER__", validation_header)
            .replace("__TOTAL__", str(total))
            .replace("__PASSED__", str(passed))
            .replace("__FAILED__", str(failed))
            .replace("__WARNINGS__", str(warnings))
            .replace("__DURATION__", _esc(duration))
            .replace("__PASS_RATE__", str(pass_rate))
            .replace("__ROWS__", rows_html))
