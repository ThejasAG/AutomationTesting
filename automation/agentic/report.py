"""The morning report: one email (HTML) + one Slack message per batch."""
from __future__ import annotations

import html
import logging
from typing import Any, Dict, List, Tuple

from automation.agentic import settings
from automation.database.config import SessionLocal
from automation.database.models import AgentAction, AgentBatch, AgentItem
from automation.notifications import dispatch
from automation.notifications import email as mail

logger = logging.getLogger("agentic.report")

LABEL = {
    "passed": ("Passed", "#16a34a"), "passed_on_retry": ("Passed on retry", "#65a30d"),
    "fixed": ("Auto-fixed (test)", "#0891b2"), "app_bug": ("App bug", "#dc2626"),
    "infra": ("Environment", "#d97706"), "failed": ("Needs a person", "#dc2626"),
    "stopped": ("Stopped", "#6b7280"), "skipped": ("Skipped", "#6b7280"),
    "pending": ("Not run", "#6b7280"), "running": ("Running", "#2563eb"),
}
ORDER = ["failed", "app_bug", "fixed", "infra", "passed_on_retry", "passed", "stopped", "skipped", "pending"]


def _load(bid: str) -> Tuple[AgentBatch, List[AgentItem], List[AgentAction]]:
    with SessionLocal() as db:
        b = db.get(AgentBatch, bid)
        items = db.query(AgentItem).filter(AgentItem.batch_id == bid).order_by(AgentItem.position).all()
        acts = db.query(AgentAction).filter(AgentAction.batch_id == bid).order_by(AgentAction.created_at).all()
        for o in [b, *items, *acts]:
            db.expunge(o)
    return b, items, acts


def build(bid: str) -> Dict[str, str]:
    b, items, acts = _load(bid)
    base = dispatch.dashboard_url()
    t = b.totals or {}
    ok = t.get("passed", 0) + t.get("passed_on_retry", 0) + t.get("fixed", 0)
    subject = (f"[Vya test agent] {ok}/{len(items)} flows green · "
               f"{t.get('failed', 0) + t.get('app_bug', 0)} need attention · {b.env}")
    e = html.escape
    rows = []
    for it in sorted(items, key=lambda i: (ORDER.index(i.status) if i.status in ORDER else 99, i.position)):
        lab, col = LABEL.get(it.status, (it.status, "#6b7280"))
        mine = [a for a in acts if a.item_id == it.id and a.kind in ("test_fix", "app_fix", "retry", "resume")]
        did = "<br>".join(e(f"{a.kind}: {a.status} — {a.title}")[:300] for a in mine)
        pr = next(((a.detail or {}).get("pr_url") for a in mine if (a.detail or {}).get("pr_url")), None)
        link = f'<a href="{base}/run/{it.final_run_id}">run</a>' if it.final_run_id else ""
        if pr:
            link += f' · <a href="{e(pr)}">PR</a>'
        rows.append(
            f"<tr><td style='padding:6px 8px'><b>{e(it.flow_name or it.flow_id)}</b></td>"
            f"<td style='padding:6px 8px;color:{col};font-weight:600'>{lab}</td>"
            f"<td style='padding:6px 8px'>{e(it.category or '')}</td>"
            f"<td style='padding:6px 8px;max-width:420px'>{e((it.root_cause or '')[:400])}"
            f"{'<br><small>' + did + '</small>' if did else ''}</td>"
            f"<td style='padding:6px 8px'>{link}</td></tr>")
    chips = " · ".join(f"{LABEL.get(k, (k,))[0]}: {v}" for k, v in sorted(t.items()))
    body = f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#111">
<h2 style="margin:0 0 4px">Vya test agent — {e(b.env)} · {b.started_at:%d %b %Y %H:%M}</h2>
<p style="margin:0 0 12px;color:#555">{e(chips)} · Claude spend ${b.spend_usd or 0:.2f}</p>
<table style="border-collapse:collapse;font-size:14px" border="1" cellspacing="0">
<tr style="background:#f3f4f6"><th>Flow</th><th>Result</th><th>Type</th><th>Cause / what the agent did</th><th></th></tr>
{''.join(rows)}</table>
<p style="color:#555;font-size:13px">Test fixes were applied only after a re-run passed with them;
app patches are proposals (or draft PRs) for a person to review. Details:
<a href="{base}/agent">{base}/agent</a></p></div>"""
    lines = [f"*{subject}*", chips, f"Claude spend ${b.spend_usd or 0:.2f}"]
    for it in items:
        if it.status not in ("passed",):
            lines.append(f"• {it.flow_name}: {LABEL.get(it.status, (it.status,))[0]}"
                         f"{' — ' + (it.root_cause or '')[:160] if it.root_cause else ''}")
    lines.append(f"{base}/agent")
    return {"subject": subject, "html": body, "text": "\n".join(lines)}


def send(bid: str) -> Dict[str, Any]:
    s = settings.get()
    rep = build(bid)
    out: Dict[str, Any] = {}
    ok, detail = mail.send(s["report_emails"], rep["subject"], rep["html"], rep["text"],
                           attachment=("agent-report.html", "text/html", rep["html"].encode()))
    out["email"] = detail
    if s.get("report_slack") and dispatch.slack_enabled():
        out["slack"] = "sent" if dispatch.send_slack_text(rep["text"]) else "failed"
    with SessionLocal() as db:
        b = db.get(AgentBatch, bid)
        if b:
            b.report_sent = ok or out.get("slack") == "sent"
        db.add(AgentAction(kind="report", batch_id=bid, title=f"Report: {detail}",
                           status="done" if ok else "failed", detail=out))
        db.commit()
    return out
