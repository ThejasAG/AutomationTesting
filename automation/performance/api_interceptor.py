"""Parse Metro bundler / console output for API call timing.

Recognises lines the app logs for network calls, e.g.:
    fetch: GET https://api.vyapy.com/restaurants → 200 in 234ms
    [API] POST /api/v1/orders 201 512ms
    LOG  GET https://api.vyapy.com/menu 200 in 89 ms
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("api_interceptor")

_METHOD = r"(GET|POST|PUT|PATCH|DELETE)"
_URL = r"(https?://[^\s]+|/[^\s]+)"
# Two common shapes: "... METHOD URL → STATUS in Nms" and "... METHOD URL STATUS Nms"
_PATTERNS = [
    re.compile(rf"{_METHOD}\s+{_URL}\s*(?:→|->|-|)\s*(\d{{3}})\s*(?:in\s*)?([\d.]+)\s*ms", re.IGNORECASE),
    re.compile(rf"{_METHOD}\s+{_URL}\s+(\d{{3}})\s+(?:in\s*)?([\d.]+)\s*ms", re.IGNORECASE),
]


class APIResponseInterceptor:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start_monitoring(self, metro_log_path: str):
        """Tail the Metro log file on a background thread (best-effort)."""
        self._path = metro_log_path
        self._stop.clear()
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()

    def _tail(self):
        try:
            # Seek to end so we only capture calls made during this run.
            pos = os.path.getsize(self._path) if os.path.exists(self._path) else 0
        except Exception:
            pos = 0
        while not self._stop.is_set():
            try:
                if os.path.exists(self._path):
                    with open(self._path, "r", errors="ignore") as f:
                        f.seek(pos)
                        for line in f:
                            rec = self.parse_metro_log_line(line)
                            if rec:
                                self.calls.append(rec)
                        pos = f.tell()
            except Exception as e:
                logger.debug("metro tail error: %s", e)
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def parse_metro_log_line(self, line: str) -> Optional[Dict[str, Any]]:
        for pat in _PATTERNS:
            m = pat.search(line)
            if m:
                try:
                    return {
                        "method": m.group(1).upper(),
                        "url": m.group(2),
                        "status": int(m.group(3)),
                        "duration_ms": float(m.group(4)),
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                except (ValueError, IndexError):
                    return None
        return None

    @staticmethod
    def _endpoint(url: str) -> str:
        try:
            p = urlparse(url)
            return p.path or url
        except Exception:
            return url

    def get_api_summary(self) -> Dict[str, Any]:
        calls = list(self.calls)
        if not calls:
            return {"total_calls": 0, "avg_ms": None, "slowest": {}, "fastest": {},
                    "failed": [], "by_endpoint": {}}

        durs = [c["duration_ms"] for c in calls]
        slowest = max(calls, key=lambda c: c["duration_ms"])
        fastest = min(calls, key=lambda c: c["duration_ms"])
        failed = [{"url": c["url"], "status": c["status"]} for c in calls if c["status"] >= 400]

        by_ep: Dict[str, Dict[str, Any]] = {}
        for c in calls:
            ep = self._endpoint(c["url"])
            d = by_ep.setdefault(ep, {"calls": 0, "_sum": 0.0, "max_ms": 0.0})
            d["calls"] += 1
            d["_sum"] += c["duration_ms"]
            d["max_ms"] = max(d["max_ms"], c["duration_ms"])
        for ep, d in by_ep.items():
            d["avg_ms"] = round(d.pop("_sum") / d["calls"], 1)
            d["max_ms"] = round(d["max_ms"], 1)

        return {
            "total_calls": len(calls),
            "avg_ms": round(sum(durs) / len(durs), 1),
            "slowest": {"url": self._endpoint(slowest["url"]), "ms": round(slowest["duration_ms"], 1)},
            "fastest": {"url": self._endpoint(fastest["url"]), "ms": round(fastest["duration_ms"], 1)},
            "failed": failed,
            "by_endpoint": by_ep,
        }
