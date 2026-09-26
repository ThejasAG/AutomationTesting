"""Drive the Consumer (iPhone) and Business (iPad) simulators AT ONCE.

This is the iOS equivalent of the Android bot's threading model: two Appium
sessions on two simulators, run in parallel threads, synchronised at the cross-app
handoffs (order_placed -> table_accepted -> kitchen_accepted -> payment). Each
phase records a ScenarioResult with consumer_status + business_status, so the
both-must-pass gate and the dashboard Scenarios tab work exactly as they do for
the Android bot.

HONEST LIMITS (why a run does not go fully green today):
  * Consumer crashes before checkout (TextEncoder / appointment.type).
  * The Business RN app needs its OWN Metro (8081 serves the Consumer bundle),
    so it currently stalls on its splash. Its labels are therefore uncaptured.
The orchestrator runs each side AS FAR AS IT CAN and reports the truth per phase,
rather than pretending. Fix those app-side blockers and the same run completes.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional

import httpx
from appium import webdriver
from appium.options.ios import XCUITestOptions

from automation.database.config import SessionLocal
from automation.database.models import ScenarioResult, TestRun
from automation.projects.repository import repository_manager as rm
from automation.intelligence.scenario_runner import ScenarioRunner
from automation.projects.builder import app_builder

logger = logging.getLogger("cross_app")

CONSUMER_PROJECT_ID = "bd34a47c-c099-4d36-ac61-810edfff31ca"
BUSINESS_PROJECT_ID = "1519bec5-d14c-45d9-9616-891b98c6e2d8"
# Which build these sims actually have installed.
#
# This is the ONE definition of the pair: cross_app_flows imports FROM this
# module, so it re-exports ENV_BUNDLES from here rather than the other way
# round (importing it back would be circular).
#
# MEASURED: these were hardcoded to prod while the iPad only had
# 'vyabusinessipadstaging' installed. Appium answered "App with bundle
# identifier 'org.vyapy.sarls.vyabusinessipad' unknown", the business session
# never started, and the run died before a single waiter step — recorded as
# "Book slot FAIL" with a NULL error, which looks like a scenario bug and is not.
ENV_BUNDLES = {
    "prod": {
        "consumer": "org.vyapy.sarls.vyaconsumer",
        "business": "org.vyapy.sarls.vyabusinessipad",
    },
    "staging": {
        "consumer": "org.vyapy.sarls.vyaconsumerstaging",
        "business": "org.vyapy.sarls.vyabusinessipadstaging",
    },
}
VYA_ENV = os.getenv("VYA_ENV", "staging")
CONSUMER_BUNDLE = ENV_BUNDLES.get(VYA_ENV, ENV_BUNDLES["prod"])["consumer"]
BUSINESS_BUNDLE = ENV_BUNDLES.get(VYA_ENV, ENV_BUNDLES["prod"])["business"]

# Fallback devices, resolved to THIS Mac's simulators: the ids below are one
# developer's machine and fail everywhere else ("Invalid device or device pair").
# local_udid_for keeps an id that exists here and otherwise picks the nearest
# local simulator of the same kind (booted first, then the same model).
from automation.scenarios.cross_app_config import list_ios_simulators, local_udid_for
_SIMS = list_ios_simulators()
DEFAULT_CONSUMER_UDID = local_udid_for(
    "iPhone", "DA24A392-FF1B-4283-A5CE-CDDE0D000D21", sims=_SIMS) \
    or "DA24A392-FF1B-4283-A5CE-CDDE0D000D21"                        # iPhone 16 Pro
DEFAULT_BUSINESS_UDID = local_udid_for(
    "iPad", "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B", sims=_SIMS) \
    or "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B"                        # iPad Pro 11"
# Dedicated phone for the B-app (waiter+kitchen) when running "phone" mode — a SEPARATE
# iPhone 16 (base), NOT the consumer's iPhone 16 Pro. Sharing one sim with the consumer
# caused WDA session collisions; this device is its own sim with the staging B-app installed.
DEFAULT_BUSINESS_PHONE_UDID = local_udid_for(
    "iPhone", "B1093E61-C510-4E6E-8A60-C2D05D150F64",
    exclude=[DEFAULT_CONSUMER_UDID], sims=_SIMS) \
    or "B1093E61-C510-4E6E-8A60-C2D05D150F64"                        # iPhone 16
APPIUM_URL = "http://127.0.0.1:4723"

# The Business app is a SEPARATE React Native app — it cannot share Metro on
# 8081 (which serves the Consumer bundle), so it runs its own packager on 8082
# and is pointed at it via RCTBundleURLProvider's RCT_jsLocation user-default.
BUSINESS_METRO_PORT = 8082
# STAGING business is a SEPARATE checkout with its own API base
# (vya.xorstack.com, vs api.vyapy.com for prod), so it needs its own packager.
# Serving the staging app from 8082 hands it the PROD bundle: the app then talks
# to api.vyapy.com while the run signs in with staging credentials, and the login
# fails with what looks like a Firebase/getToken problem. MEASURED: the iPhone 16
# held RCT_jsLocation=localhost:8082 and could never sign in, while the iPad held
# 8083 and signed in fine on the same build.
BUSINESS_STAGING_METRO_PORT = 8083
BUSINESS_STAGING_PROJECT_ID = "5a430056-efd4-49af-8679-7b86a91f9f64"
# Business login labels — captured live once the app loads from :8082.
BIZ_EMAIL_FIELD = "emailValue"
BIZ_PASSWORD_FIELD = "passwordValue"
BIZ_SIGNIN_BTN = "signInBtn"


def _business_metro_target(bundle: str):
    """(metro_port, project_id) for the business bundle actually being driven.

    The staging and prod business apps are different checkouts pointing at
    different API hosts, so they cannot share one packager.

    Both are resolved from CONFIGURATION rather than the constants below: a
    hardcoded project id is a promise about a database row, and rows get recreated.
    BUSINESS_STAGING_PROJECT_ID pointed at a project that no longer existed, so the
    staging packager fell back to serving the PRODUCTION checkout -- the staging app
    then talked to api.vyapy.com, and the waiter's staging credentials were rejected
    with what looked like a Firebase getToken failure.
    """
    from automation.projects import deployment as _deploy
    from automation.projects import environments as _env

    staging = (bundle or "").endswith("staging")
    if "consumer" in (bundle or ""):
        # The consumer app goes through here too (ensure_app_metro): a device that
        # never ran it has no RCT_jsLocation and shows "No bundle URL present".
        fallback = (8084 if staging else 8081, CONSUMER_PROJECT_ID)
    else:
        fallback = ((BUSINESS_STAGING_METRO_PORT, BUSINESS_STAGING_PROJECT_ID)
                    if staging else (BUSINESS_METRO_PORT, BUSINESS_PROJECT_ID))
    if not bundle:
        return fallback
    try:
        project = _deploy.project_for_bundle(bundle)
        env_name = _env.environment_for_bundle(bundle)
        cfg = _env.resolve(env_name, bundle_id=bundle) if env_name else None
        port = (cfg.metro_port if cfg and cfg.metro_port else fallback[0])
        return port, (project["id"] if project else fallback[1])
    except Exception as e:                       # config/DB unavailable
        logger.warning("metro target lookup failed for %s (%s) — using defaults",
                       bundle, e)
        return fallback


def ensure_business_metro(udid: str, bundle: str = BUSINESS_BUNDLE) -> bool:
    """Start the Business app's own Metro on 8082 and point the app at it.

    Returns True when :8082 answers. Without this the app stalls on a BLANK
    splash because 8081 serves the Consumer bundle. `bundle` is the app actually
    being driven — pass the STAGING bundle for staging runs, else its
    RCT_jsLocation is never set and the staging app shows blank (this bit a fresh
    iPhone that had never had the default persisted).
    """
    port, project_id = _business_metro_target(bundle)
    # Already up?
    try:
        if httpx.get(f"http://localhost:{port}/status", timeout=3).status_code == 200:
            pass
        else:
            raise RuntimeError("not running")
    except Exception:
        repo = rm.get_repo_path(project_id)
        env = dict(os.environ, NODE_OPTIONS="--max-old-space-size=8192",
                   RCT_METRO_PORT=str(port))
        try:
            subprocess.Popen(
                ["npx", "react-native", "start", "--port", str(port)],
                cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception as e:
            logger.warning("Could not start Business Metro: %s", e)
            return False
        for _ in range(60):
            try:
                if httpx.get(f"http://localhost:{port}/status", timeout=3).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)

    # Point the app at its own packager (RCTBundleURLProvider reads this).
    want = f"localhost:{port}"
    # Generous timeouts: a freshly created simulator indexes media for its first
    # half hour (mediaanalysisd at 500%+ CPU on Intel), and `simctl spawn` then
    # took >15s -- the write was skipped and the app kept "No bundle URL". An
    # unreadable current value is treated as "not set", never as a reason to skip.
    try:
        cur = subprocess.run(
            ["xcrun", "simctl", "spawn", udid, "defaults", "read", bundle,
             "RCT_jsLocation"], capture_output=True, text=True, timeout=90,
        ).stdout.strip()
    except Exception:
        cur = ""
    try:
        if cur != want:
            subprocess.run(
                ["xcrun", "simctl", "spawn", udid, "defaults", "write", bundle,
                 "RCT_jsLocation", want],
                check=False, timeout=90,
            )
            # The bundle URL is read once, at launch. An instance already running
            # (deployment launches it) keeps its red "No bundle URL" screen, and
            # activate_app only brings that instance forward -- so end it and let
            # the session start a fresh one that reads the new location.
            subprocess.run(["xcrun", "simctl", "terminate", udid, bundle],
                           capture_output=True, timeout=60)
    except Exception as e:
        logger.warning("Could not set RCT_jsLocation for %s: %s", bundle, e)
    try:
        return httpx.get(f"http://localhost:{port}/status", timeout=3).status_code == 200
    except Exception:
        return False


def ensure_app_metro(udid: str, bundle: str) -> bool:
    """Any React Native app: its environment's Metro running, and *udid*'s copy of
    the app pointed at it. Same mechanism as the Business app, which was the only
    one that got it -- so the Consumer app on a simulator it had never run on
    asked the default :8081 and showed "No bundle URL present"."""
    return ensure_business_metro(udid, bundle)


def _options(udid: str, bundle_id: str, wda_port: int) -> XCUITestOptions:
    o = XCUITestOptions()
    o.platform_name = "iOS"
    o.automation_name = "XCUITest"
    o.udid = udid
    o.bundle_id = bundle_id
    o.no_reset = True
    # Keep the session alive through long idle gaps. Appium's default newCommandTimeout is
    # 60s — but this flow leaves a session idle far longer: a PRE-WARMED business session
    # waits out the whole ~3-min consumer segment, and idb-heavy steps (@add_all_products)
    # go >60s without an Appium call. Both made the session TERMINATE mid-run ("A session is
    # either terminated or not started" — the #1 flaky failure). 20 min covers any segment.
    o.set_capability("newCommandTimeout", 1200)
    # WDA launch/build ceiling. A FRESH sim (e.g. the phone the first time it runs the B-app)
    # has NO prebuilt WebDriverAgent, so Appium compiles it from scratch — that took ~188s and
    # BLEW the old 180s limit, so Appium dropped the session-creation connection and the client
    # saw 'RemoteDisconnected: Remote end closed connection without response'. 360s gives the
    # first-ever build room; once built, usePrebuiltWDA reuses it and subsequent launches are fast.
    # WDA config from the central resolver. Distinct port per session so the two
    # simulators run concurrently — that part was already right here, so the
    # explicit port is passed through and pinned unchanged (8100 consumer /
    # 8101 business). What was missing is derivedDataPath: this path set
    # usePrebuiltWDA=True while never telling Appium WHERE the prebuilt build is,
    # so Appium could not shortcut to launching it.
    from automation.appium_service import wda as _wda
    _wda.apply(o, udid=udid, wda_port=wda_port)
    # The Business app fires a native "Send You Notifications" permission alert on
    # launch that sits ON TOP of the login form and swallows every tap/keystroke.
    o.set_capability("autoAcceptAlerts", True)
    # ── Anti-hang: the Vya RN apps animate constantly (spinners, loaders), so
    # XCUITest's default "wait for the app to be idle" before every command never
    # settles and each tap/find HANGS for minutes. Disable quiescence waiting and
    # keep snapshots shallow/fast so commands return promptly on this big UI tree.
    o.set_capability("waitForQuiescence", False)      # don't block on app idle
    o.set_capability("waitForIdleTimeout", 0)         # 0s idle wait
    o.set_capability("shouldWaitForQuiescence", False)
    o.set_capability("maxTypingFrequency", 30)
    # NOTE: do NOT cap snapshotMaxDepth here — this app's cards (e.g. NylaiKitchen2)
    # live deep in the tree, and a shallow cap made the resolver miss them without
    # actually speeding snapshots up. waitForQuiescence=False is what stops the hangs.
    return o


def _fill_field(d, name: str, text: str) -> bool:
    """Type into a React-Native TextInput by its accessibility name.

    The name resolves to a wrapper View (type=Other); the real input is the
    XCUIElementTypeTextField/SecureTextField underneath. Typing into the wrapper
    is a no-op, so target the field type explicitly.
    """
    from appium.webdriver.common.appiumby import AppiumBy
    els = d.find_elements(
        AppiumBy.IOS_PREDICATE,
        f'name == "{name}" AND (type == "XCUIElementTypeTextField" '
        f'OR type == "XCUIElementTypeSecureTextField")',
    )
    if not els:
        return False
    els[0].click()
    time.sleep(0.5)
    # CLEAR FIRST. The apps run with noReset:True, so a field can already hold the previous
    # session's value — send_keys APPENDS to it. That produced a login e-mail of
    # 'emp2A@xorstack.rstack.comemp2A@xo@xorst...' ("Please enter valid email address"), and
    # every retry concatenated more, so the run reported "login rejected / wrong creds" when
    # the credentials were fine. clear() is a no-op on an empty field.
    try:
        els[0].clear()
        time.sleep(0.2)
    except Exception:
        pass
    # Type, then VERIFY, and retype character-by-character if the field dropped
    # anything. Measured on the iPhone 16 business sim: send_keys('emp2A@…') left
    # the field holding 'emA@…' — the 'p' and '2' were swallowed by the RN
    # TextInput mid-burst. The old code logged that mismatch and returned False,
    # but the caller submitted the mangled value regardless, so a perfectly good
    # credential produced three "bounced back to the sign-in screen" attempts and
    # was reported as an app-side Firebase problem.
    def _value():
        try:
            return els[0].get_attribute("value") or ""
        except Exception:
            return None

    for attempt in (1, 2, 3):
        if attempt > 1:
            try:
                els[0].click(); time.sleep(0.3); els[0].clear(); time.sleep(0.3)
            except Exception:
                pass
        if attempt < 3:
            els[0].send_keys(text)
        else:
            # Last resort: one character at a time, which the input keeps up with.
            for ch in text:
                els[0].send_keys(ch)
                time.sleep(0.06)
        time.sleep(0.3)
        got = _value()
        if got is None:
            return True                       # cannot read it back; assume typed
        if not got or got == text or "•" in got or "●" in got:
            return True                       # match, or a masked secure field
        logger.warning("_fill_field(%s): typed %r but field holds %r (attempt %d/3)",
                       name, text, got, attempt)
    return False


class CrossAppOrchestrator:
    """Runs both apps in lock-step. Emits phase results via an optional callback."""

    def __init__(
        self,
        run_id: str,
        consumer_udid: str = DEFAULT_CONSUMER_UDID,
        business_udid: Optional[str] = None,
        waiter_udid: Optional[str] = None,
        kitchen_udid: Optional[str] = None,
        credentials: Optional[dict] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.run_id = run_id
        self.consumer_udid = consumer_udid
        # Waiter and kitchen may be the same iPad (switch accounts) or two
        # devices. business_udid is kept as a back-compat alias for the waiter.
        self.waiter_udid = waiter_udid or business_udid or DEFAULT_BUSINESS_UDID
        self.kitchen_udid = kitchen_udid or self.waiter_udid
        self.business_udid = self.waiter_udid
        # {role: {email, password}} — used to log the Business app in.
        self.credentials = credentials or {}
        self.on_event = on_event or (lambda e: None)

        # Cross-app sync barriers — mirror the scenario's sync_events.
        self.ev: Dict[str, threading.Event] = {
            name: threading.Event() for name in
            ("order_placed", "table_accepted", "kitchen_accepted",
             "items_served", "payment_requested", "payment_completed")
        }
        # Per-phase outcome collected from each side: {phase: {"consumer":.., "business":..}}
        self._results: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

    # ── result recording ─────────────────────────────────────────────────────

    def _record(self, phase_num: str, phase_name: str, side: str, status: str, note: str):
        with self._lock:
            slot = self._results.setdefault(phase_num, {
                "name": phase_name, "consumer": "N/A", "business": "N/A",
                "reasons": [], "launch": None,
            })
            slot[side] = status
            if note:
                slot["reasons"].append(f"[{side}] {note}")
        self.on_event({"type": "phase_result", "phase": phase_num, "side": side,
                       "status": status, "note": note})

    def _persist_phase(self, phase_num: str):
        """Upsert ONE ScenarioResult row per phase.

        Both threads call this for the same phase (consumer when it finishes its
        part, business when it finishes its part). It must merge into a single
        row — an insert-per-call produced duplicate rows in the Scenarios tab.
        """
        with self._lock:
            slot = self._results.get(phase_num)
            if not slot:
                return
            c, b = slot["consumer"], slot["business"]
            overall = "FAIL" if (c == "FAIL" or b == "FAIL") else \
                      ("PASS" if (c == "PASS" or b == "PASS") else "N/A")
            reasons = list(slot["reasons"])
            name = slot["name"]
        with SessionLocal() as db:
            row = (
                db.query(ScenarioResult)
                .filter_by(run_id=self.run_id, scenario_num=phase_num)
                .first()
            )
            if row is None:
                row = ScenarioResult(run_id=self.run_id, scenario_num=phase_num)
                db.add(row)
            row.scenario_name = name
            row.status = overall
            row.consumer_status = c
            row.business_status = b
            row.error = "; ".join(r for r in reasons if "FAIL" in r) or None
            row.reasons = reasons

            run = db.query(TestRun).filter_by(id=self.run_id).first()
            if run is not None:
                db.flush()
                rows = db.query(ScenarioResult).filter_by(run_id=self.run_id).all()
                run.status = "failed" if any(r.status == "FAIL" for r in rows) else "running"
            db.commit()

    # ── the two role threads ─────────────────────────────────────────────────

    def _run_consumer(self):
        repo = rm.get_repo_path(CONSUMER_PROJECT_ID)
        try:
            d = webdriver.Remote(APPIUM_URL, options=_options(self.consumer_udid, CONSUMER_BUNDLE, 8100))
        except Exception as e:
            self._record("1", "Book slot", "consumer", "FAIL", f"could not start session: {e}")
            self._persist_phase("1")
            for name in self.ev:  # unblock the business side so it doesn't hang
                self.ev[name].set()
            return
        r = ScenarioRunner(d, CONSUMER_BUNDLE, screenshot_dir=None)
        try:
            d.activate_app(CONSUMER_BUNDLE); time.sleep(6)

            # Phase 1 — book the slot
            book = ["open app", "click Nylai kitchen2", "click Reserve a table",
                    "click 1 hr", "click Today", "click bookAppoitment"]
            ok = all(self._step(r, s, "1", "Book slot", "consumer") for s in book)
            self.ev["order_placed"].set()
            self._persist_phase("1")

            # Phase 2 — after the business accepts, add product + checkout
            self.ev["table_accepted"].wait(timeout=90)
            add = ["click Menu", "add Tagliatelle", "click cartImage", "click cartCheckout"]
            for s in add:
                self._step(r, s, "2", "Add + checkout", "consumer")
                if r.app_crash():
                    self._record("2", "Add + checkout", "consumer", "FAIL",
                                 f"app crashed: {r.app_crash()}")
                    break
            self.ev["order_placed"].set()
            self._persist_phase("2")

            # Phase 3 — pay once the business requests payment
            self.ev["payment_requested"].wait(timeout=90)
            for s in ["click ePayment", "click Pay", "verify Your order is confirmed"]:
                self._step(r, s, "3", "Pay", "consumer")
            self.ev["payment_completed"].set()
            self._persist_phase("3")
        finally:
            try: d.quit()
            except Exception: pass

    def _business_login(self, r: ScenarioRunner) -> bool:
        """Log the waiter/kitchen user in, if the login screen is showing.

        Credentials come from the environment (VYA_BUSINESS_USER /
        VYA_BUSINESS_PASSWORD) — never hardcoded. Returns True when past login.
        """
        if not r._resolve([BIZ_SIGNIN_BTN]):
            return True                          # already logged in
        # Prefer the run's configured waiter credentials; fall back to env.
        waiter = self.credentials.get("waiter", {})
        user = waiter.get("email") or os.getenv("VYA_BUSINESS_WAITER_USER", "")
        pw = waiter.get("password") or os.getenv("VYA_BUSINESS_WAITER_PASSWORD", "")
        if not user or not pw:
            self._record("0", "Business login", "business", "FAIL",
                         "no waiter credentials configured — set them in the "
                         "cross-app run dialog or VYA_BUSINESS_WAITER_* env")
            self._persist_phase("0")
            return False
        try:
            from appium.webdriver.common.appiumby import AppiumBy
            _fill_field(r.d, BIZ_EMAIL_FIELD, user)
            _fill_field(r.d, BIZ_PASSWORD_FIELD, pw)
            try: r.d.hide_keyboard()
            except Exception: pass
            # Accept the Terms & Conditions checkbox — Sign In needs it.
            cb = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "clickCheckBox")
            if cb:
                cb[0].click(); time.sleep(0.4)
            r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, BIZ_SIGNIN_BTN)[0].click()
            time.sleep(8)
            ok = not r._resolve([BIZ_SIGNIN_BTN])
            self._record("0", "Business login", "business",
                         "PASS" if ok else "FAIL",
                         "signed in" if ok else "sign-in did not advance")
            self._persist_phase("0")
            return ok
        except Exception as ex:
            self._record("0", "Business login", "business", "FAIL", f"login error: {ex}")
            self._persist_phase("0")
            return False

    def _run_business(self):
        # Bring up the Business app's OWN Metro (8082) and point the app at it,
        # or it stalls on its splash forever.
        metro_ok = ensure_business_metro(self.business_udid)

        try:
            d = webdriver.Remote(APPIUM_URL, options=_options(self.business_udid, BUSINESS_BUNDLE, 8101))
        except Exception as e:
            self._record("1", "Book slot", "business", "FAIL", f"could not start session: {e}")
            self._persist_phase("1")
            self.ev["table_accepted"].set()
            self.ev["payment_requested"].set()
            return
        r = ScenarioRunner(d, BUSINESS_BUNDLE, screenshot_dir=None)
        try:
            d.terminate_app(BUSINESS_BUNDLE)
            d.activate_app(BUSINESS_BUNDLE)
            time.sleep(12)                       # allow the 8082 bundle to load

            if not metro_ok:
                note = "Business Metro (:8082) did not come up — app cannot load."
                for ph, nm in (("1", "Book slot"), ("2", "Kitchen accept"), ("3", "Bill settle")):
                    self._record(ph, nm, "business", "FAIL", note)
                    self._persist_phase(ph)
                self.ev["table_accepted"].set(); self.ev["payment_requested"].set()
                return

            # Log in (waiter/kitchen user). Without creds we stop here honestly.
            if not self._business_login(r):
                for ph, nm in (("1", "Book slot"), ("2", "Kitchen accept"), ("3", "Bill settle")):
                    self._record(ph, nm, "business", "FAIL", "not logged in")
                    self._persist_phase(ph)
                self.ev["table_accepted"].set(); self.ev["payment_requested"].set()
                return

            # ── Phase 1: waiter accepts the reservation ──────────────────────
            self.ev["order_placed"].wait(timeout=120)
            self._step(r, "accept the reservation", "1", "Book slot", "business")
            self.ev["table_accepted"].set()
            self._persist_phase("1")

            # ── Phase 2: kitchen accepts + serves the order ──────────────────
            self.ev["order_placed"].wait(timeout=120)
            self._step(r, "accept the order in the kitchen", "2", "Kitchen accept", "business")
            self._step(r, "mark items served", "2", "Kitchen accept", "business")
            self.ev["kitchen_accepted"].set()
            self._persist_phase("2")

            # ── Phase 3: request payment / verify the bill settled ───────────
            self._step(r, "request payment", "3", "Bill settle", "business")
            self.ev["payment_requested"].set()
            self.ev["payment_completed"].wait(timeout=120)
            self._step(r, "verify the bill is settled", "3", "Bill settle", "business")
            self._persist_phase("3")
        finally:
            try: d.quit()
            except Exception: pass

    # ── helpers ──────────────────────────────────────────────────────────────

    def _step(self, runner: ScenarioRunner, step: str, phase_num: str,
              phase_name: str, side: str) -> bool:
        res = runner.run_one(step, 0)
        self._record(phase_num, phase_name, side,
                     "PASS" if res.ok else "FAIL",
                     res.action if res.ok else (res.detail or res.action))
        self.on_event({"type": "step", "side": side, "step": step,
                       "ok": res.ok, "action": res.action, "detail": res.detail})
        return res.ok

    def run(self):
        """Run both sides in parallel and mark the run terminal when both finish."""
        self.on_event({"type": "start", "run_id": self.run_id})
        ct = threading.Thread(target=self._run_consumer, name="consumer", daemon=True)
        bt = threading.Thread(target=self._run_business, name="business", daemon=True)
        ct.start(); bt.start()
        ct.join(); bt.join()

        with SessionLocal() as db:
            run = db.query(TestRun).filter_by(id=self.run_id).first()
            if run is not None:
                rows = db.query(ScenarioResult).filter_by(run_id=self.run_id).all()
                run.status = "failed" if any(r.status == "FAIL" for r in rows) else "passed"
                run.job_state = run.status
                run.completed_at = datetime.utcnow()
                db.commit()
        self.on_event({"type": "done", "run_id": self.run_id})


def start_cross_app_run(consumer_udid: str = DEFAULT_CONSUMER_UDID,
                        business_udid: Optional[str] = None,
                        waiter_udid: Optional[str] = None,
                        kitchen_udid: Optional[str] = None,
                        credentials: Optional[dict] = None) -> str:
    """Create a run row and kick off the orchestrator in a background thread.
    Returns the run_id immediately."""
    from automation.database import database
    waiter = waiter_udid or business_udid or DEFAULT_BUSINESS_UDID
    kitchen = kitchen_udid or waiter
    same = "shared" if waiter == kitchen else "split"
    run_id = str(uuid.uuid4())
    with SessionLocal() as db:
        database.insert_test_run(db, {
            "id": run_id, "project_id": CONSUMER_PROJECT_ID,
            "test_suite": "Vyapy cross-app (iOS)",
            "test_name": f"Consumer + Business ({same} waiter/kitchen)",
            "status": "running", "job_state": "running",
            "started_at": datetime.utcnow(), "created_at": datetime.utcnow(),
            "device_name": f"C:{consumer_udid[:6]} W:{waiter[:6]} K:{kitchen[:6]}",
            "platform": "iOS", "bot_type": "ios-crossapp",
        })
    orch = CrossAppOrchestrator(
        run_id, consumer_udid, waiter_udid=waiter, kitchen_udid=kitchen,
        credentials=credentials,
    )
    threading.Thread(target=orch.run, name=f"crossapp-{run_id[:8]}", daemon=True).start()
    return run_id


if __name__ == "__main__":
    rid = start_cross_app_run()
    print("cross-app run:", rid)
    # keep the main thread alive while the daemon orchestrator runs
    import time as _t
    while True:
        with SessionLocal() as db:
            r = db.query(TestRun).filter_by(id=rid).first()
            if r and r.status in ("passed", "failed"):
                print("final:", r.status)
                break
        _t.sleep(3)
