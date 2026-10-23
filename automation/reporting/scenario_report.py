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
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

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
    thead th {
      background: #f7fafc; padding: 16px 20px; font-size: 16px; font-weight: 700;
      color: #333; border-bottom: 2px solid #e2e8f0; text-align: center;
    }
    thead th:first-child { text-align: left; }
    td { padding: 14px 20px; border-bottom: 1px solid #edf2f7; font-size: 14px; vertical-align: top; }
    .td-c { text-align: center; }
    .td-bill { padding: 16px 20px; font-size: 15px; line-height: 2.0; text-align: center; min-width: 500px; }
    .muted { color: #999; }
    tr:hover td { background: #f7fafc; }
    @media print {
      body { background: #fff; padding: 0; }
      .card { box-shadow: none; border-radius: 0; max-width: none; }
      .printbtn { display: none; }
      tr:hover td { background: transparent; }
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
      <div class="stat"><div class="val" style="color:#3182ce">__DURATION__</div><div class="lbl">Duration</div></div>
      <div class="stat"><div class="val" style="color:#805ad5">__PASS_RATE__%</div><div class="lbl">Pass Rate</div></div>
    </div>

    <table>
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
    base = ("padding:4px 14px;border-radius:20px;color:#fff;"
            "font-size:11px;font-weight:700;letter-spacing:.5px")
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


def _build_validation_cell(record: Dict[str, Any]) -> str:
    all_reasons = ((record.get('consumer_reasons') or [])
                   + (record.get('business_reasons') or [])
                   + (record.get('reasons') or []))
    sections = _extract_bill_check_sections(all_reasons)
    if not sections:
        return '<span style="color:#999">&mdash;</span>'

    blocks = []
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


def _build_failed_at_cell(record: Dict[str, Any]) -> str:
    if record.get("status") == "PASS":
        return '<span style="color:#27ae60">-</span>'
    err = record.get("error", "") or ""
    if not err:
        return '<span style="color:#c0392b">Failure</span>'
    structured = _format_failed_at_html(err)
    if structured:
        return structured
    return f'<span style="font-size:13px">{_esc(err[:400])}{"…" if len(err) > 400 else ""}</span>'


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
    """A small, clickable failure screenshot (data-URI) shown under the reason in the
    RICH web report. Only for failures that actually captured one. Callers gate this
    behind include_screenshots so the downloadable report can omit it."""
    shot = (record.get("screenshot") or "").strip()
    if not shot or (record.get("status", "").upper() != "FAIL"):
        return ""
    if not shot.startswith("data:image"):
        return ""
    return (
        '<div style="margin-top:10px">'
        '<div style="color:#888;font-size:12px;margin-bottom:4px">Screen at failure:</div>'
        f'<a href="{shot}" target="_blank" rel="noreferrer">'
        f'<img src="{shot}" alt="screen at failure" '
        'style="max-width:260px;max-height:360px;border:1px solid #e2e8f0;border-radius:8px;'
        'box-shadow:0 2px 8px rgba(0,0,0,0.12)" /></a>'
        '<div style="color:#a0aec0;font-size:11px;margin-top:3px">click to enlarge</div>'
        '</div>')


def _build_row(record: Dict[str, Any], index: int, include_screenshots: bool = True) -> str:
    bg = "#ffffff" if index % 2 == 0 else "#f8fafc"
    name = _esc(record.get("name", "Unknown"))
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
        f'<td style="padding:14px 20px;border-bottom:1px solid #edf2f7;color:#c0392b;'
        f'font-size:15px;font-weight:500;vertical-align:top">{failed_cell}</td>'
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


def render_run_report(records: List[Dict[str, Any]], *, title: str,
                      subtitle: str = "", duration: str = "—",
                      validation_header: str = "Bill &amp; VAT Validation",
                      include_screenshots: bool = True) -> str:
    """Render the full standalone HTML report for one run's scenario records.

    include_screenshots: embed failure screenshots (data-URI) in the report. True for
    the rich (web) view; pass False for the downloadable/exported report so it stays
    lightweight and image-free.
    """
    total = len(records)
    passed = sum(1 for r in records if (r.get("status") or "").upper() == "PASS")
    failed = sum(1 for r in records if (r.get("status") or "").upper() == "FAIL")
    pass_rate = round((passed / total) * 100) if total else 0

    rows_html = "\n        ".join(
        _build_row(r, i, include_screenshots=include_screenshots)
        for i, r in enumerate(records))
    if not rows_html:
        rows_html = ('<tr><td colspan="5" style="text-align:center;padding:40px;color:#999">'
                     'No scenario results for this run yet</td></tr>')

    return (HTML_TEMPLATE
            .replace("__TITLE__", _esc(title))
            .replace("__SUBTITLE__", subtitle or "")
            .replace("__VALIDATION_HEADER__", validation_header)
            .replace("__TOTAL__", str(total))
            .replace("__PASSED__", str(passed))
            .replace("__FAILED__", str(failed))
            .replace("__DURATION__", _esc(duration))
            .replace("__PASS_RATE__", str(pass_rate))
            .replace("__ROWS__", rows_html))
