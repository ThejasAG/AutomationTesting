"""Performance collection for iOS simulator/device runs.

Samples CPU / memory / (best-effort FPS + network) on a background thread while
tests run, measures app launch + screen-load times, and produces a scored
summary. All metric probes are best-effort: a probe that fails returns None and
never breaks the run.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("performance")

SAMPLE_INTERVAL_S = 2.0
API_THRESHOLD_MS = 500.0
FPS_TARGET = 55.0

# Transport bounds. Samples accumulate for the whole run at SAMPLE_INTERVAL_S, so
# a 3-hour job produces ~5400 of them; the summary's issue list grows with API
# endpoint cardinality. Both cross the wire now, so both are capped here rather
# than discovered as a 413 or a truncated column on the backend.
MAX_SAMPLES = 2000        # ~66 minutes at 2s before thinning starts
MAX_ISSUES = 50
MAX_ENDPOINT_CHARS = 500  # PerformanceSummary.slowest_api_endpoint is String(500)


def _run(cmd: List[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="ignore")
        return r.stdout or ""
    except Exception as e:
        logger.debug("perf cmd failed %s: %s", " ".join(cmd), e)
        return ""


def _cap_endpoint(url: Optional[str]) -> Optional[str]:
    """Bound a URL to the width of the column it lands in."""
    if url is None:
        return None
    return url[:MAX_ENDPOINT_CHARS]


def _downsample(samples: List[Dict[str, Any]], limit: int):
    """Thin a sample series to `limit` points, keeping the run's full time span.

    Returns (samples, downsampled_from) where downsampled_from is None when
    nothing was dropped.

    Evenly spaced by index rather than truncated: truncation would end the series
    early, and the end of a long run is exactly where a memory climb or a CPU
    spike shows up. The first and last samples are always kept so the chart still
    covers the whole run, and the step is derived from the original length so the
    result is deterministic for a given input.
    """
    total = len(samples)
    if total <= limit:
        return list(samples), None
    # limit-1 evenly spaced picks across the series, plus the final sample.
    idx = sorted({(i * (total - 1)) // (limit - 1) for i in range(limit - 1)} | {total - 1})
    return [samples[i] for i in idx], total


class PerformanceCollector:
    def __init__(self, device_id: str, run_id: str, bundle_id: Optional[str] = None,
                 metro_log_path: Optional[str] = None):
        self.device_id = device_id
        self.run_id = run_id
        self.bundle_id = bundle_id
        self.metro_log_path = metro_log_path
        self.metrics: List[Dict[str, Any]] = []
        self.is_collecting = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.app_launch_time_s: Optional[float] = None
        self._issues: List[str] = []
        # Lazily-created API interceptor (Metro log parsing).
        self._api = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        if self.is_collecting:
            return
        self._stop_event.clear()
        self.is_collecting = True
        if self.metro_log_path:
            try:
                from automation.performance.api_interceptor import APIResponseInterceptor
                self._api = APIResponseInterceptor()
                self._api.start_monitoring(self.metro_log_path)
            except Exception as e:
                logger.debug("api interceptor start failed: %s", e)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("PerformanceCollector started for run %s", self.run_id)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                sample = self._collect_ios_metrics()
                if sample:
                    self.metrics.append(sample)
            except Exception as e:
                logger.debug("perf sample failed: %s", e)
            self._stop_event.wait(SAMPLE_INTERVAL_S)

    def stop(self) -> dict:
        if not self.is_collecting:
            return self.get_summary()
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.is_collecting = False
        if self._api:
            try:
                self._api.stop()
            except Exception:
                pass
        logger.info("PerformanceCollector stopped for run %s (%d samples)",
                    self.run_id, len(self.metrics))
        return self.get_summary()

    # ── probes ───────────────────────────────────────────────────────────────
    def _collect_ios_metrics(self) -> Dict[str, Any]:
        cpu, mem = self._cpu_mem()
        net_count, net_avg = self._network_snapshot()
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "cpu_percent": cpu,
            "memory_mb": mem,
            "fps": self._fps(),
            "network_requests": net_count,
            "avg_response_ms": net_avg,
        }

    def _cpu_mem(self):
        """Best-effort CPU% + memory(MB) for the app (or the sim host)."""
        # Try inside-sim top first (works when the app process is visible).
        out = _run(["xcrun", "simctl", "spawn", self.device_id,
                    "top", "-l", "1", "-s", "0", "-n", "0"])
        if out and self.bundle_id:
            proc = self.bundle_id.split(".")[-1][:12]
            for line in out.splitlines():
                if proc.lower() in line.lower():
                    cpu = self._num_after(line, r"(\d+\.?\d*)\s*$|\s(\d+\.?\d*)%")
                    mem = self._mem_from_top(line)
                    if cpu is not None or mem is not None:
                        return cpu, mem
        # Fallback: host-side ps for the launchd_sim / app process of this device.
        ps = _run(["bash", "-c",
                   f"ps -A -o %cpu,rss,command | grep -i '{self.device_id}' | grep -iv grep | head -1"])
        if ps.strip():
            parts = ps.split()
            try:
                cpu = float(parts[0])
                mem = round(float(parts[1]) / 1024.0, 1)  # rss KB → MB
                return cpu, mem
            except (ValueError, IndexError):
                pass
        return None, None

    def _mem_from_top(self, line: str):
        m = re.search(r"(\d+\.?\d*)([KMG])\+?\b", line)
        if not m:
            return None
        val, unit = float(m.group(1)), m.group(2)
        return round(val / 1024.0 if unit == "K" else val * 1024.0 if unit == "G" else val, 1)

    def _num_after(self, line: str, pattern: str):
        m = re.search(pattern, line)
        if not m:
            return None
        for g in m.groups():
            if g:
                try:
                    return float(g)
                except ValueError:
                    pass
        return None

    def _fps(self) -> Optional[float]:
        # FPS needs an active Appium XCUITest perf record session, which the
        # agent does not hold here. Reported as None (not fabricated).
        return None

    def _network_snapshot(self):
        if not self._api:
            return 0, None
        try:
            s = self._api.get_api_summary()
            return s.get("total_calls", 0), s.get("avg_ms")
        except Exception:
            return 0, None

    # ── timed measurements ───────────────────────────────────────────────────
    def measure_app_launch_time(self, bundle_id: Optional[str] = None) -> Optional[float]:
        bundle_id = bundle_id or self.bundle_id
        if not bundle_id:
            return None
        try:
            _run(["xcrun", "simctl", "terminate", self.device_id, bundle_id], timeout=15)
            time.sleep(1.0)
            start = time.time()
            out = _run(["xcrun", "simctl", "launch", self.device_id, bundle_id], timeout=30)
            # `simctl launch` returns once the process is spawned; treat that as
            # time-to-launch. If the app prints a launch marker, prefer it.
            m = re.search(r"Launch time:\s*([\d.]+)", out)
            elapsed = float(m.group(1)) if m else round(time.time() - start, 2)
            self.app_launch_time_s = elapsed
            return elapsed
        except Exception as e:
            logger.debug("launch-time measure failed: %s", e)
            return None

    def measure_screen_load_time(self, driver, action_fn: Callable,
                                 screen_id: str, timeout_s: float = 15.0) -> Optional[float]:
        try:
            start = time.time()
            action_fn()
            deadline = start + timeout_s
            while time.time() < deadline:
                try:
                    els = driver.find_elements("accessibility id", screen_id)
                    if els and els[0].is_displayed():
                        return round((time.time() - start) * 1000.0, 1)
                except Exception:
                    pass
                time.sleep(0.25)
            return round((time.time() - start) * 1000.0, 1)
        except Exception as e:
            logger.debug("screen-load measure failed: %s", e)
            return None

    # ── aggregation + scoring ────────────────────────────────────────────────
    def _vals(self, key: str) -> List[float]:
        return [m[key] for m in self.metrics if m.get(key) is not None]

    def get_summary(self) -> dict:
        cpu = self._vals("cpu_percent")
        mem = self._vals("memory_mb")
        fps = self._vals("fps")
        api = self._api.get_api_summary() if self._api else {}

        avg_cpu = round(sum(cpu) / len(cpu), 1) if cpu else 0.0
        peak_cpu = round(max(cpu), 1) if cpu else 0.0
        avg_mem = round(sum(mem) / len(mem), 1) if mem else 0.0
        peak_mem = round(max(mem), 1) if mem else 0.0
        avg_fps = round(sum(fps) / len(fps), 1) if fps else None
        min_fps = round(min(fps), 1) if fps else None
        dropped = sum(1 for f in fps if f < 30) if fps else 0

        slowest = api.get("slowest") or {}
        avg_api = api.get("avg_ms")

        summary = {
            "app_launch_time_s": self.app_launch_time_s,
            "avg_cpu_percent": avg_cpu,
            "peak_cpu_percent": peak_cpu,
            "avg_memory_mb": avg_mem,
            "peak_memory_mb": peak_mem,
            "avg_fps": avg_fps,
            "min_fps": min_fps,
            "dropped_frames": dropped,
            "api_calls": api.get("total_calls", 0),
            "avg_api_response_ms": round(avg_api, 1) if avg_api else None,
            "slowest_api_ms": slowest.get("ms"),
            "slowest_api_endpoint": _cap_endpoint(slowest.get("url")),
        }
        summary["issues"] = self._detect_issues(summary, api)
        score = self.calculate_performance_score(summary)
        summary["performance_score"] = score
        summary["grade"] = self._grade(score)
        return summary

    def _detect_issues(self, s: dict, api: dict) -> List[str]:
        issues = list(self._issues)
        if s["peak_cpu_percent"] and s["peak_cpu_percent"] > 80:
            issues.append(f"CPU spike to {s['peak_cpu_percent']:.0f}%")
        if s["min_fps"] is not None and s["min_fps"] < 30:
            issues.append(f"FPS dropped to {s['min_fps']:.0f} during the run")
        if s["peak_memory_mb"] and s["peak_memory_mb"] > 400:
            issues.append(f"Memory peaked at {s['peak_memory_mb']:.0f} MB")
        for ep, d in (api.get("by_endpoint") or {}).items():
            if d.get("max_ms", 0) > API_THRESHOLD_MS:
                issues.append(f"API {ep} took {d['max_ms']:.0f}ms (threshold: {int(API_THRESHOLD_MS)}ms)")
        for f in (api.get("failed") or []):
            issues.append(f"API {f.get('url')} failed with {f.get('status')}")
        if len(issues) > MAX_ISSUES:
            # Say how many were dropped. A silently shortened list reads as a
            # healthier run than it was.
            omitted = len(issues) - (MAX_ISSUES - 1)
            issues = issues[:MAX_ISSUES - 1] + [f"+{omitted} more issues"]
        return issues

    def calculate_performance_score(self, metrics: dict) -> int:
        score = 0
        lt = metrics.get("app_launch_time_s")
        if lt is not None:
            score += 25 if lt < 2 else 15 if lt < 3 else 0
        else:
            score += 15  # unknown launch → neutral-ish
        cpu = metrics.get("avg_cpu_percent") or 0
        score += 25 if cpu and cpu < 30 else 15 if cpu < 50 else 0
        fps = metrics.get("avg_fps")
        if fps is not None:
            score += 25 if fps > 55 else 15 if fps > 30 else 0
        else:
            score += 20  # FPS not measured → don't punish heavily
        api = metrics.get("avg_api_response_ms")
        if api is not None:
            score += 25 if api < 300 else 15 if api < 500 else 0
        else:
            score += 15
        return min(100, score)

    @staticmethod
    def _grade(score: int) -> str:
        return "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"

    # ── transport ────────────────────────────────────────────────────────────
    def payload(self) -> dict:
        """Everything the backend needs to persist this run, and nothing else.

        Pure: no session, no HTTP, no filesystem. The agent posts the result to
        /runs/{run_id}/performance, which owns the write (Phase 4F.6). The score
        and grade travel as values because reproducing calculate_performance_score()
        on the backend would duplicate the collector's own scoring rules.
        """
        samples, original = _downsample(self.metrics, MAX_SAMPLES)
        return {
            "summary": self.get_summary(),
            "samples": samples,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "downsampled_from": original,
        }

    @staticmethod
    def _parse_ts(ts):
        try:
            return datetime.fromisoformat(ts) if ts else datetime.utcnow()
        except Exception:
            return datetime.utcnow()
