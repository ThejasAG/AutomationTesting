# Patch for the Vya-agentic-BOT — send results to the platform

Two changes. The bot stops writing to `scenario_history.json` as its source of
truth and instead (1) polls the adapter for a job and (2) POSTs each scenario
result to that job's `callback_url`. Your local HTML report can stay — just add
the POST alongside it.

The adapter runs on the platform host at `http://localhost:9000` (or wherever
`BOT_ADAPTER_PORT` puts it). If your bot is on another machine, replace
`localhost` with the platform host's IP.

---

## Change 1 — poll for a job before running

At the top of your run loop, ask the adapter whether there is work:

```python
import requests, time

ADAPTER = "http://localhost:9000"       # platform host

def wait_for_job():
    """Block until the platform queues a run, then return it."""
    while True:
        st = requests.get(f"{ADAPTER}/status", timeout=5).json()
        if st.get("current_run_id") and not st.get("running"):
            return st["current_run_id"]
        time.sleep(3)

run_id = wait_for_job()
# also fetch the full job (callback_url etc.) — the adapter keeps it in the queue
```

The job carries `run_id`, `scenarios`, `callback_url`, `consumer_device`,
`business_device`. If you can read `bot_queue.json` on the shared host, load it
directly; otherwise the `current_run_id` from `/status` is enough and the
`callback_url` is always:

```
http://<platform-host>:8000/api/v1/runs/<run_id>/scenario-result
```

## Change 2 — POST each result instead of (or as well as) writing JSON

Find where you currently call `scenario_reporter.add_result(...)`. Right after
it, POST the same data. This is one function; call it from `add_result` so every
existing call site is covered with no other edits:

```python
import os, requests

CALLBACK = None   # set from the job's callback_url when a run starts

def post_result(scenario_num, scenario_name, role, status,
                consumer_status, business_status, error, reasons, launch_time):
    if not CALLBACK:
        return
    headers = {}
    secret = os.getenv("BOT_SECRET")           # must match the platform's BOT_SECRET
    if secret:
        headers["X-Bot-Secret"] = secret
    try:
        requests.post(CALLBACK, json={
            "scenario_num":    str(scenario_num),
            "scenario_name":   scenario_name,
            "status":          status,           # overall; platform re-derives it too
            "consumer_status": consumer_status,  # "PASS" | "FAIL" | "N/A"
            "business_status": business_status,  # "PASS" | "FAIL" | "N/A"
            "error":           error,
            "reasons":         reasons or [],
            "launch_time":     launch_time,
        }, headers=headers, timeout=10)
    except Exception as e:
        print(f"[Report] Could not POST result to platform: {e}")
```

You already track `consumer_status` / `business_status` / `reasons` per scenario
in `scenario_reporter` — pass those straight through. The platform applies the
same rule you do: **FAIL if either Consumer or Business failed**.

Alternatively, POST to the adapter (`http://<host>:9000/result`) with the same
body plus `"run_id"` — the adapter forwards it to the platform, so your bot only
needs the adapter's address.

## Optional — mark the run finished

When all scenarios for a run are done:

```python
requests.post(f"{ADAPTER}/done/{run_id}", timeout=5)
```

---

## Field mapping (bot → platform)

| Bot value (`scenario_reporter`)     | Platform field     |
|-------------------------------------|--------------------|
| `num`                               | `scenario_num`     |
| `name`                              | `scenario_name`    |
| `status`                            | `status`           |
| `consumer_status`                   | `consumer_status`  |
| `business_status`                   | `business_status`  |
| `error`                             | `error`            |
| `reasons` (list)                    | `reasons`          |
| `launch_time`                       | `launch_time`      |

## Environment

- `BOT_SECRET` — same value on the bot and the platform. If unset on the
  platform, the header is not required (dev only).
- Adapter address — default `http://localhost:9000`; change host if remote.
