"""SMTP email — the morning report. No-op when unconfigured, like dispatch.py.

SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM (= SMTP_USER),
SMTP_SSL=1 for implicit TLS on 465 (otherwise STARTTLS).
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional, Tuple

logger = logging.getLogger("notify.email")


def enabled() -> bool:
    return all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def send(to: List[str], subject: str, html: str, text: str = "",
         attachment: Optional[Tuple[str, str, bytes]] = None) -> Tuple[bool, str]:
    """attachment = (filename, mime 'type/subtype', bytes). Returns (ok, detail)."""
    if not enabled():
        return False, "SMTP is not configured (SMTP_HOST / SMTP_USER / SMTP_PASSWORD in .env)"
    if not to:
        return False, "no recipients"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")
    msg["To"] = ", ".join(to)
    msg.set_content(text or "This report is best viewed as HTML.")
    msg.add_alternative(html, subtype="html")
    if attachment:
        name, mime, data = attachment
        main, sub = mime.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    host, port = os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT") or 587)
    try:
        if os.getenv("SMTP_SSL") == "1":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
                s.send_message(msg)
        return True, f"sent to {len(to)} recipient(s)"
    except Exception as e:
        logger.warning("email send failed: %s", e)
        return False, f"send failed: {e}"
