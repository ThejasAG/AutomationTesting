"""The email + Slack note a session sends when it ends.

Sent for unattended sessions (nightly, after a Run all, app changes) and, when the
setting says so, for sessions started by hand. The full story is always on the AI
Agent page; the note says what happened and what is waiting for approval.
"""
from __future__ import annotations

import html
import os
from typing import Any, Dict, List

from automation.claude_code import paths

DASHBOARD = os.getenv("DASHBOARD_URL", "http://localhost:5173")
KIND_NAMES = {"heal": "Run & heal", "fix_run": "Fix a failed run", "analyze": "App analysis",
              "generate": "New scenarios", "nightly": "Nightly pass"}
STATUS_TEXT = {"review": "changes waiting for approval", "gates_failed": "a change failed its checks",
               "no_changes": "no changes", "failed": "failed", "stopped": "stopped",
               "interrupted": "interrupted"}


def _flows_line(summary: Dict[str, Any]) -> str:
    c: Dict[str, int] = {}
    for f in summary.get("flows") or []:
        st = (f.get("final_status") or "").lower() or "failed"
        c[st] = c.get(st, 0) + 1
    return ", ".join(f"{n} {k}" for k, n in sorted(c.items(), key=lambda kv: -kv[1]))


def build(s: Dict[str, Any]) -> Dict[str, str]:
    summary = s.get("summary") or {}
    sdir = paths.session_dir(s["id"])
    try:
        with open(os.path.join(sdir, "report.md"), encoding="utf-8") as f:
            report_md = f.read()
    except OSError:
        report_md = ""
    flows = _flows_line(summary)
    subject = (f"AI Agent — {KIND_NAMES.get(s['kind'], s['kind'])}: "
               f"{STATUS_TEXT.get(s['status'], s['status'])}" + (f" ({flows})" if flows else ""))
    link = f"{DASHBOARD}/agent?session={s['id']}"
    problems: List[str] = []
    res = s.get("result") or {}
    if res.get("timed_out"):
        problems.append("Claude Code reached the session's time limit; the report may be partial.")
    if res.get("is_error"):
        problems.append("Claude Code stopped early: " + str(res.get("result") or "")[:300]
                        + " (a usage-limit message means the Claude plan's limit was reached).")
    if s.get("error"):
        problems.append(s["error"][:500])
    if not report_md and s["status"] not in ("stopped",):
        problems.append("Claude Code did not write a report.")
    failed_gates = [g for g in (s.get("gates") or []) if not g.get("ok")]

    try:
        import markdown
        body = markdown.markdown(report_md, extensions=["tables", "fenced_code"])
    except ImportError:
        body = f"<pre>{html.escape(report_md)}</pre>"
    if s["status"] == "review":
        what = (f"<p><b>Changes are waiting for your approval</b> on branch <code>{html.escape(s['branch'] or '')}</code> "
                f"— every check passed. Approve or reject them on the "
                f"<a href='{link}'>AI Agent page</a>.</p>")
    elif s["status"] == "gates_failed":
        what = ("<p><b>The session changed code, but a check failed</b>, so it cannot be approved: "
                + "; ".join(html.escape(f"{g['name']}: {g['detail'].splitlines()[0]}") for g in failed_gates)
                + f". Details on the <a href='{link}'>AI Agent page</a>.</p>")
    else:
        what = f"<p>Details on the <a href='{link}'>AI Agent page</a>.</p>"
    warn = "".join(f"<p style='color:#b91c1c'><b>⚠ {html.escape(p)}</b></p>" for p in problems)
    page = (f"<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:900px'>"
            f"<h2>{html.escape(subject)}</h2>{warn}{what}{body}</div>")

    slack = [f"*{subject}*"]
    if summary.get("headline"):
        slack.append(summary["headline"])
    slack += [f"⚠ {p}" for p in problems]
    if s["status"] == "review":
        slack.append(f"Changes waiting for approval: {link}")
    for b in summary.get("app_bugs") or []:
        slack.append(f"🐞 {b.get('app')}: {b.get('summary')}")
    text = "\n".join(problems + [report_md or "(no report)", link])
    return {"subject": subject, "html": page, "text": text, "slack": "\n".join(slack)}


def maybe_send(sid: str) -> Dict[str, Any]:
    from automation.agentic import settings as agent_settings
    from automation.claude_code import session
    s = session.get(sid)
    if s is None:
        return {"sent": False}
    cfg = agent_settings.get()
    if s["trigger"] == "manual" and not cfg.get("report_manual_sessions"):
        return {"sent": False, "why": "manual session"}
    rep = build(s)
    with open(os.path.join(paths.session_dir(sid), "report.html"), "w", encoding="utf-8") as f:
        f.write(rep["html"])
    out: Dict[str, Any] = {}
    try:
        from automation.notifications import email
        ok, detail = email.send(cfg.get("report_emails") or [], rep["subject"], rep["html"], rep["text"])
        out["email"] = detail if ok else f"not sent: {detail}"
    except Exception as e:  # noqa: BLE001
        out["email"] = f"not sent: {e}"
    if cfg.get("report_slack"):
        try:
            from automation.notifications.dispatch import send_slack_text
            out["slack"] = send_slack_text(rep["slack"])
        except Exception as e:  # noqa: BLE001
            out["slack"] = f"not sent: {e}"
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        if r is not None:
            email_ok = not str(out.get("email", "")).startswith("not sent")
            r.report_sent = email_ok or out.get("slack") is True
            db.commit()
    return out
