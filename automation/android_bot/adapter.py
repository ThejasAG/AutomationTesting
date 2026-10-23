"""HTTP bridge between the platform and Tanish's Android bot.

    platform  ──POST /run──►  adapter (:9000)  ──writes──►  bot_queue.json
                                    ▲                              │
    bot  ──GET /status──────────────┘                              │ polls
    bot  ──POST /result──►  adapter  ──forward──►  callback_url (platform)

Run it standalone:  python -m automation.android_bot.adapter
It does NOT need the main platform in-process — only the platform's HTTP API,
which it reaches via the callback_url each job carries.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from automation.android_bot import queue_manager as q

logger = logging.getLogger("android_bot.adapter")

adapter_app = FastAPI(title="Vya Android Bot Adapter", version="1.0.0")


# ── Schemas ──────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    run_id: str
    scenarios: List[Any] = []
    callback_url: str
    consumer_device: Optional[str] = None
    business_device: Optional[str] = None


class ResultRequest(BaseModel):
    run_id: str
    callback_url: Optional[str] = None      # falls back to the queued job's URL
    scenario_num: str
    scenario_name: str
    status: str
    consumer_status: str = "N/A"
    business_status: str = "N/A"
    error: Optional[str] = None
    reasons: Optional[List[str]] = None
    launch_time: Optional[float] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@adapter_app.post("/run")
def receive_run(req: RunRequest):
    """Platform hands us a job. Queue it; the bot picks it up by polling /status."""
    q.add_job(
        req.run_id, req.scenarios, req.callback_url,
        req.consumer_device, req.business_device,
    )
    logger.info("Queued Android job run_id=%s (%d scenarios)", req.run_id, len(req.scenarios))
    return {"accepted": True, "run_id": req.run_id}


@adapter_app.get("/status")
def status():
    """The bot polls this to discover pending work and report progress."""
    return q.current_status()


@adapter_app.post("/result")
def receive_result(req: ResultRequest):
    """Bot posts one scenario result here; we forward it to the platform.

    The bot may instead POST straight to the platform's callback_url — this route
    exists so the bot only ever needs to know the adapter's address.
    """
    job = q.get_pending_job()  # informational; job may already be 'started'
    callback = req.callback_url
    if not callback:
        # Look the job up to recover its callback_url.
        for j in q._load():          # noqa: SLF001 — module-local helper
            if j.get("run_id") == req.run_id:
                callback = j.get("callback_url")
                break
    if not callback:
        raise HTTPException(status_code=400, detail="No callback_url for this run_id")

    q.mark_job_started(req.run_id)
    q.increment_completed(req.run_id)

    payload = {
        "scenario_num": req.scenario_num,
        "scenario_name": req.scenario_name,
        "status": req.status,
        "consumer_status": req.consumer_status,
        "business_status": req.business_status,
        "error": req.error,
        "reasons": req.reasons or [],
        "launch_time": req.launch_time,
    }
    headers = {}
    secret = os.getenv("BOT_SECRET", "")
    if secret:
        headers["X-Bot-Secret"] = secret

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(callback, json=payload, headers=headers)
        forwarded = resp.status_code
    except Exception as exc:
        logger.error("Forward to platform failed (%s): %s", callback, exc)
        raise HTTPException(status_code=502, detail=f"Forward failed: {exc}")

    return {"forwarded": True, "platform_status": forwarded}


@adapter_app.post("/done/{run_id}")
def mark_done(run_id: str):
    """Bot calls this when it finishes a run's scenarios."""
    q.mark_job_done(run_id)
    return {"done": True, "run_id": run_id}


def main():
    import uvicorn
    port = int(os.getenv("BOT_ADAPTER_PORT", "9000"))
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(adapter_app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
