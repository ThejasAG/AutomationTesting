"""Persistent self-healing locator memory.

The resolver already tries several strategies (exact id → predicate → fuzzy text),
but that knowledge lived only for the run. This store makes it PERSIST:

  * When a step resolves to a working locator, we remember `(app, target) → locator`.
  * Next run, we try that remembered locator FIRST — one fast, direct find instead
    of re-deriving it through the fuzzy fallbacks (faster AND more stable).
  * When the app's id drifts and the remembered locator stops resolving, the runner
    falls through to the normal strategies, finds the element the slow way, and
    RE-LEARNS the new locator. The suite heals itself instead of going red.

Pure/testable: no Appium, no device. The runner passes in what worked; this only
stores and recalls it. Safe by construction — a miss just means "fall through".
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Optional, Tuple

_LOCK = threading.RLock()
_DEFAULT_PATH = os.path.join("automation", "knowledge", "learned_locators.json")

# Locator methods we persist. 'aid' = accessibility id, 'pred' = iOS predicate.
AID = "aid"
PRED = "pred"


def _norm(target: str) -> str:
    """Normalise a step target to a stable key ('  Click  Book Now ' → 'book now')."""
    return " ".join(str(target or "").lower().split())


class LearnedLocatorStore:
    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self.path = path
        # bundle -> target_key -> {method, value, hits, heals, drifts, ts}
        self._data: Dict[str, Dict[str, dict]] = {}
        self._dirty = 0
        self._load()

    def _load(self) -> None:
        try:
            if os.path.isfile(self.path):
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f) or {}
        except Exception:
            self._data = {}

    def save(self) -> None:
        """Atomic write so a crash mid-run never corrupts the store."""
        with _LOCK:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=0)
                os.replace(tmp, self.path)
                self._dirty = 0
            except Exception:
                pass

    def get(self, bundle: str, target: str) -> Optional[Tuple[str, str]]:
        """The last known-good (method, value) for this app+target, or None."""
        e = self._data.get(bundle or "", {}).get(_norm(target))
        if e and e.get("method") and e.get("value"):
            return e["method"], e["value"]
        return None

    def learn(self, bundle: str, target: str, method: str, value: str,
              healed: bool = False) -> None:
        """Record the locator that worked. `healed=True` means it was found via a
        fallback (the remembered/exact one had failed) — i.e. a real self-heal."""
        if not (bundle and target and method and value):
            return
        with _LOCK:
            app = self._data.setdefault(bundle, {})
            k = _norm(target)
            existing = app.get(k)
            is_new = existing is None
            e = existing or {"hits": 0, "heals": 0, "drifts": 0}
            drifted = (not is_new) and (e.get("value") != value or e.get("method") != method)
            e["method"], e["value"] = method, value
            e["hits"] = int(e.get("hits", 0)) + 1
            if healed:
                e["heals"] = int(e.get("heals", 0)) + 1
            if drifted:
                e["drifts"] = int(e.get("drifts", 0)) + 1
            e["ts"] = int(time.time())
            app[k] = e
            self._dirty += 1
            # Persist promptly on anything valuable — a new locator, a drift, or a
            # self-heal — else batch repeat-hits to avoid churning the disk.
            if is_new or drifted or healed or self._dirty >= 10:
                self.save()

    def stats(self, bundle: Optional[str] = None) -> dict:
        """Coverage/health numbers for the reliability dashboard."""
        apps = {bundle: self._data.get(bundle, {})} if bundle else self._data
        total = heals = drifts = 0
        for _b, targets in apps.items():
            for e in targets.values():
                total += 1
                heals += int(e.get("heals", 0))
                drifts += int(e.get("drifts", 0))
        return {"learned": total, "self_heals": heals, "id_drifts": drifts}


_STORE: Optional[LearnedLocatorStore] = None


def get_store() -> LearnedLocatorStore:
    """Process-wide singleton so every runner shares (and grows) the same memory."""
    global _STORE
    if _STORE is None:
        with _LOCK:
            if _STORE is None:
                _STORE = LearnedLocatorStore()
    return _STORE
