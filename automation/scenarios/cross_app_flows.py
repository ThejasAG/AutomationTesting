"""The six major cross-app flows, as sequential scripts + a runner.

Unlike ``cross_app_orchestrator`` (Consumer + Business in parallel with sync
barriers), these flows are **sequential** with **account switching** on the
Business iPad — exactly what the end-to-end scenarios need.

  Flow 1  preorder → pay CARD in C-App → waiter assign/send → kitchen ready
          → waiter serve/notify → pay in B-App (amount>bill, verify change) → close
  Flow 2  same as 1, but the final payment is made in the C-App
  Flow 3  order LATER (no preorder) → waiter assigns + ADDS items → kitchen
          → waiter serve/notify → pay in B-App → close
  Flow 4  same as 3, but the final payment is made in the C-App
  Flow 5  waiter CREATES the appointment (roopa D, 9686496589) → consumer accepts
          it in the wallet → waiter adds items → kitchen → serve/notify → pay B-App
  Flow 6  same as 5, but the final payment is made in the C-App

A flow is an ordered list of SEGMENTS. Each segment runs on one role's
device/app/account:

  role=consumer  → Consumer app on the iPhone   (account: roopa)
  role=waiter    → Business app on the iPad      (account: emp2A)
  role=kitchen   → Business app on the SAME iPad (account: kemp2A)

Steps are plain-English (resolved by ScenarioRunner) OR ``@tokens`` the runner
handles specially:
  @add_all_products   — add every product on the menu, one by one, selecting a
                        modifier when a product requires one (there is no "add all").
  @pay:<method>       — open the payment section, pick the method (cash/card/coupon),
                        read the bill, pay MORE than it, confirm, and verify the
                        change calculation (emits a "Bill check" reason → rich report).
  @logout_business    — Menu → "tap here to logout" (Business app).
  @logout_consumer    — Menu → "tap here to logout" (Consumer app).

IDs reuse the verified building-block scenarios; the few that need a live screen
are marked ``# TUNE`` and fail loudly (never silently) so they can be corrected.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy

from automation.database.config import SessionLocal
from automation.database import database
from automation.database.models import ScenarioResult, TestRun
from automation.intelligence.scenario_runner import ScenarioRunner
from automation.scenarios.cross_app_orchestrator import (
    APPIUM_URL, CONSUMER_BUNDLE, BUSINESS_BUNDLE, ENV_BUNDLES,
    DEFAULT_CONSUMER_UDID, DEFAULT_BUSINESS_UDID, DEFAULT_BUSINESS_PHONE_UDID,
    _options, _fill_field, ensure_business_metro, _business_metro_target,
)
from automation.scenarios import cross_app_config as cfgmod
from automation.scenarios import ui_health as _uih

logger = logging.getLogger("cross_app_flows")

# Which app pair each environment drives. "prod" = the old live Vya apps;
# "staging" = the separate STG-* builds (own bundle ids, pointed at vya.xorstack.com).

def bundle_for_env(bundle_id: str, env: str) -> str:
    """The same app's bundle id in *env*, or *bundle_id* unchanged if it is not one
    of ours. Saved scenarios store a fixed bundle, so switching a single scenario
    between Live and Staging means translating it — the pair is already in
    ENV_BUNDLES, so look it up there rather than string-munging a "staging" suffix.
    """
    for role_map in ENV_BUNDLES.values():
        for role, bid in role_map.items():
            if bid == bundle_id:
                return ENV_BUNDLES.get(env, role_map).get(role, bundle_id)
    return bundle_id


# The consumer whose booking the waiter opens. The Business "My Bookings" screen
# labels the card with the diner's full name (e.g. "Roopa D"). One-line fix here.
CONSUMER_NAME = "Roopa"
STEP_TIMEOUT = 240   # hard per-step ceiling (s): a wedged Appium resolve once hung a run 21 min.
# Raised from 150 because real steps on this host now run close to it: measured
# @consumer_home 85.6s and 'click NylaiKitchen2' 123.3s, both flagged SLOW LOAD.
# With RAM exhausted (swap 7.3G of 8G) those times swing run to run, so a 150s
# ceiling made flow_book_demo pass or fail by luck — 2 of 11 runs. This is
# headroom for a slow machine, NOT permission for a step to hang: a genuinely
# wedged resolve still dies, just 90s later.
# Auto-prune failure screenshots: keep them for only the most recent N runs so the DB can't
# grow without bound (~230 KB/shot). Older runs keep their result + reason, just not the image.
SCREENSHOT_KEEP_RUNS = 25
# ONLY these exact ids use the idb coordinate fast-path in _smart_click. They are clean,
# on-screen modal buttons that Appium's full-tree snapshot hangs on (e.g. preOrderBooking's
# 'Booking Confirmed' modal sits over the huge menu tree). Everything else keeps the proven
# Appium resolver — the idb path is too blunt for cards like NylaiKitchen2 (it matched a
# same-named text label and tapped the wrong restaurant).
_IDB_CLICK_IDS = {"preOrderBooking", "orderLater"}
# When paying "more than the bill", how much over the bill to tender (to verify change).
PAY_OVER_BY = 10.0


# ── Reusable segment step blocks ───────────────────────────────────────────
_C_BOOK_PREFIX = [
    "open app",
    "@consumer_home",                 # app resumes on its last screen → back out to Home
    "click NylaiKitchen2",            # restaurant card is listed directly on Home
    "select Any",                     # dining area — NylaiKitchen2 shows slots under 'Any'
    "select 1 hr",
    "@first_time_slot",               # tap the first available time chip (e.g. 17:15)
    "@book_appointment",
]

# Consumer: pre-order items and pay by CARD. Flow confirmed from the live app:
# preOrderBooking → menu (add via +) → "CHECKOUT (n)" → Cart "CHECKOUT" →
# Stripe sheet (Visa ••••4242 preselected) "Pay € X" → "Pre-Order Confirmed".
_C_PREORDER_PAY_CARD = _C_BOOK_PREFIX + [
    "click preOrderBooking",          # confirm pre-order → opens the restaurant menu
    "@add_all_products",              # tap product card → detail → confirmProduct
    "@to_checkout",                   # open cart (cartImage) → cartCheckout → Stripe
    "@pay_stripe",                    # card preselected (TEST MODE) → tap "Pay € X"
    "@got_it",                        # dismiss "Pre-Order Confirmed" (YES, GOT IT)
]

# Consumer: book, then choose ORDER LATER (no pre-order, no pay yet).
_C_ORDER_LATER = _C_BOOK_PREFIX + [
    "click orderLater",
]

# Consumer: accept the waiter-created appointment from the wallet (flows 5/6).
_C_ACCEPT_APPT = [
    "open app",
    "click walletTab",                # bottom tab (confirmed id)
    "@wait_screen:wallet",            # shell AND the bookings list, or report why not
    "accept the appointment",         # TUNE: open the pending event + Accept
]

# Consumer: settle the final bill from the C-App (Wallet → Checkout → re-checkout
# → pay-for → proceed). Labels confirmed from the live payment screens.
_C_PAY_FINAL = [
    "open app",
    "click walletTab",                # bottom tab (confirmed id)
    "click Checkout",                 # wallet card: "Left to pay … / Checkout"
    "click YES, RE-CHECKOUT",         # "Re-Initiate Checkout?" dialog
    "click Me Only",                  # "Would you also like to pay for…"
    "click Proceed with the payment",
    "verify payment successful",
]

# Waiter: create the appointment for the diner (flows 5/6). Mirrors the verified
# 'Business: Book event (waiter)' block, with roopa's details.
_W_CREATE_APPT = [
    "click addNewEvent",
    "@wait_form",                     # form renders a beat later — wait or the first type() misses
    "type roopa in firstName",
    "type D in lastName",
    "@hide_keyboard",                 # keyboard covers 'Any' — tapping it types 'k' otherwise
    "click anyBtn",
    "@first_time_slot",               # idb (~2s), NOT the plain-text slow MATCHES predicate (10-20 min)
    "type 9686496589 in mobileInputBtn",   # default guest count (1 adult) — the recorded flow adds no count step
    "@hide_keyboard",                 # the number keyboard covers Save — dismiss it or the tap hits a key
    "scroll down",
    "@save_appointment",              # tap Save and CONFIRM the form actually closed
]

# Waiter: open the diner's reservation, assign a table, send to kitchen. UNIFIED — works for
# BOTH pre-ordered bookings (items already there) and order-later ones (@ensure_order_items
# adds items only if the order is empty), then sends to the kitchen.
_W_ASSIGN_SEND = [
    "@open_reservation",             # open the diner's Reserved booking (needs 30-min window)
    "@assign_table",                 # 'Select a table' modal: first free table + Confirm
    "@ensure_order_items",           # add items ONLY if there's no pre-order
    "click selectAllItemsBtn",       # real id (OrderSummary.js)
    "click sendItemsBtn",            # real id
]
# Kept as an alias — same unified flow (adds items when needed).
_W_ASSIGN_ADDALL_SEND = _W_ASSIGN_SEND

# Kitchen: mark the in-progress order ready + close (dynamic item-select, no hard-coded name).
_K_READY = [
    "@kitchen_ready",
]

# Waiter: serve the items, then notify the table it's time to pay.
# NOTE the leading @open_order. This segment always follows the KITCHEN segment, which shares
# the one iPad and therefore logs the waiter out and back in — that lands on the bookings board,
# NOT on the order screen these steps act on. Without it, 'click selectAllItemsBtn' hunts a
# button that isn't on screen and hangs the full 150s step timeout before failing the segment.
_W_SERVE_NOTIFY = [
    "@open_order",
    "click selectAllItemsBtn",
    "click serveItemsBtn",
    "click notifyPaymentBtn",
    "click Yes",
]

# Waiter: settle in the B-App via E-Payment (amount>bill, verify change), close.
_W_PAY_CLOSE = [
    "@pay:epay",                      # E-Payment: enter amount > bill → verify change
    "click closeTableBtn",
]

# Waiter: just close the table (used when the consumer paid in the C-App).
_W_CLOSE = [
    "click closeTableBtn",
]

# Waiter: serve → notify → settle by a SPECIFIC method (cash / voucher) → close.
# Ported from the bot's PAY scenarios; reuses the verified @pay:<method> handler.
_W_SERVE_PAY_CASH = _W_SERVE_NOTIFY + ["@pay:cash", "click closeTableBtn"]
_W_SERVE_PAY_VOUCHER = _W_SERVE_NOTIFY + ["@pay:voucher", "click closeTableBtn"]


def _seg(num: str, name: str, role: str, steps: List[str]) -> Dict[str, Any]:
    return {"num": num, "name": name, "role": role, "steps": steps}


#: A row inside the hour's events sidebar (AddCountModal -> OrderCard). The card has
#: no accessibilityLabel, so idb reports its children's text concatenated, e.g.
#: '4657 <icon> RESERVED 13:45 - 14:45 <icon> 13:38'. The 'HH:MM - HH:MM' booking
#: window is the distinguishing part: the board's cards never render it, which is why
#: eight bookings in one hour all read 'RoopaDcardReserved' there and are
#: indistinguishable, while here each one states its own time.
_SIDEBAR_ROW_RE = re.compile(r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b")

#: Returned instead of a card label when the booking was opened straight from the
#: events sidebar. A distinct object, not "" or None, so the "no card found" paths
#: cannot mistake a success for a failure.
_OPENED_VIA_SIDEBAR = object()


def _norm(label) -> str:
    """Compare UI labels without caring about case, spacing or punctuation.

    The same control is spelled "SIGN-IN", "Sign In" and "signIn" across screens and
    builds; matching raw strings means keeping a list of spellings in sync forever,
    and a missed variant reads as "the screen isn't there".
    """
    return re.sub(r"[^a-z0-9]", "", str(label or "").lower())


def _safe_displayed(el) -> bool:
    """el.is_displayed() that never raises — a stale/detached element counts as hidden."""
    try:
        return bool(el.is_displayed())
    except Exception:
        return False


def _appium_binary() -> str:
    """Locate the `appium` executable. shutil.which() fails inside the flow's process
    because the daemon's PATH lacks nvm/homebrew bins — so also probe the usual spots."""
    import glob
    import os as _os
    import shutil
    found = shutil.which("appium")
    if found:
        return found
    for pat in (_os.path.expanduser("~/.nvm/versions/node/*/bin/appium"),
                "/opt/homebrew/bin/appium", "/usr/local/bin/appium",
                _os.path.expanduser("~/.local/bin/appium")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]   # newest node version last
    return "appium"


def _idb_binary() -> str:
    """Locate the `idb` executable. shutil.which() fails in the flow's daemon process
    (PATH lacks /usr/local/bin & the pip user-bin), which made every idb call throw
    FileNotFoundError — silently breaking the fast idb steps. Probe the usual spots."""
    import glob
    import os as _os
    import shutil
    found = shutil.which("idb")
    if found:
        return found
    for c in ("/usr/local/bin/idb", "/opt/homebrew/bin/idb"):
        if _os.path.exists(c):
            return c
    hits = sorted(glob.glob(_os.path.expanduser("~/Library/Python/*/bin/idb")))
    return hits[-1] if hits else "idb"


_IDB = _idb_binary()   # resolved once at import


# ── The six flows ──────────────────────────────────────────────────────────
FLOWS: Dict[str, Dict[str, Any]] = {
    "flow1": {
        "id": "flow1",
        "name": "Preorder → C-App card → pay in B-App",
        "description": "Consumer books, pre-orders every item, pays by CARD; waiter "
                       "assigns + sends; kitchen readies; waiter serves + notifies and "
                       "settles the bill in the Business app (tender > bill, verify change).",
        "segments": [
            _seg("1", "C-App: book + preorder + pay (card)", "consumer", _C_PREORDER_PAY_CARD),
            _seg("2", "Waiter: assign table + send to kitchen", "waiter", _W_ASSIGN_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify + pay (B-App) + close", "waiter",
                 _W_SERVE_NOTIFY + _W_PAY_CLOSE),
        ],
    },
    "flow2": {
        "id": "flow2",
        "name": "Preorder → C-App card → pay in C-App",
        "description": "Same as Flow 1, but after the waiter notifies payment the bill "
                       "is settled from the CONSUMER app; the waiter then closes the table.",
        "segments": [
            _seg("1", "C-App: book + preorder + pay (card)", "consumer", _C_PREORDER_PAY_CARD),
            _seg("2", "Waiter: assign table + send to kitchen", "waiter", _W_ASSIGN_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify payment", "waiter", _W_SERVE_NOTIFY),
            _seg("5", "C-App: pay the bill", "consumer", _C_PAY_FINAL),
            _seg("6", "Waiter: close the table", "waiter", _W_CLOSE),
        ],
    },
    "flow3": {
        "id": "flow3",
        "name": "Order later → waiter adds items → pay in B-App",
        "description": "Consumer books and chooses ORDER LATER; the waiter assigns a "
                       "table, ADDS every item, sends to kitchen; kitchen readies; waiter "
                       "serves + notifies and settles in the Business app.",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: assign + add items + send to kitchen", "waiter", _W_ASSIGN_ADDALL_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify + pay (B-App) + close", "waiter",
                 _W_SERVE_NOTIFY + _W_PAY_CLOSE),
        ],
    },
    "flow4": {
        "id": "flow4",
        "name": "Order later → waiter adds items → pay in C-App",
        "description": "Same as Flow 3, but the bill is settled from the CONSUMER app; "
                       "the waiter then closes the table.",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: assign + add items + send to kitchen", "waiter", _W_ASSIGN_ADDALL_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify payment", "waiter", _W_SERVE_NOTIFY),
            _seg("5", "C-App: pay the bill", "consumer", _C_PAY_FINAL),
            _seg("6", "Waiter: close the table", "waiter", _W_CLOSE),
        ],
    },
    "flow5": {
        "id": "flow5",
        "name": "Waiter-created appointment → pay in B-App",
        "description": "No C-App booking: the waiter CREATES the appointment (roopa D, "
                       "9686496589); the consumer accepts it from the wallet; the waiter "
                       "adds every item, sends to kitchen; kitchen readies; waiter serves "
                       "+ notifies and settles in the Business app.",
        "segments": [
            _seg("1", "Waiter: create appointment (roopa D)", "waiter", _W_CREATE_APPT),
            _seg("2", "C-App: accept the appointment (wallet)", "consumer", _C_ACCEPT_APPT),
            _seg("3", "Waiter: assign + add items + send to kitchen", "waiter", _W_ASSIGN_ADDALL_SEND),
            _seg("4", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("5", "Waiter: serve + notify + pay (B-App) + close", "waiter",
                 _W_SERVE_NOTIFY + _W_PAY_CLOSE),
        ],
    },
    "flow6": {
        "id": "flow6",
        "name": "Waiter-created appointment → pay in C-App",
        "description": "Same as Flow 5, but the bill is settled from the CONSUMER app; "
                       "the waiter then closes the table.",
        "segments": [
            _seg("1", "Waiter: create appointment (roopa D)", "waiter", _W_CREATE_APPT),
            _seg("2", "C-App: accept the appointment (wallet)", "consumer", _C_ACCEPT_APPT),
            _seg("3", "Waiter: assign + add items + send to kitchen", "waiter", _W_ASSIGN_ADDALL_SEND),
            _seg("4", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("5", "Waiter: serve + notify payment", "waiter", _W_SERVE_NOTIFY),
            _seg("6", "C-App: pay the bill", "consumer", _C_PAY_FINAL),
            _seg("7", "Waiter: close the table", "waiter", _W_CLOSE),
        ],
    },
    # ── Ported from the bot's PAY scenarios (real element IDs, verified engine) ──
    "flow_pay_cash": {
        "id": "flow_pay_cash",
        "name": "Preorder → settle by CASH in B-App (PAY1)",
        "description": "Consumer books + pre-orders; waiter assigns + sends; kitchen readies; "
                       "waiter serves + notifies and settles the bill by CASH (tender > bill, "
                       "verify change), then closes. Ported from Vya-agentic-Bot PAY1.",
        "segments": [
            _seg("1", "C-App: book + preorder + pay (card)", "consumer", _C_PREORDER_PAY_CARD),
            _seg("2", "Waiter: assign table + send to kitchen", "waiter", _W_ASSIGN_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify + pay CASH + close", "waiter", _W_SERVE_PAY_CASH),
        ],
    },
    "flow_pay_voucher": {
        "id": "flow_pay_voucher",
        "name": "Preorder → settle by FOOD VOUCHER in B-App (PAY4)",
        "description": "Same as the cash flow but the waiter settles the bill with a FOOD "
                       "VOUCHER, then closes. Ported from Vya-agentic-Bot PAY4.",
        "segments": [
            _seg("1", "C-App: book + preorder + pay (card)", "consumer", _C_PREORDER_PAY_CARD),
            _seg("2", "Waiter: assign table + send to kitchen", "waiter", _W_ASSIGN_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify + pay VOUCHER + close", "waiter", _W_SERVE_PAY_VOUCHER),
        ],
    },
    # Short, single-app flow for LIVE demos: fast + low-risk (no cross-app handoff, no
    # payment). Proves the engine really drives the app end-to-end in ~2-3 minutes.
    "flow_book_demo": {
        "id": "flow_book_demo",
        "name": "Quick demo — Consumer books a table (~2 min)",
        "description": "The Consumer opens the app, picks NylaiKitchen2, Any · 1 hr, taps a "
                       "time slot, and books. One app, ~7 fast exact-id steps — the safe "
                       "thing to run live in front of people (completes right at booking).",
        "segments": [
            _seg("1", "C-App: book a table (Any · 1 hr)", "consumer", _C_BOOK_PREFIX),
        ],
    },
    # Waiter quick demo — self-contained (no consumer needed): the waiter logs in and
    # CREATES a booking on the Business iPad. The waiter-side parallel of the consumer demo.
    "flow_waiter_demo": {
        "id": "flow_waiter_demo",
        "name": "Quick demo — Waiter creates a booking (~2 min)",
        "description": "The waiter logs into the Business iPad, opens 'add new event', fills "
                       "the diner's name, picks a time slot and saves — one app, real exact-id "
                       "steps. Self-contained (no consumer needed), safe to run live.",
        "segments": [
            _seg("1", "Waiter: create a booking", "waiter", _W_CREATE_APPT),
        ],
    },
    # Kitchen quick demo — the kitchen logs in and marks the current in-progress order ready,
    # then closes it. NOTE: needs an in-progress order to exist (a waiter must have sent one);
    # otherwise it reports 'no in-progress order' and stops cleanly.
    "flow_kitchen_demo": {
        "id": "flow_kitchen_demo",
        "name": "Quick demo — Kitchen marks an order ready",
        "description": "The kitchen logs into the Business iPad, opens an in-progress order in the "
                       "queue, selects its items, marks them Ready and closes the ticket. Acts on "
                       "an order already in the kitchen queue — the kitchen's real job. (A "
                       "self-contained 'waiter creates + sends' setup isn't possible from the "
                       "B-app alone: a waiter-created booking stays ConfirmationPending until the "
                       "consumer accepts it in the C-app, so use a full cross-app flow to produce "
                       "a fresh order end-to-end.) REQUIRES a queued order: with an empty kitchen "
                       "queue this demo FAILS — that is an unmet precondition, not a broken app.",
        "segments": [
            _seg("1", "Kitchen: mark the order ready", "kitchen", _K_READY),
        ],
    },
}


# Every step token the runner understands, for the editor's picker. Anything else is
# passed to the fuzzy click/type resolver, so free text still works — this is a menu,
# not a whitelist.
STEP_CATALOG: Dict[str, List[Dict[str, str]]] = {
    "Handlers (@)": [
        {"step": "@consumer_home", "help": "Reach the C-app Home tab"},
        {"step": "@first_time_slot", "help": "Pick the first bookable time chip"},
        {"step": "@book_appointment", "help": "Tap BOOK NOW and confirm the dialog opened"},
        {"step": "@add_all_products", "help": "Add products to the order"},
        {"step": "@to_checkout", "help": "Open the cart and go to checkout"},
        {"step": "@pay_stripe", "help": "Pay on the Stripe sheet"},
        {"step": "@got_it", "help": "Dismiss the confirmation dialog"},
        {"step": "@wait_form", "help": "Wait for a form to render before typing"},
        {"step": "@hide_keyboard", "help": "Dismiss the keyboard covering a button"},
        {"step": "@save_appointment", "help": "Tap Save and confirm the form closed"},
        {"step": "@open_reservation", "help": "Open the diner's Reserved booking"},
        {"step": "@open_order", "help": "Re-open the in-progress order after a role switch"},
        {"step": "@assign_table", "help": "Assign a table on the Select A Table sheet"},
        {"step": "@ensure_order_items", "help": "Add items only if the order is empty"},
        {"step": "@kitchen_ready", "help": "Mark a queued order Ready"},
        {"step": "@pay:epay", "help": "Settle by E-Payment"},
        {"step": "@pay:cash", "help": "Settle by cash"},
        {"step": "@pay:voucher", "help": "Settle by food voucher"},
        {"step": "@logout_business", "help": "Sign out of the Business app"},
        {"step": "@logout_consumer", "help": "Sign out of the Consumer app"},
    ],
    "Common": [
        {"step": "open app", "help": "Foreground the role's app"},
        {"step": "click <id>", "help": "Tap by accessibility id, e.g. click saveBtn"},
        {"step": "type <text> in <field>", "help": "e.g. type roopa in firstName"},
        {"step": "select <text>", "help": "Tap by visible text, e.g. select Any"},
        {"step": "scroll down", "help": "Swipe down to reveal lower controls"},
        {"step": "verify <text>", "help": "Assert text is on screen"},
    ],
}

_ROLES = ("consumer", "waiter", "kitchen")


def _db_flows() -> Dict[str, Dict[str, Any]]:
    """User-edited/created flows from the DB, keyed by flow id. Never raises — if the
    table is missing (older DB) the built-ins still work."""
    try:
        from automation.database.config import SessionLocal
        from automation.database.models import CrossAppFlowEdit
        with SessionLocal() as db:
            return {r.id: r.to_dict() for r in db.query(CrossAppFlowEdit).all()}
    except Exception as e:
        logger.debug("cross-app flow overrides unavailable: %s", e)
        return {}


def resolve_flow(flow_id: str) -> Optional[Dict[str, Any]]:
    """The definition a RUN should use: a DB edit wins over the built-in of the same id."""
    edit = _db_flows().get(flow_id)
    if edit and edit.get("segments"):
        return {"id": flow_id, "name": edit["name"],
                "description": edit.get("description") or "",
                "segments": edit["segments"]}
    return FLOWS.get(flow_id)


def list_flows() -> List[Dict[str, Any]]:
    """Flow metadata for the UI (id, name, description, segment outline).

    Built-ins merged with DB edits: an edited built-in shows its edited steps and is
    flagged `custom`, so the editor can offer 'revert to built-in'."""
    edits = _db_flows()
    out: List[Dict[str, Any]] = []
    for f in FLOWS.values():
        e = edits.get(f["id"])
        src = e if (e and e.get("segments")) else f
        out.append({
            "id": f["id"], "name": src.get("name") or f["name"],
            "description": src.get("description") or f.get("description", ""),
            "builtin": True, "edited": bool(e and e.get("segments")),
            "segments": [{"num": s["num"], "name": s["name"], "role": s["role"],
                          "steps": list(s["steps"])} for s in src["segments"]],
        })
    for fid, e in edits.items():                 # brand-new flows (no built-in twin)
        if fid in FLOWS:
            continue
        out.append({
            "id": fid, "name": e["name"], "description": e.get("description") or "",
            "builtin": False, "edited": True,
            "segments": [{"num": s.get("num", str(i + 1)), "name": s.get("name", ""),
                          "role": s.get("role", "consumer"), "steps": list(s.get("steps") or [])}
                         for i, s in enumerate(e.get("segments") or [])],
        })
    return out


# ── Runner ─────────────────────────────────────────────────────────────────
_AMOUNT_RE = re.compile(r'[€]\s*([\d]{1,6}(?:[.,]\d{2}))|(?<![\d.])(\d{1,6}\.\d{2})\s*€?')


# Live in-process flow runs, by run_id -> the Event that asks one to stop.
#
# The Stop button used to be COSMETIC for these runs: `stop_run` wrote
# status='stopped' to the database and nothing else, while the daemon thread kept
# driving the simulators to the end of the flow. Two visible consequences: the
# devices stayed busy long after the user thought the run was over, and the
# thread's own `finally` later overwrote 'stopped' with passed/failed — so a
# stopped run un-stopped itself.
#
# A thread cannot be safely killed in Python, and killing this one would be wrong
# anyway: it holds live Appium sessions that must be quit() or the simulator is
# left wedged for the next run. So cancellation is COOPERATIVE — the runner checks
# this Event between steps and segments and unwinds through its normal finally,
# quitting every driver on the way out.
_ACTIVE_FLOW_RUNS: Dict[str, "threading.Event"] = {}
_ACTIVE_FLOW_RUNS_LOCK = threading.Lock()


def request_flow_stop(run_id: str) -> bool:
    """Ask an in-process flow run to stop. True if one was live to be asked.

    Returns False for a run that is queued, already finished, or executing on a
    distributed agent rather than in this process — the caller still marks the
    database, which is what stops those.
    """
    with _ACTIVE_FLOW_RUNS_LOCK:
        ev = _ACTIVE_FLOW_RUNS.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


def flow_run_is_active(run_id: str) -> bool:
    """Whether this process is currently running that flow."""
    with _ACTIVE_FLOW_RUNS_LOCK:
        return run_id in _ACTIVE_FLOW_RUNS


class FlowRunner:
    """Execute one flow's segments in order, switching device/app/account."""

    def __init__(self, run_id: str, flow: Dict[str, Any],
                 devices: Dict[str, str], credentials: Dict[str, Dict[str, str]],
                 on_event: Optional[Callable[[dict], None]] = None,
                 env: str = "prod"):
        self.run_id = run_id
        self.flow = flow
        self.devices = devices
        self.credentials = credentials
        self.on_event = on_event or (lambda e: None)
        # Which app pair to drive: "prod" (old Vya) or "staging" (STG-* apps).
        self.env = env if env in ENV_BUNDLES else "prod"
        self.consumer_bundle = ENV_BUNDLES[self.env]["consumer"]
        self.business_bundle = ENV_BUNDLES[self.env]["business"]
        self._sessions: Dict[str, Any] = {}
        self._runners: Dict[str, ScenarioRunner] = {}
        self._biz_account: Optional[str] = None
        self._biz_metro_ready = False
        # Set by request_flow_stop() when the user presses Stop. Checked between
        # steps and segments; never kills the thread (see _ACTIVE_FLOW_RUNS).
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- session management --------------------------------------------------
    def _business_udid(self) -> str:
        # Waiter and kitchen MUST share the iPad (account switch). Kitchen is
        # sometimes misconfigured onto the iPhone — force it to the waiter iPad.
        return self.devices.get("waiter") or DEFAULT_BUSINESS_UDID

    def _session_for(self, role: str) -> ScenarioRunner:
        if role == "consumer":
            udid, bundle, wda = (self.devices.get("consumer") or DEFAULT_CONSUMER_UDID,
                                 self.consumer_bundle, 8100)
        else:
            udid, bundle, wda = self._business_udid(), self.business_bundle, 8101
            if not self._biz_metro_ready:
                # Pass the ACTUAL business bundle (staging vs prod) so its RCT_jsLocation is
                # pointed at Metro:8082 — otherwise the staging app shows a BLANK splash on a
                # device that never had that default set (e.g. a fresh iPhone).
                self._biz_metro_ready = ensure_business_metro(udid, self.business_bundle)
        self._cur_udid = udid
        if udid not in self._sessions:
            d = webdriver.Remote(APPIUM_URL, options=_options(udid, bundle, wda))
            d.activate_app(bundle)
            self._wait_app_ready(d, bundle)   # poll instead of a blind sleep(10)
            # Debug builds stack LogBox warnings over the UI; clear them once here
            # so the first real step doesn't tap into an overlay.
            # CONSUMER ONLY: the dismiss control's coordinates were verified on the
            # phone. The iPad's layout differs and an unverified tap there is a way
            # to break a segment that otherwise works, so leave the business app to
            # its own (already working) handling.
            self._cur_udid = udid
            if role == "consumer":
                self._clear_logbox(udid)
            self._sessions[udid] = d
            self._runners[udid] = ScenarioRunner(d, bundle, screenshot_dir=None)
        return self._runners[udid]

    @staticmethod
    def _wait_app_ready(d, bundle: str, timeout: float = 12.0) -> None:
        """Return as soon as the app is foreground AND has rendered something tappable,
        instead of blindly waiting 10s. Debug builds fetch the JS bundle on launch, so
        the first render can lag — but it's usually ~1-2s, not 10."""
        from appium.webdriver.common.appiumby import AppiumBy
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if d.query_app_state(bundle) == 4:  # 4 = running in foreground
                    # A rendered control means the JS bundle loaded and UI is up.
                    if d.find_elements(AppiumBy.IOS_PREDICATE,
                                       "type == 'XCUIElementTypeButton' OR "
                                       "type == 'XCUIElementTypeStaticText'"):
                        time.sleep(0.4)   # tiny settle for the first frame
                        return
            except Exception:
                pass
            time.sleep(0.5)

    # -- diagnostics ---------------------------------------------------------
    _ID_RE = re.compile(r'(Btn|Card|Tab|Input|Item|Cart|Checkout|Pay|Order|Book|Slot|'
                        r'Confirm|Add|Menu|Preorder|PreOrder|coupon|cash|card|Wallet|'
                        r'Accept|assign|Assign|serve|Serve|ready|Ready|close|Close|'
                        r'notify|Notify|product|Product|Total|counter|Reserved|signIn|'
                        r'save|Save|any|Any)', re.I)

    def _visible_ids(self, r: ScenarioRunner, limit: int = 28) -> List[str]:
        """Compact list of interesting element ids on the current screen — recorded
        on a failed step so a single run self-documents the real UI for tuning.

        Uses idb (fast, ~2s) instead of Appium page_source (which can stall for
        minutes on this app's huge accessibility tree)."""
        import subprocess, json as _json
        out: List[str] = []
        udid = getattr(self, "_cur_udid", "") or ""
        if udid:
            try:
                raw = subprocess.run([_IDB, "ui", "describe-all", "--udid", udid],
                                     capture_output=True, text=True, timeout=20).stdout
                for e in _json.loads(raw):
                    for k in ("AXIdentifier", "AXLabel", "AXValue"):
                        v = (e.get(k) or "").strip()
                        if (v and len(v) <= 34 and " " not in v and v not in out
                                and self._ID_RE.search(v)):
                            out.append(v)
                if out:
                    return out[:limit]
            except Exception:
                pass
        # fallback: Appium page_source (slower)
        try:
            for n in re.findall(r'name="([^"]+)"', r.d.page_source):
                n = n.strip()
                if n and len(n) <= 34 and ' ' not in n and n not in out and self._ID_RE.search(n):
                    out.append(n)
        except Exception:
            pass
        return out[:limit]

    # -- generic UI helpers --------------------------------------------------
    def _tap_text_contains(self, r: ScenarioRunner, substr: str) -> bool:
        """Tap the first element whose label/name contains substr (case-insensitive)."""
        try:
            els = r.d.find_elements(
                AppiumBy.IOS_PREDICATE,
                f'label CONTAINS[c] "{substr}" OR name CONTAINS[c] "{substr}" '
                f'OR value CONTAINS[c] "{substr}"')
            if els:
                els[0].click()
                time.sleep(1.2)
                return True
        except Exception:
            pass
        return False

    def _read_bill_total(self, r: ScenarioRunner) -> Optional[float]:
        """Largest €-amount on the current screen ≈ the bill total."""
        try:
            xml = r.d.page_source
        except Exception:
            return None
        vals = []
        for m in _AMOUNT_RE.finditer(xml):
            raw = m.group(1) or m.group(2) or ""
            raw = raw.replace(",", ".")
            try:
                vals.append(float(raw))
            except ValueError:
                pass
        return max(vals) if vals else None

    def _type_amount(self, r: ScenarioRunner, amount: float) -> bool:
        """Type an amount into the first editable field on the payment screen."""
        try:
            els = r.d.find_elements(
                AppiumBy.IOS_PREDICATE,
                'type == "XCUIElementTypeTextField" OR type == "XCUIElementTypeSecureTextField"')
            if not els:
                return False
            els[0].click(); time.sleep(0.3)
            els[0].send_keys(f"{amount:.2f}")
            return True
        except Exception:
            return False

    # -- business account switching -----------------------------------------
    def _logout_business(self, r: ScenarioRunner) -> None:
        """Sign out of the Business app so we can switch accounts (waiter <-> kitchen).
        Verified on-device: the Menu opens via an APPIUM tap on `menuBtn` (an idb coordinate
        tap does NOT navigate), and the control is `logOutBtn` (NOT a "logout" text). A confirm
        dialog may follow. We poll the role state to confirm we actually reached the sign-in
        screen, retrying the whole thing once."""
        # Every step below used to swallow its exception and return None, so when a
        # run reported "logout did not reach the sign-in screen (still 'waiter')"
        # there was NOTHING recorded about whether menuBtn was even found, whether
        # logOutBtn was clicked, or what the app did next — the only visible signal
        # was the role, which cannot tell "the button was missing" apart from "the
        # tap did nothing". Record the trail; behaviour is unchanged.
        self._last_logout_trail = []
        trail = self._last_logout_trail
        for _attempt in range(2):
            trail.append(f"attempt {_attempt + 1}: role={self._biz_role_state()}")
            # 1) Open the Menu. MEASURED: 'menuBtn' exists in NEITHER build (grep of
            #    App/Screens and App/MobileScreens returns nothing), so this always
            #    found 0 and the menu never opened — then logOutBtn, which lives on
            #    the Menu screen, was clicked while OFF-SCREEN and did nothing. The
            #    phone reaches it via the bottom tab labelled 'Menu' at (292,753).
            opened_menu = False
            for ident in ("menuBtn", "menuTab", "Menu"):
                try:
                    e = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
                except Exception as ex:
                    trail.append(f"{ident} ERROR {type(ex).__name__}")
                    continue
                trail.append(f"{ident} found={len(e)}")
                if not e:
                    continue
                try:
                    e[0].click(); time.sleep(1.5)
                except Exception as ex:
                    trail.append(f"{ident} click ERROR {type(ex).__name__}")
                    continue
                # VERIFY the menu actually opened — logOutBtn must be on screen now.
                try:
                    lo_now = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "logOutBtn")
                    shown = bool(lo_now) and bool(lo_now[0].is_displayed())
                except Exception:
                    shown = False
                trail.append(f"{ident} clicked -> logOutBtn displayed={shown}")
                if shown:
                    opened_menu = True
                    break
            if not opened_menu:
                trail.append("menu did not open by any known control")
            # 2) Tap the real logout button.
            try:
                lo = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "logOutBtn")
                trail.append(f"logOutBtn found={len(lo)}")
                if lo:
                    lo[0].click(); time.sleep(1.5)
                    trail.append("logOutBtn clicked")
            except Exception as ex:
                trail.append(f"logOutBtn ERROR {type(ex).__name__}")
            # 3) Confirm if a dialog appears (Yes / Logout / logOutBtn again).
            #    Measured on this build: no confirm dialog appears — logout goes
            #    straight to the sign-in screen. Kept for builds that do show one.
            for c in ("Yes", "confirmLogout", "logoutConfirm", "Logout", "logOutBtn"):
                try:
                    cc = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, c)
                    if cc:
                        cc[0].click(); trail.append(f"confirmed via '{c}'"); break
                except Exception:
                    pass
            # 4) Verify we actually reached the sign-in screen.
            for _ in range(6):
                time.sleep(2)
                if self._biz_role_state() == "login":
                    trail.append("reached sign-in")
                    return
            trail.append(f"still role={self._biz_role_state()} after ~12s")
        logger.warning("business logout did not reach the sign-in screen — switch may fail; "
                       "trail: %s", " | ".join(trail))

    # Positive markers for the Business app's screens, read via idb (reliable on the huge
    # tree, unlike the fuzzy Appium resolver which can miss an element that IS present).
    # Waiter and kitchen are two accounts on the SAME app whose HOME screens differ:
    #   waiter  → addNewEvent / qrScaner / allBtn·tableBtn·pickupBtn (booking tabs)
    #   kitchen → kitchenAllBtn·kitchenTableBtn·kitchenPickupBtn / inProgressOrderCard
    _BIZ_LOGIN_IDS = ("signInBtn", "emailValue", "passwordValue")
    _BIZ_WAITER_IDS = ("addNewEvent", "qrScaner", "modifyTable", "orderFilterBtn")
    _BIZ_KITCHEN_IDS = ("kitchenAllBtn", "kitchenTableBtn", "kitchenPickupBtn",
                        "inProgressOrderCard", "completedOrderCard")

    def _biz_bundle_source(self) -> str:
        """Which Metro the business app is pointed at, and whether it is serving.

        A blank/unknown screen is nearly always this: the wrong packager (or none).
        Reading it back from the device turns a guess into a fact in the report.
        """
        import subprocess as _sp
        udid = self._business_udid()
        loc = "?"
        try:
            out = _sp.run(["xcrun", "simctl", "spawn", udid, "defaults", "read",
                           self.business_bundle, "RCT_jsLocation"],
                          capture_output=True, text=True, timeout=15).stdout.strip()
            loc = out or "(unset)"
        except Exception:
            pass
        expected, _pid = _business_metro_target(self.business_bundle)
        alive = "?"
        try:
            import httpx as _hx
            alive = "up" if _hx.get(f"http://localhost:{expected}/status",
                                    timeout=3).status_code == 200 else "down"
        except Exception:
            alive = "down"
        verdict = ("OK" if loc.endswith(str(expected))
                   else f"WRONG — {self.business_bundle} must load from :{expected}")
        return (f"bundle source: RCT_jsLocation={loc} (expected localhost:{expected}, "
                f"metro {expected} is {alive}) -> {verdict}")

    def _biz_role_state(self) -> str:
        """Which role is CURRENTLY signed into the Business app — 'waiter', 'kitchen',
        'login' (signed out), or 'unknown' (still loading). Decides by what is actually on
        screen, so we can tell a persisted waiter session from a kitchen one across runs."""
        els = self._idb_els()
        ids = {e["id"] for e in els} | {e["label"] for e in els}
        if any(i in ids for i in self._BIZ_LOGIN_IDS):
            return "login"
        if any(i in ids for i in self._BIZ_KITCHEN_IDS):
            return "kitchen"
        if any(i in ids for i in self._BIZ_WAITER_IDS):
            return "waiter"
        return "unknown"

    def _biz_screen_state(self) -> str:
        """Coarser check: 'login', 'home' (either role signed in), or 'unknown'."""
        role = self._biz_role_state()
        if role == "login":
            return "login"
        if role in ("waiter", "kitchen"):
            return "home"
        return "unknown"

    def _login_business(self, r: ScenarioRunner, account: str,
                        notes: Optional[List[str]] = None) -> bool:
        creds = self.credentials.get(account, {})
        user, pw = creds.get("email", ""), creds.get("password", "")
        if not user or not pw:
            return False

        def _note(m: str) -> None:
            if notes is not None:
                notes.append(m)

        try:
            # 1) Detect the actual role/screen first — wait out any splash/loading.
            role = self._biz_role_state()
            for _ in range(8):                      # ~16s for the app to settle
                if role in ("login", "waiter", "kitchen"):
                    break
                time.sleep(2)
                role = self._biz_role_state()
            if role == account:
                _note(f"    · already logged in as {account} (detected {role} home) — skipping sign-in")
                return True

            # 'unknown' after the settle loop means the app never rendered a screen
            # we recognise — no sign-in fields, no waiter home, no kitchen home. That
            # is an ENVIRONMENT fault, not a credentials one, and it used to be
            # reported as "signed in as 'unknown' but need 'waiter' — logging out",
            # then "could not log in (check creds / T&C checkbox)". MEASURED cause:
            # the staging B-app was pointed at the PROD packager (RCT_jsLocation
            # localhost:8082 serves repo 1519bec5 -> api.vyapy.com) while the run
            # signed in with staging credentials, so it sat on a blank splash.
            # Say which packager the app is actually on, so this is one glance.
            if role == "unknown":
                # RECOVER ONCE before blaming anything. A leftover screen from an
                # earlier run parks the app somewhere with no role markers — MEASURED:
                # the phone sat on the assign-a-table screen ('Please assign a Table' +
                # T0AssignAnyBtn + AssignTableBtn), which is a perfectly valid screen
                # but matches neither the sign-in nor either home. noReset keeps it
                # across runs, so every later run inherits it.
                if self._table_modal_up():
                    _note("    · a leftover table sheet is up — dismissing it")
                    self._dismiss_table_modal(r, notes if notes is not None else [])
                else:
                    # The role markers all live on the bookings board (Home tab).
                    # A waiter sitting on Orders/History/Menu is perfectly signed in
                    # yet reads as 'unknown' — MEASURED: the app resumed on "My
                    # Orders" (In Queue 00 / ALL / TABLE / PICKUP) and a relaunch
                    # just returned to the same tab, so recovery never converged.
                    # Tap Home first; only relaunch if that does not reveal a
                    # recognisable screen.
                    # Tap it through idb: Appium does not resolve the tab-bar item
                    # by accessibility id (find_elements("Home") -> 0), but idb
                    # reports it as a labelled element at the bottom of the screen.
                    try:
                        tab = next((e for e in self._idb_els()
                                    if e["label"].strip() == "Home" and e["h"] > 20), None)
                        if tab:
                            self._idb_tap(tab["cx"], tab["cy"])
                            time.sleep(2.5)
                            _note("    · unrecognised screen — tapped the Home tab")
                    except Exception:
                        pass
                if self._biz_role_state() == "unknown" and not self._table_modal_up():
                    _note("    · app is on an unrecognised screen — relaunching once")
                    try:
                        r.d.terminate_app(self.business_bundle); time.sleep(1.5)
                        r.d.activate_app(self.business_bundle)
                    except Exception as e:
                        _note(f"    · relaunch note: {type(e).__name__}")
                role = self._biz_role_state()
                for _ in range(8):                  # ~16s to settle after recovery
                    if role in ("login", "waiter", "kitchen"):
                        break
                    time.sleep(2)
                    role = self._biz_role_state()
                if role == account:
                    _note(f"    · already logged in as {account} after recovery")
                    return True

            if role == "unknown":
                _note(f"    · [FAIL] the Business app never rendered a known screen "
                      f"(role='unknown' after ~16s + one recovery) — it is not a "
                      f"credentials problem")
                _note(f"    ↳ {self._biz_bundle_source()}")
                els_now = self._idb_els()
                _note(f"    ↳ on screen: {[e['label'] or e['id'] for e in els_now if (e['label'] or e['id'])][:12]}")
                return False

            if role != "login":
                # A DIFFERENT role's home is up. This used to dead-end here on the assumption
                # that the caller had already logged out — but the caller decides BEFORE the
                # app has settled, so it often reads 'unknown', skips the logout, and leaves
                # us here. The run then failed at step 1 with "cannot sign in here" after
                # ~11 minutes of waiting, while holding both the diagnosis AND the fix.
                # This loop is the first place that knows the REAL role, so act on it.
                _note(f"    · signed in as '{role}' but need '{account}' — logging out to switch")
                self._logout_business(r)
                role = self._biz_role_state()
                for _ in range(8):                  # ~16s for the sign-in screen to render
                    if role == "login":
                        break
                    time.sleep(2)
                    role = self._biz_role_state()
                if role != "login":
                    _note(f"    · [FAIL] logout did not reach the sign-in screen "
                          f"(still '{role}') — cannot switch to {account}")
                    _note(f"    ↳ logout trail: "
                          f"{' | '.join(getattr(self, '_last_logout_trail', [])) or 'not recorded'}")
                    return False

            # 2) Fill, agree to T&C, submit — and RETRY, because this login is racy.
            #
            # The B-app calls Firebase getToken() right after a successful sign-in, and on a
            # SIMULATOR that throws:
            #   "Possible unhandled promise rejection: Error: [messaging/unregistered] You must
            #    be registered for remote messages before calling getToken"
            # The home renders (homeBtn/historyBtn/menuBtn appear in the tree) and the app then
            # falls back to the sign-in screen. The credentials are fine — it is a race with the
            # app's own unhandled rejection, which is why the same account signs in on one run
            # and not the next. A single attempt therefore fails intermittently for a reason no
            # amount of credential-checking will explain, so attempt it a few times.
            ATTEMPTS = 3
            for attempt in range(1, ATTEMPTS + 1):
                if self._biz_role_state() != "login":
                    break                            # a retry may have already landed us home
                _fill_field(r.d, "emailValue", user)
                _fill_field(r.d, "passwordValue", pw)
                try:
                    r.d.hide_keyboard()
                except Exception:
                    pass
                # The T&C box is REQUIRED — Sign In stays disabled without it. It is also reset
                # every time the app bounces back to the sign-in screen, so re-tick each attempt.
                self._tick_tc_checkbox(r, _note)
                btns = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "signInBtn")
                if btns:
                    btns[0].click()

                # 3) VERIFY the CORRECT role's home appears (the shared login resolves waiter vs
                #    kitchen server-side from the account). Poll for it — never assume success.
                for _ in range(12):                  # ~24s
                    time.sleep(2)
                    if self._biz_role_state() == account:
                        if attempt > 1:
                            _note(f"    · signed in as {account} on attempt {attempt}/{ATTEMPTS} "
                                  f"(the app bounced back to login on the earlier tries — "
                                  f"Firebase getToken rejects on a simulator)")
                        return True
                got = self._biz_role_state()
                if got != "login":
                    break                            # somewhere unexpected — stop retrying
                if attempt < ATTEMPTS:
                    _note(f"    · attempt {attempt}/{ATTEMPTS}: bounced back to the sign-in "
                          f"screen (app-side getToken rejection) — retrying")
            got = self._biz_role_state()
            _note(f"    · submitted credentials {ATTEMPTS}x but the {account} home never stuck "
                  f"(now on '{got}'). The home DOES render briefly, so this is the app's "
                  f"unhandled Firebase getToken rejection on a simulator, not bad credentials.")
            return False
        except Exception as e:
            logger.warning("business login (%s) error: %s", account, e)
            return False

    def _book_appointment(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Tap BOOK NOW and CONFIRM the booking dialog actually opened.

        `click bookAppoitment` reported [ok] while the reservation form stayed put:
        Appium found the button and dispatched a click, which says nothing about
        whether the app acted on it. The button only becomes live once the chosen
        time chip has been committed, so a tap fired too soon is swallowed
        silently — and the next step then hunts for `orderLater` on a screen that
        never changed, burning its full 150s timeout before failing.

        The dialog appearing IS the success signal, so poll for it and retry.
        """
        MARKERS = {"orderLater", "preOrderBooking", "appointmentId"}

        def dialog_open() -> bool:
            # Must go through idb. These are GenericElements, which Appium's
            # collapsed snapshot never reports — checking via r._resolve() would
            # return False no matter how well the tap worked, so the step could
            # never confirm its own success.
            return any(e.get("label") in MARKERS for e in self._idb_els())

        if dialog_open():
            notes.append("[ok] @book_appointment — booking dialog already open")
            return True

        for attempt in range(1, 4):
            # Tap through idb, not Appium. A plain element click reports [ok] while
            # the form does not move: BOOK NOW sits under the same collapsed-tree
            # problem as the restaurant cards, so the click lands on a stale or
            # covered node. An idb tap at the button's real centre works first time.
            # A LogBox toast sits along the bottom edge, right over BOOK NOW, and
            # eats the tap. Clear it first or the tap is silently swallowed.
            n = self._clear_logbox()
            if n:
                notes.append(f"[ok] @book_appointment — cleared {n} LogBox overlay(s)")
            # POLL for the button; do not judge on one snapshot. BOOK NOW renders only
            # after the chosen time slot has been committed, and on a loaded host that
            # commit is slow — the preceding @first_time_slot has been measured at
            # 114.2s. A single idb read taken the instant the slot step returned found
            # no button and failed the flow outright, on a screen where BOOK NOW did
            # appear moments later. ~24s of polling costs nothing when it is already
            # there (first read wins) and saves the run when the app is merely slow.
            btn = None
            for _try in range(12):
                btn = next((e for e in self._idb_els()
                            if e.get("label") == "bookAppoitment"), None)
                if btn:
                    if _try:
                        notes.append(f"[ok] @book_appointment — BOOK NOW appeared after "
                                     f"~{_try * 2}s")
                    break
                time.sleep(2)
            if btn:
                self._idb_tap(btn["cx"], btn["cy"])
            elif attempt == 1:
                notes.append("[FAIL] @book_appointment — BOOK NOW is not on screen "
                             "(polled ~24s). On screen: "
                             f"{[e['label'] or e['id'] for e in self._idb_els() if e['label'] or e['id']][:16]}")
                return False
            # Give the dialog a moment; the app re-renders after committing the slot.
            for _ in range(5):
                time.sleep(1.5)
                if dialog_open():
                    notes.append(f"[ok] @book_appointment — booking confirmed"
                                 f"{' (retry %d)' % attempt if attempt > 1 else ''}")
                    return True
            notes.append(f"[warn] @book_appointment — tap {attempt} did not open the "
                         f"dialog; retrying")
            time.sleep(1.0)

        notes.append("[FAIL] @book_appointment — BOOK NOW tapped 3x but the booking "
                     "dialog never opened (form still on screen)")
        return False

    def _tap_save_btn(self) -> bool:
        """Tap Save on the New Appointment form, AROUND the LogBox toast that covers it.

        Measured on the iPad, with the form open:
            saveBtn  GenericElement  x 832..1179  y 685..735
            toast    GenericElement  x  10..1200  y 708..756   <- drawn on top
        The strip overlaps the button's lower half, so Appium's .click() — which always
        aims at the element CENTRE (y=710) — lands on the toast and is swallowed. The
        form then just sits there and @save_appointment burned its full 150s timeout
        every run, reporting "tapped Save but the form is still open".

        Clearing it first is attempted but cannot be relied on. _clear_logbox() does
        find this toast (its text "8 Deprecation warning: …" matches _LOGBOX on
        "Warning:"), yet its dismiss tap at the strip's right edge does not close it on
        the iPad — measured: six taps at (1180, 732), the toast still up. The iPhone
        geometry that tap was derived from simply does not transfer.

        So aim at the part of the button the strip does NOT cover instead. Verified
        live: a tap at y=694 saved the appointment immediately, and the invitation
        appeared on the Consumer wallet.
        """
        els = self._idb_els()
        btn = next((e for e in els if (e["label"] or "").strip() == "saveBtn"), None)
        if btn is None:
            return False
        y = btn["cy"]
        covering = [t for t in self._logbox_strips(els)
                    if t is not btn
                    and t["x"] <= btn["cx"] <= t["x"] + t["w"]
                    and t["y"] <= y <= t["y"] + t["h"]]
        if covering:
            clear_y = btn["y"] + 9                # just inside the button's top edge
            if clear_y < min(t["y"] for t in covering):   # ...if that is actually uncovered
                y = clear_y
        return self._idb_tap(btn["cx"], int(y))

    @staticmethod
    def _logbox_strips(els: List[dict]) -> List[dict]:
        """EVERY collapsed LogBox strip on screen, not just the first.

        _logbox_strip() returns one, which is all its dismissal caller needs. Here it is
        the wrong answer: a debug build STACKS them, and on the live iPad the first in
        idb's list ("3 no valid aps-environment …", y 761.5) is NOT the one covering
        saveBtn ("8 Deprecation warning …", y 708). Testing only the first one said
        "nothing covers the button", the tap kept aiming at the centre, and the fix
        looked like it had done nothing.
        """
        screen_w = max((e.get("w") or 0 for e in els), default=0)
        if not screen_w:
            return []
        return [e for e in els
                if e.get("type") == "GenericElement"
                and 40 <= (e.get("h") or 0) <= 60
                and (e.get("w") or 0) >= 0.9 * screen_w]

    def _save_appointment(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Tap Save on the New Appointment form and CONFIRM the form closed.

        `click saveBtn` on its own reported [ok] while the form stayed open and no booking was
        created — Appium found the element and dispatched a click, which says nothing about
        whether the app accepted it. The form staying up IS the failure signal, so poll for it
        to disappear and retry. A LogBox toast sits along the bottom edge, right where Save is,
        and can swallow the tap, so clear it before each try."""
        FORM_MARKERS = ("saveBtn", "New Appointment", "firstName", "closeEventModal")

        def form_open() -> bool:
            labels = {(e["label"] or "").strip() for e in self._idb_els()}
            labels |= {(e["id"] or "").strip() for e in self._idb_els()}
            return any(m in labels for m in FORM_MARKERS)

        for attempt in range(1, 4):
            # r.dismiss_logbox() was here. It cannot see this toast at all — a
            # GenericElement, which Appium's collapsed tree never reports. _clear_logbox()
            # (idb) does see it, so use that; but do not RELY on it, because tapping the
            # strip's right edge does not close it on the iPad (see _tap_save_btn).
            self._clear_logbox()
            tapped = self._tap_save_btn()
            if not tapped:
                try:                                  # no idb node -> fall back to Appium
                    els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "saveBtn")
                    if els:
                        els[0].click()
                        tapped = True
                except Exception:
                    pass
            if not tapped:
                notes.append(f"[FAIL] @save_appointment — no saveBtn on screen (attempt {attempt})")
                return False
            for _ in range(8):                       # ~16s for the form to close
                time.sleep(2)
                if not form_open():
                    notes.append(f"[ok] @save_appointment — saved; the form closed"
                                 f"{'' if attempt == 1 else f' (attempt {attempt}/3)'}")
                    return True
            if attempt < 3:
                notes.append(f"    · @save_appointment — tapped Save but the form is still "
                             f"open (attempt {attempt}/3) — clearing overlays and retrying")
                try:
                    r.run_one("scroll down", 0)
                except Exception:
                    pass
        notes.append("[FAIL] @save_appointment — tapped Save 3x and the New Appointment form "
                     "never closed, so NO booking was created. The tap reaches the button but "
                     "the app does not accept it (validation, or the LogBox toast over the "
                     "bottom edge eating the press).")
        return False

    def _tick_tc_checkbox(self, r, _note) -> bool:
        """Tick the Terms & Conditions box on the Business sign-in screen, and VERIFY it.

        `clickCheckBox` is the whole row ("By Signing in I agree to the Terms & Conditions"):
        w=339, h=17 on the iPhone. Appium's .click() hits the element CENTRE — which is the
        middle of the TEXT, not the square at the far left, and on the Terms & Conditions link
        it can even navigate away and clear the form. The square sits ~10pt in from the row's
        left edge; a tap there ticks it and Sign In goes from disabled-pale to enabled.

        The box exposes no `value` and signInBtn reports enabled=True even while visually
        disabled, so neither can confirm the tick. A screenshot hash can: if the pixels don't
        change, the tap missed. Verify rather than announce success — the old code logged
        "ticked the checkbox (appium)" unconditionally, so a login blocked by an UNTICKED box
        looked like bad credentials in every report.
        """
        import io
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput

        # Sample ONLY the checkbox square, not the whole screen. A full-screen hash is useless
        # here: the LogBox toasts re-render and bump their counters constantly, so the hash
        # changes on its own and the tick reads as "verified" when the box is still empty —
        # which then surfaces as a mysterious "login rejected / wrong creds".
        win = None
        try:
            win = r.d.get_window_size()
        except Exception:
            pass

        def box_sig(rect) -> tuple:
            """Mean RGB of the checkbox square. Ticking fills it, so this shifts a lot."""
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(r.d.get_screenshot_as_png())).convert("RGB")
                sx = im.width / (win["width"] if win else im.width)
                sy = im.height / (win["height"] if win else im.height)
                h = rect["height"]
                x0, y0 = int(rect["x"] * sx), int(rect["y"] * sy)
                crop = im.crop((x0, y0, int(x0 + h * sx * 1.4), int(y0 + h * sy)))
                px = list(crop.getdata())
                if not px:
                    return ()
                n = len(px)
                return (round(sum(p[0] for p in px) / n),
                        round(sum(p[1] for p in px) / n),
                        round(sum(p[2] for p in px) / n))
            except Exception:
                return ()

        def tap(x: int, y: int) -> None:
            a = ActionBuilder(r.d, mouse=PointerInput("touch", "finger"))
            a.pointer_action.move_to_location(int(x), int(y)).pointer_down().pause(0.1).pointer_up()
            a.perform()

        # The password was just typed, so the KEYBOARD is up — and on the iPhone it covers the
        # bottom ~340pt, which is exactly where the T&C row sits (y~560 of an 852pt screen).
        # Every tap then lands on a key instead. hide_keyboard() is unreliable here (and the
        # caller swallows its failure), so confirm it is really gone and force it if not.
        for attempt in range(3):
            try:
                if not r.d.is_keyboard_shown():
                    break
            except Exception:
                break
            try:
                r.d.hide_keyboard()
            except Exception:
                # No dismiss affordance — tap a neutral spot well above the form.
                try:
                    size = r.d.get_window_size()
                    tap(size["width"] // 2, int(size["height"] * 0.18))
                except Exception:
                    pass
            time.sleep(0.8)
        else:
            _note("    · [WARN] keyboard still up — it covers the T&C row and will eat the tap")

        try:
            cbs = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "clickCheckBox")
        except Exception:
            cbs = []
        if not cbs:
            _note("    · no T&C checkbox on screen (already agreed?)")
            return False
        rect = cbs[0].rect            # re-read AFTER the keyboard closed — the layout shifts
        y = rect["y"] + rect["height"] // 2

        # FIND the square instead of guessing an offset. The row's inner padding differs per
        # device — the square is ~10pt in on the iPhone but ~32pt in on the iPad, whose label
        # is a different string ("I have read and accept..." vs "By Signing in I agree...").
        # Any hardcoded offset works on one device and silently misses on the other, which is
        # exactly how this failed: the tap landed on blank padding and the run then reported
        # "login rejected / wrong creds".
        dx0 = self._find_checkbox_dx(r, rect, win)
        order = ([dx0] if dx0 is not None else []) + [10, 32, 6, 14, 22, 3]
        for dx in order:
            before = box_sig({**rect, "x": rect["x"] + dx - rect["height"] // 2})
            tap(rect["x"] + dx, y)
            time.sleep(0.9)
            after = box_sig({**rect, "x": rect["x"] + dx - rect["height"] // 2})
            # Require a REAL colour shift: empty box is near-white, ticked is filled purple.
            if before and after and sum(abs(a - b) for a, b in zip(after, before)) > 30:
                _note(f"    · ticked the Terms & Conditions checkbox "
                      f"(tap at x+{dx}, verified {before}->{after})")
                return True
        _note("    · [WARN] could not tick the Terms & Conditions checkbox — Sign In stays "
              "disabled, so the login below will fail for that reason (not bad credentials)")
        return False

    @staticmethod
    def _find_checkbox_dx(r, rect, win) -> Optional[int]:
        """Offset (in points, from the row's left edge) of the checkbox square.

        The square is the LEFTMOST non-white thing on the T&C row, so find it by pixels rather
        than assuming a per-device constant. Returns None if the screenshot can't be read."""
        try:
            import io as _io
            from PIL import Image
            im = Image.open(_io.BytesIO(r.d.get_screenshot_as_png())).convert("RGB")
            if not win:
                return None
            sx, sy = im.width / win["width"], im.height / win["height"]
            y0 = int((rect["y"] + rect["height"] * 0.5) * sy)
            x0 = int(rect["x"] * sx)
            span = int(min(rect["width"], 80) * sx)            # only scan the left of the row
            for col in range(0, span):
                px = im.getpixel((min(x0 + col, im.width - 1), min(y0, im.height - 1)))
                if sum(px) < 720:                              # anything not near-white
                    return max(2, int(col / sx) + int(rect["height"] * 0.35))
        except Exception:
            pass
        return None

    def _ensure_business_account(self, r, account: str, notes: List[str]) -> bool:
        if self._biz_account == account:
            return True
        # Detect who is ACTUALLY signed in right now. The app persists login across runs
        # (noReset), and waiter/kitchen share one app — so check the SCREEN, don't assume.
        # WAIT for a definite answer before deciding. A single read right after the app is
        # activated usually returns 'unknown' (still rendering) — and on 'unknown' the
        # switch below is skipped, so a persisted waiter session survives into a kitchen
        # segment and login then refuses. Poll first; decide on fact, not on a race.
        role = self._biz_role_state()
        for _ in range(8):                          # ~16s
            if role in ("login", "waiter", "kitchen"):
                break
            time.sleep(2)
            role = self._biz_role_state()
        if role == "unknown":
            # Parked on a screen with no role marker (e.g. the Menu page, which only has
            # logOutBtn/accountBtn). Navigate Home to reach a detectable screen.
            try:
                hb = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "homeBtn")
                if hb:
                    hb[0].click(); time.sleep(2)
            except Exception:
                pass
            role = self._biz_role_state()
        if role == account:
            self._biz_account = account
            notes.append(f"[ok] already logged in as {account} (detected {role} screen)")
            return True
        # Wrong role signed in (or a stale known account) — log out so we can switch accounts.
        if role in ("waiter", "kitchen") or self._biz_account is not None:
            who = role if role in ("waiter", "kitchen") else self._biz_account
            notes.append(f"    · logged in as {who}, but need {account} — logging out to switch")
            self._logout_business(r)
        if self._login_business(r, account, notes):
            self._biz_account = account
            notes.append(f"[ok] logged in as {account}")
            return True
        notes.append(f"[FAIL] could not log in as {account} — still on the sign-in screen "
                     f"(check creds / T&C checkbox / staging backend)")
        return False

    # -- @token handlers -----------------------------------------------------
    def _find_add_to_cart(self, r: ScenarioRunner):
        """The product-sheet 'Add to cart' button (real id: `confirmProduct`), or
        None if no product detail sheet is open."""
        try:
            for e in r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "confirmProduct"):
                if e.is_displayed():
                    return e
        except Exception:
            pass
        return None

    def _select_first_option(self, r: ScenarioRunner) -> bool:
        """Tap the first selectable option in a product sheet (a required variant),
        so Add-to-cart (`confirmProduct`) becomes enabled. Options are ids ending in
        'Product' (e.g. cashewProduct); skip the sheet's own controls. Never types."""
        skip = {"confirmProduct", "productInfo", "productClose"}
        try:
            opts = r.d.find_elements(AppiumBy.IOS_PREDICATE, 'name ENDSWITH "Product"')
        except Exception:
            opts = []
        for e in opts:
            try:
                nm = (e.get_attribute("name") or "").strip()
                if nm in skip or not e.is_displayed():
                    continue
                e.click(); time.sleep(0.5)
                return True
            except Exception:
                continue
        return False

    def _dismiss_product_modal(self, r: ScenarioRunner) -> str:
        """Add the open product sheet to cart. If Add-to-cart is enabled, just tap
        it (no modifiers). If it's disabled because a variant is required, pick the
        first option to enable it, then add. Closes the sheet if it can't. Never types.

        Returns 'added' | 'closed' | 'none'.
        """
        add = self._find_add_to_cart(r)
        if add is None:                        # a sheet may still be animating in
            time.sleep(0.7)
            add = self._find_add_to_cart(r)
        if add is None:
            return "none"                      # no product sheet — Inc just incremented
        try:
            r.d.hide_keyboard()                # a focused field may have popped it
        except Exception:
            pass

        # 1) Try to add directly — works when the button is enabled (optional add-ons).
        try:
            add.click(); time.sleep(0.9)
        except Exception:
            pass
        if self._find_add_to_cart(r) is None:  # sheet closed → added
            return "added"

        # 2) Still open → a required variant must be chosen. Pick the first option.
        if self._select_first_option(r):
            add = self._find_add_to_cart(r)
            if add is not None:
                try:
                    add.click(); time.sleep(0.9)
                except Exception:
                    pass
            if self._find_add_to_cart(r) is None:
                return "added"

        # 3) Give up gracefully → close the sheet so the run isn't stuck.
        try:
            b = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "productClose")
            if b:
                b[0].click(); time.sleep(0.5)
        except Exception:
            pass
        return "closed"

    def _idb_els(self, udid: str = "") -> List[dict]:
        """Fast full-screen snapshot via idb (~1-2s) as a flat list of element dicts —
        replaces the expensive Appium IOS_PREDICATE traversals on this app's huge tree.
        'map the screen once, resolve everything locally' instead of one traversal per
        element. Each dict: id, label, type, x/y/w/h and centre cx/cy (points)."""
        import json as _json
        import subprocess as _sp
        udid = udid or getattr(self, "_cur_udid", "") or self.devices.get("consumer") or DEFAULT_CONSUMER_UDID
        try:
            raw = _sp.run([_IDB, "ui", "describe-all", "--udid", udid],
                          capture_output=True, text=True, timeout=15).stdout
            arr = _json.loads(raw) if raw.strip().startswith("[") else []
        except Exception:
            return []
        out = []
        for e in arr:
            f = e.get("frame") or {}
            x, y, w, h = f.get("x", 0), f.get("y", 0), f.get("width", 0), f.get("height", 0)
            out.append({"id": e.get("AXUniqueId") or "", "label": (e.get("AXLabel") or "").strip(),
                        "type": e.get("type") or "", "x": x, "y": y, "w": w, "h": h,
                        "cx": int(x + w / 2), "cy": int(y + h / 2)})
        return out

    # Card labels differ BY DEVICE, and both forms are live in this fleet:
    #   iPad  : '<Name>card<Status>'   e.g. RoopaDcardReserved
    #   iPhone: '<Name><Status>Card'   e.g. RoopaDReservedCard
    # Matching only the iPad form made every phone card parse as status '' — so it
    # failed the assignable check and @open_reservation reported "card not found"
    # while RoopaDReservedCard sat in its own screen dump.
    _CARD_RE = re.compile(r"^(?P<name>.+?)card(?P<status>[A-Za-z]*)$")
    # Longest first so 'ConfirmationPending' wins over 'Pending' and
    # 'InProgress' over 'Progress'. A vocabulary, not a greedy regex: '(.+?)([A-Z]\w*)Card'
    # happily splits RoopaDReservedCard into name='Roopa', status='DReserved'.
    _CARD_STATUSES = ("confirmationpending", "inprogress", "cancelled", "completed",
                      "reserved", "expired", "payment", "serve")

    @classmethod
    def _split_card(cls, label: str):
        """(name, status_lowercase) for either device's card-label convention."""
        lbl = (label or "").strip()
        m = cls._CARD_RE.match(lbl)
        if m:
            return m.group("name"), m.group("status").lower()
        low = lbl.lower()
        if low.endswith("card"):
            stem = lbl[:-4]                      # drop the trailing 'Card'
            stem_low = stem.lower()
            for st in cls._CARD_STATUSES:
                if stem_low.endswith(st):
                    return stem[:len(stem) - len(st)], st
            return stem, ""
        return lbl, ""

    @classmethod
    def _choose_card(cls, els: List[dict], diner_key: str, statuses: tuple,
                     hour_y: Optional[float], diner_name: str = "",
                     hour_lbl: str = "") -> tuple:
        """Pick the diner's reservation card. Returns (exact_label, diagnostic).

        Two things make this hard, and both are real on this board:

        • The STATUS SUFFIX MOVES mid-flow — Reserved -> InProgress -> Completed —
          so a locator pinned to one status stops resolving while the booking is
          still perfectly real.
        • The same diner accumulates cards across runs. SnehaGunaga alone had
          Expired / InProgress / Completed / Reserved on the board at once, so the
          name on its own is ambiguous.

        The rule: only ever return a card belonging to *this* diner, and when
        several are equally valid, return nothing plus a diagnostic rather than
        guessing — opening the wrong reservation is silent and unrecoverable.
        """
        diner, others, diner_all = [], [], []
        for e in els:
            lbl = (e.get("label") or "").strip() or (e.get("id") or "").strip()
            if "card" not in lbl.lower() or (e.get("w") or 0) <= 60:
                continue
            who, status = cls._split_card(lbl)
            is_diner = diner_key in who.lower()
            if is_diner:
                diner_all.append((e, lbl, status))
            if status not in statuses:
                continue                            # expired/completed/cancelled/…
            (diner if is_diner else others).append((e, lbl, status))

        if not diner:
            if diner_all:
                # The diner IS on the board; the status just moved on. Say so —
                # never substitute a stranger's card.
                return None, (f"'{diner_name}' has {len(diner_all)} card(s) but none in "
                              f"{'/'.join(statuses)}: "
                              + ", ".join(f"{l} (status={s or '?'})" for _, l, s in diner_all))
            if others:
                return None, (f"no card for '{diner_name}'; {len(others)} assignable card(s) "
                              f"belong to other diners: "
                              + ", ".join(l for _, l, _ in others)
                              + " — refusing to open someone else's reservation")
            return None, ""

        if hour_y is not None:
            diner.sort(key=lambda t: abs(t[0]["cy"] - hour_y))   # nearest the booked hour

        if len(diner) > 1:
            if hour_y is None:
                labels_only = {l for _, l, _ in diner}
                if len(labels_only) > 1:
                    return None, (f"multiple cards for '{diner_name}' and no booked hour to "
                                  f"disambiguate: " + ", ".join(sorted(labels_only)))
                # Same label, same status, nothing to tell them apart — the board
                # lays several bookings side by side in a row. Interchangeable for
                # "open this diner's assignable booking", so take the first and say
                # so, exactly as the tied-on-the-hour case does.
                return diner[0][1], (f"note: {len(diner)} identical '{diner[0][1]}' cards and no "
                                     f"booked hour — opened the first")
            best = abs(diner[0][0]["cy"] - hour_y)
            tied = [l for e2, l, _ in diner if abs(e2["cy"] - hour_y) == best]
            if len(set(tied)) > 1:
                # DIFFERENT cards tied on the hour — genuinely ambiguous, and picking
                # one risks opening the wrong booking. Refuse.
                return None, (f"multiple cards for '{diner_name}' equally near "
                              f"{hour_lbl or 'the booked hour'}: " + ", ".join(sorted(set(tied))))
            if len(tied) > 1:
                # IDENTICAL label, same status, same hour row — the board lays several
                # bookings side by side in one row (measured on the phone: two
                # RoopaDReservedCard at y=1550, x=80 and x=361, with the 12:00 row at
                # cy=1556). They are indistinguishable by anything observable, so they
                # are interchangeable for "open the diner's reserved booking at this
                # hour". Take the first deterministically and SAY there were several,
                # rather than dead-ending a flow on a distinction that does not exist.
                return diner[0][1], (f"note: {len(tied)} identical '{tied[0]}' cards in the "
                                     f"{hour_lbl or 'booked'} row — opened the first")
        return diner[0][1], ""

    def _click_sidebar_row(self, slot: str, statuses: tuple, notes: List[str]) -> bool:
        """Open the booking for *slot* from the hour's events sidebar.

        This is the whole point of using the sidebar. On the board, every booking in
        an hour renders the same label -- nine 'RoopaDcardReserved' in the 13:00 row,
        measured -- so "which one is the 13:45 booking" is unanswerable there and the
        runner opened the leftmost one, or nothing at all.

        Each sidebar row states its own booking window, so the right one can be named
        rather than guessed:
            '4657 <icon> RESERVED 13:45 - 14:45 <icon> 13:38'
        Match on the START time, and on status so a cancelled or expired row with the
        same time is not opened by mistake.
        """
        want = re.match(r"^(\d{1,2}):(\d{2})", slot or "")
        if not want:
            return False
        want_hhmm = f"{int(want.group(1)):02d}:{want.group(2)}"

        rows = []
        for e in self._idb_els():
            lbl = (e.get("label") or "").strip()
            m = _SIDEBAR_ROW_RE.search(lbl)
            if not m:
                continue
            start = m.group(0).split("-")[0].strip()
            if f"{int(start.split(':')[0]):02d}:{start.split(':')[1]}" != want_hhmm:
                continue
            if statuses and not any(st in _norm(lbl) for st in statuses):
                continue
            rows.append((e, lbl))

        if not rows:
            return False
        if len(rows) > 1:
            notes.append(f"    · {len(rows)} sidebar rows start at {want_hhmm}; "
                         f"opening the first")
        e, lbl = rows[0]
        notes.append(f"    · opening the {want_hhmm} booking from the events list: "
                     f"{lbl[:60]!r}")
        return self._idb_tap(e["cx"], e["cy"])

    def _table_sheet_open(self) -> bool:
        """Is the table-select sheet up, EVEN BEFORE its chips have loaded?

        EventTableSelect gates its chips on tablesLoading, so for the first beat of
        an open sheet there are no chips at all -- and "no chips" is exactly what a
        sheet that was never opened looks like. Telling those apart matters because
        the sheet is a Modal with onBackdropPress={handleTableClose}: tapping
        'modifyTable' again to "open" an already-open sheet hits the backdrop
        covering it and closes the sheet instead.

        Its heading is the signal that survives the loading state --
        'Select a table' on a first assignment, 'Modify Table' when re-assigning
        (Screens/Event/index.js:2368).
        """
        els = self._idb_els()
        labels = {_norm(e.get("label")) for e in els} | {_norm(e.get("id")) for e in els}
        if "selectatable" in labels:
            return True
        # 'Modify Table' is ALSO the id of the button that opens the sheet, so on its
        # own it proves nothing. As the sheet's HEADING it appears alongside the
        # sheet's own commit control, which the reservation screen does not have.
        return "modifytable" in labels and bool(
            labels & {"assigntablebtn", "applytablebtn"})

    def _swipe_calendar(self, r, direction: str) -> bool:
        """Scroll the bookings calendar vertically. True if it actually moved.

        'mobile: swipe' WITHOUT an element is a measured no-op on this board: ten
        consecutive elementless swipes left the 13:00 events badge at y=1730 every
        time. The gesture needs an anchor element that is genuinely inside the
        viewport -- the same rule _pick_swipe_surface enforces for the horizontal
        rows. An hour label currently on screen is the natural anchor, since the
        calendar is exactly the thing we want to move.

        Movement is verified rather than assumed, so a wedged list reports "did not
        scroll" instead of looping until the step times out.
        """
        def hour_positions():
            return {e["label"].strip(): e["cy"] for e in self._idb_els()
                    if re.match(r"^\d{1,2}:00$", e["label"].strip())}

        before = hour_positions()
        if not before:
            return False
        # Prefer a DIRECT DRAG to a stepwise swipe. 'mobile: swipe' costs a measured
        # 9.3s per call here (13.7s including the scans around it) and moves a fixed
        # ~590pt, so walking from 00:00 to 15:00 is six swipes and ~82s -- a third of
        # the step's whole 240s budget spent scrolling. The rows are 100pt apart and
        # idb reports live positions, so the exact distance is known: one drag covers
        # it. Falls through to the swipe loop if the drag does not move the list.
        try:
            h0 = float(r.d.get_window_size()["height"])
        except Exception:
            h0 = 834.0
        want_dy = 0.0
        target = getattr(self, "_scroll_target_hour", "")
        if target:
            y_now = before.get(target)
            if y_now is not None:
                want_dy = y_now - 0.4 * h0
        if abs(want_dy) > 60:
            x = 600
            y_from = min(max(0.55 * h0, 120), h0 - 80)
            y_to = min(max(y_from - want_dy, 90), h0 - 60)
            try:
                r.d.execute_script("mobile: dragFromToForDuration", {
                    "fromX": x, "fromY": y_from, "toX": x, "toY": y_to,
                    "duration": 0.6})
                time.sleep(1.0)
                after0 = hour_positions()
                if any(after0.get(k) != v for k, v in before.items() if k in after0):
                    return True
            except Exception:
                pass
        try:
            h = float(r.d.get_window_size()["height"])
        except Exception:
            h = 0.0
        on_screen = [lbl for lbl, y in before.items()
                     if not h or 0.1 * h <= y <= 0.9 * h]
        if not on_screen:
            return False
        for lbl in on_screen[:3]:
            try:
                els = r.d.find_elements(AppiumBy.IOS_PREDICATE, f'label == "{lbl}"')
                if not els:
                    continue
                # An hour label resolves to TWO elements (the text and its row
                # container), and the first is not necessarily the one on screen.
                # Swiping an off-screen anchor is a silent no-op -- the same trap
                # _pick_swipe_surface documents for the horizontal rows -- so choose
                # the match whose own rect is really inside the viewport.
                anchor = None
                for e in els:
                    try:
                        rect = e.rect or {}
                    except Exception:
                        continue
                    ey = (rect.get("y") or 0) + (rect.get("height") or 0) / 2
                    if not h or 0.1 * h <= ey <= 0.9 * h:
                        anchor = e
                        break
                if anchor is None:
                    continue
                r.d.execute_script("mobile: swipe",
                                   {"direction": direction, "element": anchor.id})
            except Exception:
                continue
            time.sleep(0.8)
            after = hour_positions()
            if any(after.get(k) != v for k, v in before.items() if k in after):
                return True
        return False

    @staticmethod
    def _events_sidebar_up(els: List[dict]) -> bool:
        """Is the hour's events list (AddCountModal) open?

        Tapping an hour's 'Events N' badge opens a SIDEBAR listing that hour's
        bookings VERTICALLY -- which is the whole point: on the board an hour's
        bookings are laid out sideways, so the 4th or 5th can only be reached by
        swiping the row. In the sidebar every one of them is reachable by a
        vertical scroll.

        The badge is a toggle, so "did the tap open it or close it" has to be
        answerable. Two observable signals, both measured live on the iPad:
          • 'closeEventModal' -- the sidebar's own close button (AddCountModal
            index.js:291); present only while it is up.
          • 'Orders Not Found !!' -- its empty state, for an hour whose bookings
            are all filtered out. The sidebar IS up; it just has nothing to show.
        """
        for e in els:
            lbl = (e.get("label") or "").strip()
            ident = (e.get("id") or "").strip()
            if ident == "closeEventModal" or lbl == "closeEventModal":
                return True
            if "orders not found" in lbl.lower():
                return True
            # The POPULATED list is the common case and exposes neither of the above.
            # Its rows are OrderCard, which carries no accessibilityLabel, so idb
            # reports the concatenated text of its children -- measured on the iPad
            # at x=799 (the board's own cards end at ~700):
            #     '4657 <icon> RESERVED 13:45 - 14:45 <icon> 13:38'
            # a ticket number, a status, and the booking's own 'HH:MM - HH:MM'
            # window. That window is exactly what the board's cards never show, and
            # it is what makes one of nine identical 'RoopaDcardReserved' cards
            # identifiable.
            if _SIDEBAR_ROW_RE.search(lbl):
                return True
        return False

    @staticmethod
    def _logbox_strip(els: List[dict]) -> Optional[dict]:
        """The COLLAPSED LogBox toast, found by GEOMETRY rather than by message text.

        Measured on both devices in this fleet:
            iPhone  (10, 801.7, 382, 48)   screen 402 wide
            iPad    (10, 708.0, 1190, 48)  screen 1210 wide
        — always a full-width 48pt strip. Matching on the message instead is what
        let it through before: the iPad toast reads "6 Each child in a list should
        have a unique key prop", which contains none of the words the old keyword
        gate looked for, so the gate returned early and the toast stayed up.
        """
        screen_w = max((e.get("w") or 0 for e in els), default=0)
        if not screen_w:
            return None
        for e in els:
            if (e.get("type") == "GenericElement"
                    and 40 <= (e.get("h") or 0) <= 60
                    and (e.get("w") or 0) >= 0.9 * screen_w):
                return e
        return None

    @staticmethod
    def _occluding(target: dict, els: List[dict]) -> Optional[dict]:
        """An element drawn OVER *target*'s centre, if any.

        A LogBox toast is checked FIRST and irrespective of list position. Measured
        on the iPad: the toast at (10,708,1190,48) is listed BEFORE addNewEvent at
        (700,698,50,50), yet it is what actually receives the tap — so idb's
        ordering is not a reliable z-order for it. Assuming "front-most last" here
        made this exact overlap invisible to the check.

        For everything else, later-in-the-list is treated as drawn on top; that is
        the only ordering signal available, and it catches the booking-card overlap
        (a card at (700,716,492,138) sits across the same FAB).
        """
        cx, cy = target.get("cx"), target.get("cy")
        toast = FlowRunner._logbox_strip(els)
        if toast is not None and toast is not target:
            if toast["x"] <= cx <= toast["x"] + toast["w"] \
                    and toast["y"] <= cy <= toast["y"] + toast["h"]:
                return toast
        try:
            start = els.index(target) + 1
        except ValueError:
            return None
        for e in els[start:]:
            if e is target or (e.get("w") or 0) <= 0 or (e.get("h") or 0) <= 0:
                continue
            # A full-screen container is an ancestor, not an overlay.
            if (e.get("w") or 0) * (e.get("h") or 0) >= 0.95 * (
                    max((x.get("w") or 0) for x in els) * max((x.get("h") or 0) for x in els)):
                continue
            if e["x"] <= cx <= e["x"] + e["w"] and e["y"] <= cy <= e["y"] + e["h"]:
                return e
        return None

    def _clear_logbox(self, udid: str = "", limit: int = 8) -> int:
        """Dismiss stacked React-Native LogBox overlays, via idb.

        ScenarioRunner.dismiss_logbox() looks for XCUIElementTypeButton named
        "Dismiss" — but on this app they are GenericElements, which Appium's
        collapsed snapshot never reports, so it silently finds nothing. idb does
        see them. This matters because a debug build stacks 8-9 warnings and the
        toast sits along the BOTTOM edge, exactly over BOOK NOW: the tap is eaten,
        the booking is never created, and every later segment then fails hunting
        for a reservation that does not exist.

        Returns how many overlays were dismissed.
        """
        cleared = 0
        for _ in range(limit):
            els = self._idb_els(udid)
            # A COLLAPSED toast comes FIRST, because it has neither the keywords
            # the gate below looks for nor a "Dismiss" control — it is one
            # GenericElement holding the warning text. Measured on this build:
            # frame (10, 801.7, 382, 48), sitting directly over BOOK NOW at
            # (201, 804). So both checks below missed it entirely, the tap landed
            # on the toast, LogBox expanded, and @book_appointment "needed a
            # retry" — attempt 2 only worked because the EXPANDED LogBox does have
            # a Dismiss. Its ✕ is at the right end of its own frame; tapping
            # anywhere else on it expands LogBox instead of closing it.
            toast = next((e for e in els
                          if e["type"] == "GenericElement"
                          and ScenarioRunner._LOGBOX.search(e["label"])), None)
            if toast:
                self._idb_tap(toast["x"] + toast["w"] - 20, toast["cy"], udid)
                cleared += 1
                time.sleep(1.0)
                continue
            # Only act when a LogBox is genuinely up. Its Dismiss control sits at
            # roughly the same coordinates as the bottom tab bar (Dismiss ~x=100,
            # y=850; the Home tab is x=0-134, y=780-840), so tapping on a stale or
            # coincidental match navigates the app to another tab instead — which
            # then fails several steps later as "element not found" on a screen
            # nobody meant to be on.
            if not any(k in (e.get("label") or "")
                       for e in els
                       for k in ("Console Error", "Console Warning", "LogBox", "addLog")):
                break
            btn = next((e for e in els
                        if (e.get("label") or "").strip() == "Dismiss"), None)
            if not btn:
                break
            self._idb_tap(btn["cx"], btn["cy"], udid)
            cleared += 1
            time.sleep(1.0)
        return cleared

    def _idb_tap(self, x, y, udid: str = "") -> bool:
        import subprocess as _sp
        udid = udid or getattr(self, "_cur_udid", "") or self.devices.get("consumer") or DEFAULT_CONSUMER_UDID
        x, y = self._rotate_for_device(x, y, udid)
        try:
            _sp.run([_IDB, "ui", "tap", "--udid", udid, str(int(x)), str(int(y))], timeout=10)
            return True
        except Exception:
            return False

    def _rotate_for_device(self, x, y, udid: str):
        """Map an app-space point to the DEVICE space `idb ui tap` expects.

        On the landscape iPad these are not the same space. Measured:
            app frame     1210 x 834   (landscape, what describe-all reports)
            screenshot     834 x 1210  (portrait, the physical device)
            'addNewEvent' app (725, 723)  ->  really at device (723, 485)

        So idb REPORTS rotated coordinates but TAPS in device coordinates, and every
        coordinate tap on this iPad landed ~240pt away from its target. That is why
        'addNewEvent' opened nothing, and why @save_appointment kept "tapping Save"
        while the form sat there — the tap was never on the button. Portrait devices
        (the phones) have both spaces identical, so this is a no-op there.

            x_dev = y_app        y_dev = app_height_in_device_space - x_app
        """
        try:
            import subprocess as _sp, json as _json
            raw = _sp.run([_IDB, "ui", "describe-all", "--udid", udid],
                          capture_output=True, text=True, timeout=15).stdout
            app = next((e for e in _json.loads(raw or "[]")
                        if (e.get("type") or "") == "Application"), None)
            f = (app or {}).get("frame") or {}
            w, h = f.get("width", 0), f.get("height", 0)
            if w > h:                      # landscape app -> portrait device
                return y, w - x
        except Exception:
            pass
        return x, y

    def _steppers(self, r: ScenarioRunner):
        """Product quantity steppers on the menu — via idb (fast), not an Appium
        ENDSWITH predicate that traverses the whole tree. Each product is ONE element
        whose id ends 'Inc'; keep the compact stepper (no price '€', short label) so
        its frame is just the '- 0 +' control. Returns [(idb_dict, name), …]."""
        out = []
        for e in self._idb_els():
            nm = e["id"] or e["label"]
            if nm.endswith("Inc") and "€" not in nm and len(nm) <= 70:
                out.append((e, nm))
        return out

    def _add_all_products(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Add a couple of products. The '+' has no id, so tap it by COORDINATE at
        the right edge of each product's stepper element. Waits for the menu to
        render first; verifies items landed via the CHECKOUT bar count."""
        # 1) Wait for the menu to render (products appear after preOrderBooking).
        #    If a cart already has items, preOrderBooking may open the CART directly —
        #    detect that and proceed (nothing to add).
        found = []
        for _ in range(8):
            found = self._steppers(r)
            if found:
                break
            try:
                if r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "cartCheckout"):
                    notes.append("[ok] @add_all_products — cart already has item(s), skipping add")
                    return True
            except Exception:
                pass
            time.sleep(1.5)
        if not found:
            notes.append("[FAIL] @add_all_products — no menu products and no cart (unexpected screen)")
            return False

        # 2) Tap the '+' (right edge) of the first couple of products, re-finding
        #    each time (adding re-renders the list → stale rects otherwise).
        added, seen = 0, set()
        for _ in range(6):
            found = self._steppers(r)
            target = None
            for e, nm in found:
                key = re.sub(r'\s+\d+\s+', ' ', nm)   # ignore the qty digit in the label
                if key not in seen:
                    target, tnm, tkey = e, nm, key
                    break
            if target is None:
                break
            seen.add(tkey)
            label = tnm.replace("favProduct", "").split("Dec")[0].strip()
            try:
                # Tap the card's image (upper-centre) to open the product detail
                # sheet, then Add-to-Cart from there (the tiny '+' box has no id and
                # is too small to hit reliably; the detail sheet's button is solid).
                # target is an idb dict (frame in points) — tap by coordinate via idb.
                tx = int(target["x"] + target["w"] / 2)
                ty = int(target["y"] + target["h"] * 0.32)
                self._idb_tap(tx, ty); time.sleep(1.3)
                outcome = self._dismiss_product_modal(r)
                if outcome == "added":              # only a real Add-to-cart counts
                    added += 1
                notes.append(f"    · {label} → {outcome}")
            except Exception as ex:
                notes.append(f"    · {label} tap failed: {str(ex).splitlines()[0][:80]}")
            if added >= 2:
                break

        # 3) Verify the cart has items (the CHECKOUT bar shows a count) — via idb.
        in_cart = False
        try:
            for e in self._idb_els():
                blob = (e["label"] + " " + e["id"])
                if "checkout" in blob.lower() and re.search(r'\(\d+\)|•|\d', blob):
                    in_cart = True
                    break
        except Exception:
            pass
        ok = added > 0
        notes.append(f"[{'ok' if ok else 'FAIL'}] @add_all_products — added {added} "
                     f"item(s){' (checkout bar present)' if in_cart else ''}")
        return ok

    def _pay(self, r: ScenarioRunner, method: str, notes: List[str]) -> bool:
        """Open the payment section, pick the method, pay MORE than the bill, and
        verify the change. Emits a 'Bill check' reason so the rich report shows it."""
        bill = self._read_bill_total(r)
        # Business payment section methods (from the live app): E-Payment / Cash /
        # Food Voucher — each has an amount field. Consumer card pay is just Pay.
        method_labels = {
            "epay": ["E-Payment", "ePaymentBtn", "ePayment", "EPayment"],
            "cash": ["Cash", "cashPaymentBtn", "cashPayment"],
            "voucher": ["Food Voucher", "foodVoucherBtn", "FoodVoucher"],
            "card": ["cardPaymentBtn", "cardPayment", "Card"],
        }.get(method, [f"{method}PaymentBtn", method])
        picked = False
        for lbl in method_labels:
            if r._resolve([lbl]):
                r.run_one(f"click {lbl}", 0)
                picked = True
                break
        if not picked:
            notes.append(f"[FAIL] @pay:{method} — payment method not found (TUNE)")
            return False

        paid = None
        # E-Payment and Cash both take an amount → tender MORE than the bill so we
        # can verify the change calculation. Card just confirms (no amount).
        if method in ("epay", "cash") and bill is not None:
            paid = round(bill + PAY_OVER_BY, 2)
            self._type_amount(r, paid)

        ok_confirm = False
        for c in ("Confirm Payment", "confirmPaymentBtn", "paymentConfirmBtn",
                  "Proceed with the payment", "payNow", "Pay", "Confirm"):
            if r._resolve([c]):
                ok_confirm = r.run_one(f"click {c}", 0).ok
                break
        time.sleep(2)

        # Calculation check → a "Bill check" reason the report renders.
        if bill is not None and paid is not None:
            change = round(paid - bill, 2)
            disp_change = self._read_bill_total(r)  # best-effort: what the screen now shows
            calc = f"Bill check: total €{bill:.2f}, tendered €{paid:.2f}, change €{change:.2f}"
            if disp_change is not None and abs(disp_change - change) > 0.01 and disp_change < bill:
                calc += (f"; Bill total mismatch: displayed=€{disp_change:.2f}, "
                         f"calculated=€{change:.2f}, diff=€{abs(disp_change-change):.2f}")
            notes.append(("[ok] " if ok_confirm else "[FAIL] ") + calc)
        elif bill is not None:
            notes.append(("[ok] " if ok_confirm else "[FAIL] ") +
                          f"Bill check: total €{bill:.2f} (method {method}, no amount entry)")
        else:
            notes.append(("[ok] " if ok_confirm else "[FAIL] ") +
                          f"@pay:{method} — could not read the bill total (TUNE)")
        return ok_confirm

    def _assign_table(self, r: ScenarioRunner, notes: List[str],
                      table: str = "") -> bool:
        """Assign a table on the opened reservation, by accessibility id.

        The sheet's controls are now individually addressable (Business app,
        App/Components/Modal/index.js): each chip carries `tableChip<Name>` —
        tableChipI1, tableChipO4 … — and the commit button carries
        `applyTableBtn`. Both also expose accessibilityState.selected, which is
        what makes "did the tap select anything" answerable instead of assumed.

        Element clicks only. The chips live in a horizontal ScrollView, so a
        coordinate tap cannot reach one that is scrolled out of view, and the
        sheet sits over the bookings board where a stray tap opens a booking.

        Every exit verifies real app state: a chip is only "selected" when the
        app says selected=true, and the assignment only counts when the sheet
        actually closes.
        """
        import re as _re

        # A chip's name is exactly 'tableChip<Name>'. The BEGINSWITH query also
        # returns the ScrollView ANCESTORS, whose names are every chip's label
        # concatenated ('tableChipI1 tableChipI2 … Vertical scroll bar, 3 pages') —
        # so an unfiltered [0] can be a container, and clicking that scrolls or
        # does nothing while looking like a chip. Require a single token.
        # Two different assign-a-table UIs ship in this app:
        #   iPad  : 'tableChip<Name>'   chips + 'applyTableBtn'
        #   iPhone: 'T<N>AssignAnyBtn'  chips + 'AssignTableBtn'   (measured live)
        # Both expose accessibilityState.selected, so selection stays verifiable.
        # THREE chip spellings ship in this fleet, and the third is the one that
        # actually runs on the iPad today:
        #   'tableChip<Name>'    — an older Modal/index.js build
        #   'T<N>AssignAnyBtn'   — EventTableSelect's accessibilityLabel
        #   '<Name>'             — the chip's own TEXT: I1, I2, O1, O2
        #
        # MEASURED on the live iPad with the sheet open: the first two predicates
        # matched ZERO elements while ACCESSIBILITY_ID 'I1'/'I2'/'O1'/'O2' each
        # matched exactly one. The installed build predates the
        # accessibilityLabel in the checked-out source, so the Pressable exposes
        # its Text child instead. Searching only for the labelled forms found no
        # chips at all -- which is why @assign_table reported nothing to tap on a
        # sheet that plainly showed four tables.
        #
        # A bare table name is a WEAK locator (any 'I1' anywhere would match), so
        # it is used ONLY while the table sheet is up and only for names shaped
        # like a table: one or two letters then digits.
        _CHIP_NAME = _re.compile(r"(?:tableChip\w+|T\d+AssignAnyBtn)\Z")
        _CHIP_TEXT = _re.compile(r"[A-Za-z]{1,2}\d{1,3}\Z")

        def chips():
            """(label, element) for every real table chip on the sheet."""
            out = []
            try:
                for e in r.d.find_elements(
                        AppiumBy.IOS_PREDICATE,
                        'name BEGINSWITH "tableChip" OR '
                        '(name BEGINSWITH "T" AND name ENDSWITH "AssignAnyBtn")'):
                    try:
                        nm = (e.get_attribute("name") or "").strip()
                    except Exception:
                        continue
                    if _CHIP_NAME.fullmatch(nm):
                        out.append((nm, e))
            except Exception:
                pass
            if out:
                return out
            # Fall back to the chips' own text, but only on the sheet itself.
            if not self._table_sheet_open():
                return out
            seen = set()
            for cand in self._idb_els():
                nm = (cand.get("label") or "").strip()
                if not _CHIP_TEXT.fullmatch(nm) or nm in seen:
                    continue
                # The commit button and the heading are not chips.
                if nm in ("AssignTableBtn", "applyTableBtn"):
                    continue
                try:
                    els3 = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, nm)
                except Exception:
                    continue
                if len(els3) == 1:          # ambiguous names are not safe to tap
                    seen.add(nm)
                    out.append((nm, els3[0]))
            return out

        def selected_labels():
            return [lbl for lbl, e in chips()
                    if (e.get_attribute("selected") or "").lower() == "true"]

        def commit_btn():
            """The commit control, whichever build this is."""
            for ident in ("applyTableBtn", "AssignTableBtn"):
                try:
                    els2 = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
                except Exception:
                    els2 = []
                if els2:
                    return els2[-1]
            return None

        def commit_enabled():
            b = commit_btn()
            if b is None:
                return None
            try:
                return bool(b.is_enabled())
            except Exception:
                return None

        def open_table_sheet() -> bool:
            """Open the table-select sheet from the opened reservation.

            Opening a reservation shows the EVENT sheet (closeEventModal, the booking
            card, 'modifyTable'); the table chips live in a SEPARATE sheet that
            App/Screens/Event/index.js renders only once onModifyTableClick has set
            showAssignTableModal. Without this step the chip search finds nothing and
            the run reports "no Apply/Confirm to commit" -- true, but only because the
            sheet holding them was never opened.

            No-op when the chips are already present, so the flow stays correct on a
            build that opens straight onto them.
            """
            if chips():
                return True
            # THE SHEET MAY ALREADY BE OPEN AND STILL FETCHING.
            #
            # EventTableSelect renders its chips only after the table list arrives
            # (tablesLoading gates them), so chips() is empty for the first beat of
            # an OPEN sheet -- indistinguishable, by chips alone, from a sheet that
            # was never opened. Re-tapping 'modifyTable' then made it worse, because
            # the sheet is a Modal with onBackdropPress={handleTableClose}: the tap
            # lands on the backdrop covering that control and DISMISSES the sheet.
            # That is the reported "it clicks the event, then simply comes back" --
            # the sheet was open, with I1/I2/O1/O2 on screen, and we closed it.
            #
            # So: if the sheet's own heading is up, wait for its chips instead.
            if self._table_sheet_open():
                for _ in range(10):               # ~10s for the table fetch
                    time.sleep(1.0)
                    if chips():
                        return True
                    if not self._table_sheet_open():
                        break                     # it closed under us — reopen below
                if chips():
                    return True
            # 'tableBtn' is NOT a sheet control. It is the BOARD's booking-type
            # filter tab (Screens/Home/index.js:1052, beside allBtn/pickupBtn) —
            # measured live at x=598,y=55 on the bookings board. Tapping it
            # navigated AWAY from the opened reservation, so the next pass found no
            # chips, re-opened the sheet, tapped it again ... alternating
            # modifyTable/tableBtn until the 240s step timeout killed the segment.
            # Only controls that actually belong to the reservation sheet go here.
            for ident in ("modifyTable", "assignTableBtn"):
                try:
                    els2 = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
                except Exception:
                    continue
                if not els2:
                    continue
                try:
                    els2[0].click()
                except Exception as e:
                    notes.append(f"    · @assign_table — could not tap {ident!r}: "
                                 f"{type(e).__name__}")
                    continue
                notes.append(f"    · @assign_table — opened the table sheet via {ident!r}")
                for _ in range(8):                # the sheet fetches its table list
                    time.sleep(1.0)
                    if chips():
                        return True
            return bool(chips())

        # ── new path: individually addressable chips ─────────────────────────
        found = []
        for _attempt in range(6):                 # sheet renders a beat late
            found = chips()
            if found:
                break
            if open_table_sheet():
                found = chips()
                break
            # Re-opening only makes sense while we are still ON the reservation.
            # If its own control has gone, a further pass cannot help: it would tap
            # whatever the current screen offers and burn the step's whole budget
            # (six identical "opened the table sheet" notes, then a 240s timeout).
            # Stop and say where we ended up instead.
            try:
                still_on_sheet = bool(r.d.find_elements(
                    AppiumBy.ACCESSIBILITY_ID, "modifyTable"))
            except Exception:
                still_on_sheet = True             # can't tell — keep trying
            if not still_on_sheet:
                # RETURN, do not break. Breaking falls through to the legacy
                # coordinate path below, which scans the CURRENT screen for
                # anything matching [IOio]\d — and the screen we are on once the
                # reservation has closed is the order summary. MEASURED: it
                # matched 'I1' there, tapped it, found no Apply (there is no
                # sheet), and reported "opened the table modal but found no
                # Apply/Confirm" — pointing every past investigation at the
                # table sheet and its testIDs when the sheet was never up.
                notes.append("[FAIL] @assign_table — the reservation closed before the table "
                             "sheet could be read (no 'modifyTable'); not tapping blindly on "
                             "whatever screen replaced it")
                notes.append("    ↳ on screen: "
                             f"{[e['label'] for e in self._idb_els()][:10]}")
                return False
            time.sleep(1.0)

        if found:
            wants = ({f"tableChip{table}", f"{table}AssignAnyBtn", table} if table else set())
            pick = next((t for t in found if t[0] in wants), None) if wants else None
            if wants and pick is None:
                notes.append(f"[FAIL] @assign_table — requested table '{table}' not on the "
                             f"sheet. Available: {sorted(l for l, _ in found)}")
                self._dismiss_table_modal(r, notes)
                return False
            # WHICH TABLES TO TRY. A specific request gets exactly that one; with
            # no request the flow wants "the first FREE table", which is not the
            # same as the first chip. MEASURED: the app validates the choice
            # (validateTableForBooking) and rejects a table whose bookedSlots
            # overlap this appointment — 'This table is already booked at the
            # selected time !!' — then shows a toast and RETURNS WITHOUT POSTING,
            # leaving the sheet up. T0 was already taken at 14:30, so committing
            # it could never work however cleanly it was clicked.
            candidates = [pick] if pick is not None else list(found)
            tried = []

            for label, el in candidates[:6]:      # bounded; a room has ~10 tables
                was_enabled = commit_enabled()
                try:
                    el.click(); time.sleep(1.0)
                except Exception as ex:
                    tried.append(f"{label}: click error {type(ex).__name__}")
                    continue

                # VERIFY the selection registered. A click that lands on nothing is
                # indistinguishable from a successful one without this.
                sel = selected_labels()
                now_enabled = commit_enabled()
                if label in sel:
                    notes.append(f"    · @assign_table — selected '{label}' (verified selected=true)")
                elif was_enabled is False and now_enabled is True:
                    notes.append(f"    · @assign_table — selected '{label}' (verified: commit "
                                 f"button went from disabled to enabled)")
                elif now_enabled is True:
                    notes.append(f"    · @assign_table — selected '{label}' (commit button enabled)")
                else:
                    tried.append(f"{label}: selection did not register")
                    continue

                # Commit. Appium reports two matches for the button (the Pressable
                # and its wrapper, concentric); either resolves to the same control.
                apply_btn = None
                for _ in range(6):
                    apply_btn = commit_btn()
                    if apply_btn is not None:
                        try:
                            if apply_btn.is_enabled():
                                break
                        except Exception:
                            break
                    time.sleep(1.0)
                if apply_btn is None:
                    notes.append("[FAIL] @assign_table — table selected but no commit button on "
                                 "the sheet")
                    self._dismiss_table_modal(r, notes)
                    return False
                try:
                    apply_btn.click()
                except Exception as ex:
                    tried.append(f"{label}: commit click error {type(ex).__name__}")
                    continue

                # VERIFY the assignment. "The sheet's elements are gone" is NOT the
                # same as "the sheet closed": committing pushes a LogBox error whose
                # viewer covers the screen and takes the sheet out of the tree, which
                # this used to read as success while nothing had been assigned.
                # Collapse the viewer first so we judge the real screen.
                # Poll for EITHER outcome, whichever lands first — the app says no
                # via a toast within a second or two, so waiting out the full window
                # on every rejected table is what blew the 150s step ceiling after
                # only three candidates. One idb read per pass, not three.
                closed = False
                refused = ""
                for _ in range(8):                # ~10s worst case
                    time.sleep(1.2)
                    els_now = self._idb_els()
                    msg = next((e["label"] for e in els_now
                                if "already booked" in e["label"].lower()
                                or "not enough" in e["label"].lower()), "")
                    if msg:
                        refused = msg
                        break
                    if not self._table_modal_in(els_now):
                        # Could be genuinely closed, or merely hidden behind the
                        # LogBox viewer — collapse it and look again before believing.
                        if self._dismiss_logbox_viewer():
                            if not self._table_modal_up():
                                closed = True
                            break
                        closed = True
                        break
                if closed:
                    shown = (label[9:] if label.startswith("tableChip")
                             else label[:-len("AssignAnyBtn")]
                             if label.endswith("AssignAnyBtn") else label)
                    notes.append(f"[ok] @assign_table — assigned '{shown}' and the sheet closed")
                    return True

                # Still open -> the app refused this table. Capture WHY if it said so,
                # then untoggle it and try the next one.
                tried.append(f"{label}: refused{' — ' + refused[:60] if refused else ''}")
                try:
                    el.click(); time.sleep(0.8)   # toggleTable() deselects
                except Exception:
                    pass

            notes.append("[FAIL] @assign_table — no table could be assigned. Tried: "
                         + "; ".join(tried[:6]))
            self._dismiss_table_modal(r, notes)
            return False

        # ── no addressable chips: fall through to the legacy detection, which
        #    also tells a genuinely absent sheet apart from an unreadable one ──
        #
        # GATE the legacy scan on the sheet actually being up. '[IOio]\d' is a
        # two-character pattern that matches things on OTHER screens — 'I1' on
        # the order summary, measured — so an ungated scan taps a random element
        # and then blames the missing Apply button. Only the sheet's own screen
        # may be coordinate-tapped.
        if not self._table_modal_up():
            notes.append("[ok] @assign_table — no table sheet on screen "
                         "(already assigned / pickup); continuing")
            return True

        tapped = False
        for _ in range(6):                    # wait for the modal / order summary to render
            els = self._idb_els()
            tables = [e for e in els
                      if _re.fullmatch(r"[IOio]\d{1,2}", e["label"].strip())
                      and e["w"] > 20 and e["h"] > 20 and 0 <= e["cy"] <= 1400]
            if tables:
                t = tables[0]
                self._idb_tap(t["cx"], t["cy"]); time.sleep(0.8)
                notes.append(f"    · @assign_table — tapped table '{t['label'].strip()}'")
                tapped = True
                break
            time.sleep(1.0)
        if not tapped:                        # Appium fallback: a table-name element
            try:
                cand = r.d.find_elements(AppiumBy.IOS_PREDICATE, "label MATCHES '[IOio][0-9]{1,2}'")
                if cand:
                    cand[0].click(); time.sleep(0.8); tapped = True
                    notes.append("    · @assign_table — tapped table (appium)")
            except Exception:
                pass
        if not tapped:
            notes.append("    · @assign_table — no table modal (already assigned / pickup)")
        # Confirm (text-matched — no testID)
        for _ in range(3):
            # The commit button on the "Select A Table" modal is "Apply" (older builds: "Confirm").
            cel = next((e for e in self._idb_els()
                        if e["label"].strip() in ("Apply", "Confirm")), None)
            if cel:
                self._idb_tap(cel["cx"], cel["cy"]); time.sleep(1.3)
                notes.append(f"[ok] @assign_table — table committed ('{cel['label'].strip()}')")
                return True
            try:
                c = r.d.find_elements(AppiumBy.IOS_PREDICATE,
                                      'label == "Apply" OR name == "Apply" OR '
                                      'label == "Confirm" OR name == "Confirm"')
                if c:
                    c[0].click(); time.sleep(1.3)
                    notes.append("[ok] @assign_table — table committed (appium)")
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        # Never report success while the modal is still up. If a table was tapped we opened
        # the modal and failed to commit it — and because the app runs with noReset:True that
        # modal SURVIVES into the next run, where iOS scopes accessibility to the topmost
        # sheet: idb then returns ~4 flattened elements and every later step sees an empty
        # screen ("0 card element(s) seen") on a board that visibly has cards. One stuck modal
        # silently poisons every subsequent run, so close it before giving up.
        if tapped:
            if self._dismiss_table_modal(r, notes):
                notes.append("[FAIL] @assign_table — opened the table modal but found no "
                             "Apply/Confirm to commit it (modal closed so it cannot poison "
                             "the next run)")
            else:
                notes.append("[FAIL] @assign_table — table modal is STUCK OPEN and could not "
                             "be closed; it will hide the bookings board until dismissed")
            return False
        # Nothing was tapped. That means EITHER no modal appeared (fine — already
        # assigned, or a pickup order), OR the modal is up and neither tool can see
        # inside it. Those are opposite outcomes and this used to report both as
        # "[ok] no table modal".
        #
        # MEASURED with the sheet open: the whole modal collapses to ONE
        # GenericElement labelled 'Select A Table I1 I2 O1 O2 …' spanning
        # (0,0,1210,834). idb sees 4 elements total; Appium sees 0 buttons and only
        # the board BEHIND the sheet. The table chips and Apply have no
        # accessibility identity at all, so there is nothing to click.
        #
        # Reporting [ok] here is what poisons the run AFTER this one: noReset keeps
        # the sheet up, iOS scopes accessibility to the topmost sheet, and the next
        # segment reads a 4-element screen and fails with "no reserved card for
        # diner" on a board that visibly has cards.
        if self._table_modal_up():
            closed = self._dismiss_table_modal(r, notes)
            notes.append(
                "[FAIL] @assign_table — the 'Select A Table' sheet is open but exposes no "
                "tappable elements: it flattens to a single accessibility element "
                "('Select A Table I1 I2 O1 …'), so neither idb nor Appium can reach the "
                "table chips or Apply. The table buttons and Apply need testIDs in the "
                "Business app — this cannot be resolved from automation."
                + ("" if closed else " The sheet is also STUCK OPEN and will hide the "
                                     "bookings board until dismissed."))
            return False
        notes.append("[ok] @assign_table — no table modal (already assigned / pickup); continuing")
        return True

    @staticmethod
    def _table_modal_in(els: List[dict]) -> bool:
        """Is the 'Select A Table' sheet present in *els*?

        Checks the sheet's CONTROLS first, then its title. The title alone used to
        be enough only because the sheet collapsed into a single accessibility
        element whose label concatenated everything ('Select A Table I1 I2 …').
        Now that the sheet is properly decomposed the title is its own StaticText
        and idb does not always report it — measured on run 3a040e54 segment 4,
        where the tree held tableChipI1…applyTableBtn and NO title, so this
        returned False, nothing dismissed the sheet, and it hid the bookings board
        until the segment failed with "0 card element(s) seen".
        """
        for e in els:
            lbl = (e.get("label") or "").strip()
            ident = (e.get("id") or "").strip()
            if lbl in ("applyTableBtn", "AssignTableBtn") or ident in ("applyTableBtn", "AssignTableBtn"):
                return True
            if lbl.startswith("tableChip") or ident.startswith("tableChip"):
                return True
            # phone variant: T0AssignAnyBtn … / "Please assign a Table"
            if lbl.endswith("AssignAnyBtn") or ident.endswith("AssignAnyBtn"):
                return True
            if "assign a table" in lbl.lower():
                return True
            if "select a table" in lbl.lower():
                return True
        return False

    def _table_modal_up(self) -> bool:
        """Is the 'Select A Table' sheet on screen?"""
        return self._table_modal_in(self._idb_els())

    def _dismiss_table_modal(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Close a leftover 'Select A Table' sheet. Returns True if the board is visible after.
        The sheet flattens in idb (one element carrying 'Select A Table I1 I2 …'), so detect it
        by that text rather than by a per-button id."""
        modal_up = self._table_modal_up

        if not modal_up():
            return True
        for ident in ("closeBtn", "modalCloseBtn", "cancelBtn", "Close", "Cancel"):
            try:
                els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
                if els:
                    els[0].click(); time.sleep(1.2)
                    if not modal_up():
                        return True
            except Exception:
                pass
        # DO NOT RELAUNCH A REAL RESERVATION.
        #
        # 'AssignTableBtn' means the table sheet is up -- but a reservation that
        # opens straight onto its assign-a-table screen shows exactly the same
        # control, so _table_modal_in cannot tell the two apart. When the caller has
        # just opened a booking, the relaunch below threw that booking away: the app
        # came back on the bookings board, and @assign_table then ran against the
        # board with no reservation open. That is the reported "it goes back and
        # then tries to assign the table".
        #
        # A LEFTOVER sheet sits over the board, so the board's own controls are
        # still in the tree behind it. A reservation replaces the board entirely.
        # Use that to tell them apart, and refuse to relaunch the real thing.
        els_now = self._idb_els()
        seen = {e["id"] for e in els_now} | {e["label"] for e in els_now}
        # 'historyBtn' and 'menuBtn' are the PERSISTENT LEFT NAV RAIL -- present on
        # every screen, including an open reservation. Including them made
        # board_behind always True, so this guard never fired and the relaunch below
        # still threw the booking away: the app reloaded from scratch the moment
        # @assign_table touched the sheet. Only markers unique to the BOOKINGS BOARD
        # belong here.
        board_behind = any(m in seen for m in ("addNewEvent", "qrScaner"))
        on_reservation = any(m in seen for m in
                             ("closeEventModal", "selectAllItemsBtn", "addItemsBtn",
                              "sendToKitchenBtn", "assignToBtn", "ORDER SUMMARY"))
        if on_reservation and not board_behind:
            notes.append("    · table sheet belongs to the OPEN reservation — "
                         "not relaunching")
            return True

        # No close affordance — a relaunch always clears a transient sheet.
        try:
            r.d.terminate_app(self.business_bundle); time.sleep(1.5)
            r.d.activate_app(self.business_bundle); time.sleep(6)
        except Exception:
            pass
        return not modal_up()

    def _ensure_order_items(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """On the opened Order Summary: if the booking was PRE-ORDERED, items are already
        there — do nothing. If it's an order-later booking with an EMPTY order, ADD items
        first so there's something to send to the kitchen. (Per the flow: 'add items if there
        is no pre-order one'.) Real ids: selectAllItemsBtn only renders once the order has
        products; addItemsBtn opens the menu; then assign the added products."""
        # Pre-ordered items present?  selectAllItemsBtn is shown only when the order has items.
        if r._resolve(["selectAllItemsBtn"]):
            notes.append("[ok] @ensure_order_items — pre-ordered items present; no add needed")
            return True
        # Empty order → add items.
        if r._resolve(["addItemsBtn"]):
            r.run_one("click addItemsBtn", 0); time.sleep(1.3)
            self._add_all_products(r, notes)            # add a couple of products (idb)
            for bid in ("assignToBtn", "selectAll", "assignProductsBtn"):   # commit them to the order
                if r._resolve([bid]):
                    r.run_one(f"click {bid}", 0); time.sleep(0.8)
            notes.append("[ok] @ensure_order_items — no pre-order; added items to the order")
            return True
        notes.append("[ok] @ensure_order_items — no addItems button (already has items?); continuing")
        return True

    def _kitchen_ready(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Kitchen KANBAN board (verified on-device): each queued order card carries its own
        `orderReadyBtn` (mark it Prepared), and each prepared/completed order carries an
        `orderCloseBtn` (close the ticket). There is NO single inProgressOrderCard to open and
        NO per-item selection here — you tap Ready on a queued order, then Close on a prepared
        one. These are RN buttons that IGNORE idb coordinate taps, so use Appium element clicks."""
        def _appium_click_first(idv: str) -> bool:
            try:
                els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, idv)
                if els:
                    els[0].click(); time.sleep(1.5)
                    return True
            except Exception:
                pass
            return False

        # Let the board settle (after the account switch / first render).
        for _ in range(6):
            if r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "orderReadyBtn") or \
               r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "orderCloseBtn"):
                break
            time.sleep(2.5)

        # 1) Mark a queued order Ready (Prepared).
        readied = _appium_click_first("orderReadyBtn")
        if readied:
            notes.append("[ok] @kitchen_ready — marked a queued order Ready (Prepared)")
        else:
            notes.append("    · @kitchen_ready — no orderReadyBtn (no queued order to ready)")
        time.sleep(1.0)

        # 2) Close a prepared order ticket.
        closed = _appium_click_first("orderCloseBtn")
        if closed:
            notes.append("[ok] @kitchen_ready — closed a prepared order ticket")

        # Marking Ready is the ASSERTION; closing is cleanup after it. These used to be OR'd,
        # so a run with nothing to ready still went green by closing a leftover prepared
        # ticket — the step passed without once doing the thing it is named after.
        if readied:
            return True
        if closed:
            notes.append("[FAIL] @kitchen_ready — nothing to mark Ready; only closed a "
                         "leftover prepared ticket (no queued order arrived from a waiter)")
            return False
        notes.append("[FAIL] @kitchen_ready — no order to Ready or Close (kitchen queue empty). "
                     "This step needs a waiter to have sent an order first — run a full "
                     "cross-app flow, not the standalone kitchen demo.")
        return False

    def _hide_keyboard(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Dismiss the on-screen keyboard before tapping a control it may be covering.
        On the iPad Business create-appointment form the keyboard opens after typing the
        diner name and overlaps the 'Any' dining button — tapping that button then lands
        on a keyboard key (typing a stray 'k') instead of the button. Blur the field first.
        idb confirms whether the keyboard is really gone; never blocks."""
        udid = getattr(self, "_cur_udid", "") or self.devices.get("consumer") or DEFAULT_CONSUMER_UDID
        # 1) XCUITest native dismissal (Done/return) then Appium's hide_keyboard — neither
        #    moves the view, so they're safe if they work.
        try:
            r.d.execute_script("mobile: hideKeyboard",
                               {"keys": ["Done", "return", "Return", "next", "Next", "go"]})
            time.sleep(0.4)
        except Exception:
            try:
                r.d.hide_keyboard(); time.sleep(0.4)
            except Exception:
                pass
        # 2) If keyboard keys are still in the tree, blur by tapping a non-interactive
        #    label in the form's upper band (below any header/back button, above the
        #    keyboard) — tapping a StaticText dismisses the keyboard without navigating.
        try:
            els = self._idb_els(udid)
            kb_up = any((e.get("type") or "").endswith("Key") for e in els)
            if kb_up:
                labels = [e for e in els
                          if (e.get("type") or "") == "StaticText"
                          and 140 < e["cy"] < 380 and e["w"] > 40]
                if labels:
                    self._idb_tap(labels[0]["cx"], labels[0]["cy"], udid); time.sleep(0.5)
                    notes.append("[ok] @hide_keyboard — blurred field (label tap) to close keyboard")
                else:
                    notes.append("[ok] @hide_keyboard — keyboard persisted; no safe blur target")
            else:
                notes.append("[ok] @hide_keyboard — keyboard down")
        except Exception as e:
            notes.append(f"[ok] @hide_keyboard — {type(e).__name__}; continuing")
        return True

    def _first_time_slot(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Tap the first available time slot — via idb (fast + bounded), NOT an Appium
        `MATCHES` predicate. That regex predicate traverses AND regex-matches this app's
        huge accessibility tree and can HANG ~50s+ (it wedged a live run). idb dumps the
        tree as JSON in ~1-2s; we find a HH:MM label and tap its centre. If no slot is
        present that's a DATA condition, not an automation failure — never block/hang."""
        import json as _json
        import subprocess as _sp
        udid = getattr(self, "_cur_udid", "") or self.devices.get("consumer") or DEFAULT_CONSUMER_UDID

        def _read_slots():
            """idb-read the tree and collect any HH:MM time chips (label, centre)."""
            try:
                raw = _sp.run([_IDB, "ui", "describe-all", "--udid", udid],
                              capture_output=True, text=True, timeout=15).stdout
                els = _json.loads(raw) if raw.strip().startswith("[") else []
            except Exception:
                return []

            # WHERE the bookings board's hour gutter is, if a board is on screen.
            # The board stacks 00:00..23:00 in one narrow left-hand column, so the
            # gutter is the x shared by three or more full-hour labels. The
            # consumer's booking form has no board behind it -- its '18:00' and
            # '21:00' are real, bookable chips spread along a row -- so this stays
            # None there and nothing is excluded.
            _hour_xs = {}
            for e in els:
                lb = (e.get("AXLabel") or "").strip()
                if re.match(r"^\d{1,2}:00$", lb):
                    x = (e.get("frame") or {}).get("x", 0)
                    _hour_xs[x] = _hour_xs.get(x, 0) + 1
            hour_gutter_x = next((x for x, n in _hour_xs.items() if n >= 3), None)

            out = []
            for e in els:
                lbl = (e.get("AXLabel") or "").strip()
                # The two apps label their slots DIFFERENTLY, and both forms are
                # live in this fleet:
                #   business (waiter) : '<HH:MM>Btn'  -- AddNewEventModal:1181
                #   consumer (diner)  : '<HH:MM>'     -- measured: '17:45' at x=21
                # So accept both. Requiring the 'Btn' suffix matched every business
                # chip and NO consumer chip, and the diner's booking then failed
                # with "no bookable time chip on this screen" against a form that
                # was showing eighteen of them.
                m = re.match(r"^(\d{1,2})[:.](\d{2})(Btn)?$", lbl)
                if not m:
                    continue
                f = e.get("frame", {}) or {}
                # Drop the BOOKINGS BOARD's hour labels, which stay in the tree
                # BEHIND the waiter's modal. They are bare 'HH:00' in the board's
                # left-hand gutter, and matching them picked '17:00' -- an hour
                # row, not a bookable slot -- when the real chips started at 17:35.
                # The consumer form has no board behind it, so this only ever
                # excludes the thing it is aimed at: a full-hour label sharing the
                # x of the other full-hour labels, with no 'Btn' suffix.
                if not m.group(3) and m.group(2) == "00" and hour_gutter_x is not None \
                        and abs((f.get("x") or 0) - hour_gutter_x) < 8:
                    continue
                cx = int(f.get("x", 0) + f.get("width", 0) / 2)
                cy = int(f.get("y", 0) + f.get("height", 0) / 2)
                out.append((int(m.group(1)) * 60 + int(m.group(2)),
                            lbl[:-3] if m.group(3) else lbl, cx, cy))
            return out

        slots = _read_slots()
        # The time-slot row sits LOWER in the create-booking form; on the phone (and sometimes
        # the iPad) it isn't rendered into the tree until it's scrolled into view. So if the
        # first read finds nothing, scroll the form down and re-read — a few times — before
        # concluding there's no slot. This is the fix for "no time chip visible -> Save blocked":
        # without a selected time the app's validation silently rejects Save.
        _scrolls = 0
        while not slots and _scrolls < 3:
            try:
                r.run_one("scroll down", 0)
                time.sleep(1.0)
            except Exception:
                break
            _scrolls += 1
            slots = _read_slots()
        if _scrolls:
            notes.append(f"    · @first_time_slot — scrolled {_scrolls}x to reveal the time slots")
        if slots:
            # The LogBox toasts are NOT avoided here any more.
            #
            # They do cover the slot row -- measured on the iPad: two stacked strips
            # at y=708 and y=761.5, each 1190x48, sitting over 17:35/17:40/17:45
            # whose centres are all at y=793. But dropping the covered chips threw
            # away the EARLIEST slots, which are the only ones inside the waiter's
            # 30-minute open window, and left the flow booking a time it could not
            # later open.
            #
            # It is also unnecessary. The Appium click below auto-scrolls the chip
            # clear of the toasts before tapping it, and that was verified with both
            # strips still on screen: not committed before the click, committed
            # after. Closing them is actively worse -- tapping a strip's close
            # control expands it into the full stack-trace viewer about as often as
            # it closes it (measured: 2 strips -> "Log 11 of 11" and 9 strips).
            slots.sort()
            from datetime import datetime as _dt
            now_min = _dt.now().hour * 60 + _dt.now().minute
            # Two competing constraints (both verified from the app source):
            #  • Too imminent (<~3 min): a slow run creeps past the slot before bookAppoitment
            #    fires -> the app rejects the stale slot and stays on the Reservation screen.
            #  • Too far (>30 min): the WAITER can't open the booking — BookingCard.onPress only
            #    navigates when now >= start-30min, else it just toasts. A far buffered slot is
            #    un-openable, which silently broke every cross-app waiter flow.
            # So book the EARLIEST slot ~5-28 min ahead: non-stale AND inside the 30-min window.
            window = [s for s in slots if 5 <= (s[0] - now_min) <= 28]
            future = [s for s in slots if (s[0] - now_min) >= 3]
            chosen = window[0] if window else (future[0] if future else slots[min(1, len(slots) - 1)])
            in_window = chosen in window
            _, lbl, cx, cy = chosen
            # Remember the booked slot: the waiter's My Bookings is a time-of-day calendar,
            # so @open_reservation steers to THIS hour's row to find the card (not blind-scroll).
            self._booked_slot = lbl
            tag = "within waiter 30-min open window" if in_window else "earliest available (may be outside window)"
            tapped = False
            # PRIMARY: tap the slot by its Appium element. WDA scrolls the real element into
            # view and taps its true centre, so 'selectedTime' actually registers. This matters
            # because the waiter's create-booking modal lays slots in a HORIZONTALLY SCROLLED
            # row: idb reports CONTENT-space x (e.g. beyond the visible strip), so an idb
            # coordinate tap lands in the wrong place and misses -> selectedTime stays null ->
            # Save is silently blocked (looks like "time & save never clicked"). An ACCESSIBILITY_ID
            # lookup is the fast/indexed locator (NOT the tree-regex predicates that hang), and we
            # bound it anyway so a slow snapshot can't wedge the run.
            import concurrent.futures as _fut
            safe = lbl.replace('"', '')
            def _click_slot():
                # The chip's accessibilityLabel is `${el}Btn` -- '17:35Btn', not
                # '17:35' (AddNewEventModal/index.js:1181). Searching the bare time
                # matched NOTHING (measured: ACCESSIBILITY_ID=0, PREDICATE=0 for
                # '17:35'; 3 matches for '17:35Btn'), so this returned False, the run
                # fell through to the idb coordinate tap, and that tap used a
                # CONTENT-space x from a horizontally scrolled row -- up to x=3680 on
                # a 1210pt screen -- so it landed on nothing. selectedTime stayed
                # null while the step reported the slot selected.
                for probe in (f"{safe}Btn", safe):
                    e = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, probe)
                    if not e:
                        e = r.d.find_elements(AppiumBy.IOS_PREDICATE,
                                              f'label == "{probe}"')
                    if e:
                        e[0].click()
                        return True
                return False
            _ex = _fut.ThreadPoolExecutor(max_workers=1)
            try:
                tapped = bool(_ex.submit(_click_slot).result(timeout=35))
                _ex.shutdown(wait=False)
                if tapped:
                    time.sleep(1.0)
                    notes.append(f"[ok] @first_time_slot — selected slot '{lbl}' (appium, {tag})")
            except Exception:
                _ex.shutdown(wait=False)
            # FALLBACK: idb coordinate tap (fine for the consumer's simple vertical list, where
            # content-space ~= screen-space). Only used if the Appium element wasn't found/clicked.
            if not tapped:
                # idb reports CONTENT-space x for this HORIZONTALLY SCROLLED row
                # (measured: slots run to x=3680 on a 1210pt screen), so a
                # coordinate tap only works for a chip that is really in the
                # viewport. Tapping an off-screen x hits whatever is at that point
                # -- usually nothing -- and selectedTime stays null.
                try:
                    _vw = float(r.d.get_window_size()["width"])
                except Exception:
                    _vw = 0.0
                if _vw and not (0 <= cx <= _vw):
                    notes.append(f"[FAIL] @first_time_slot — slot '{lbl}' is outside the "
                                 f"visible strip (x={cx:.0f} of {_vw:.0f}) and its chip "
                                 f"id did not resolve, so it cannot be tapped reliably")
                    return False
                try:
                    _sp.run([_IDB, "ui", "tap", "--udid", udid, str(cx), str(cy)], timeout=10)
                    time.sleep(1.0); tapped = True
                    notes.append(f"[ok] @first_time_slot — tapped time slot '{lbl}' via idb ({tag})")
                except Exception as ex:
                    notes.append(f"[FAIL] @first_time_slot — could not select slot '{lbl}' ({ex})")
                    return False

            # VERIFY THE SELECTION TOOK.
            #
            # A click that lands on nothing raises no error, so "tapped" only ever
            # meant "the gesture was dispatched". The run then reported
            # "selected slot '17:00'" while the form showed no time chosen at all,
            # and @save_appointment sat on a Save the app silently refuses.
            #
            # The chip states its own selection only through a background colour
            # (selectedTime === el ? '#d6d6d6' : '#eee'), which accessibility does
            # not expose -- so check the thing that DOES change: the app enables the
            # save control once a time is committed.
            if not self._time_slot_committed(r, lbl):
                notes.append(f"[FAIL] @first_time_slot — tapped '{lbl}' but no time is "
                             f"selected (the form still has none), so Save would be "
                             f"silently rejected")
                return False
            return True
        # NO SLOT. This used to report "[ok] ... booking proceeds without a slot",
        # which is not a thing that can happen: BOOK NOW only goes live once a time
        # chip is committed, so the flow went on to tap it three times, get ignored
        # each time, and fail with "the booking dialog never opened" — a true
        # statement about the wrong step. Fail here, with the real reason.
        notes.append("[FAIL] @first_time_slot — no bookable time chip on this screen, so the "
                     "booking cannot proceed (BOOK NOW stays inert without a committed slot). "
                     "Usually the restaurant has no open slot left for the chosen date/duration "
                     "at this hour, not an automation fault.")
        return False

    def _on_home(self) -> bool:
        """Are we on the Home tab? Checked via idb (sees the full tree; the Appium
        snapshot is depth-capped and misses these)."""
        import subprocess
        udid = getattr(self, "_cur_udid", "") or self.devices.get("consumer") or DEFAULT_CONSUMER_UDID
        try:
            raw = subprocess.run([_IDB, "ui", "describe-all", "--udid", udid],
                                 capture_output=True, text=True, timeout=20).stdout
            # "NylaiKitchen2" was in this list and is NOT Home-specific: the Wallet
            # renders the same restaurant as "NylaiKitchen2Card", which contains it
            # as a substring. That made this return True on the Wallet, so
            # @consumer_home reported "Home reached" without tapping anything and
            # the next step then hunted for a restaurant card on the wrong screen.
            if any(m in raw for m in
                   ("upcomingBlock", "finishedBlock", "walletUpcomingSearchInput")):
                return False          # unambiguously the Wallet
            return any(m in raw for m in
                       ("homeSearchBar", "homeCouponIcon", "homeFilter"))
        except Exception:
            return False

    # Controls that dismiss a first-run intro, in priority order. Label matching is
    # case-insensitive and generic on purpose: these are the words onboarding
    # carousels use, not one app's ids, so a newly onboarded app needs no code here.
    _SKIP_LABELS = ("skip", "skipfornow", "getstarted", "continue", "next", "done")
    # Markers that say "this is a first-run/signed-out screen". Kept separate from
    # _on_home's Home markers: absence of Home does NOT imply first run (the app may
    # simply be on the Wallet), so first run needs positive evidence of its own.
    # Matched case-insensitively with punctuation stripped, so "SIGN-IN", "Sign In"
    # and "signIn" are one marker rather than three spellings to keep in sync.
    # Already normalised (no spaces/punctuation) because they are matched against a
    # normalised blob — a marker written "stop exploring" could never match, since
    # normalisation removes the space from the screen text too.
    _FIRST_RUN_MARKERS = ("stopexploring", "startdiscovering", "signin", "signup",
                          "login", "getstarted", "welcome")

    def _first_run_preamble(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Clear a first-run intro (and sign in) if the app is showing one.

        A no-op on an app that is already past first run, so every flow can call it
        unconditionally. Returns True if it changed anything.

        Why this exists: the platform installs apps automatically now, and a fresh
        install always comes up at onboarding, signed out -- which made every step
        after it fail against a screen that never loads. `uninstall`'s own docstring
        flags this exact trade-off; this is the "first-run preamble" it refers to.
        """
        acted = False
        for _ in range(8):                      # carousels are a few pages long
            els = self._idb_els()
            if not els:
                break
            blob = _norm(" ".join(str(e.get("label") or "") for e in els))
            if not any(m in blob for m in self._FIRST_RUN_MARKERS):
                break                           # not a first-run screen -> done
            if self._on_home():
                break
            skip = next((e for e in els
                         if _norm(e.get("label")) in self._SKIP_LABELS), None)
            if not skip:
                # Past the carousel and onto the signed-out landing screen: the way
                # forward is the sign-in entry point, not a skip control.
                # Substring, not equality: the control that opens the email form is
                # spelled "SIGN-IN", "Login with mail address", "Sign in with email"
                # … depending on screen and build. Prefer an email/mail one when the
                # screen offers social logins we cannot drive (Google/Apple open
                # system sheets), and never match those.
                cands = [e for e in els
                         if any(k in _norm(e.get("label"))
                                for k in ("signin", "login", "continuewithemail"))
                         and not any(b in _norm(e.get("label"))
                                     for b in ("google", "apple", "facebook"))
                         # Prose is not a control: the explanatory paragraph on this
                         # screen contains "Sign in …" and outranked the real button.
                         and e.get("type") != "StaticText"
                         and len(str(e.get("label") or "")) <= 40]
                entry = next((e for e in cands
                              if any(k in _norm(e.get("label"))
                                     for k in ("mail", "email"))), None) \
                    or (cands[0] if cands else None)
                if entry:
                    notes.append(f"[ok] first run — opening '{entry.get('label')}'")
                    self._idb_tap(entry["cx"], entry["cy"])
                    acted = True
                    time.sleep(2.5)
                    continue
                break
            notes.append(f"[ok] first run — tapped '{skip.get('label')}'")
            self._idb_tap(skip["cx"], skip["cy"])
            acted = True
            time.sleep(1.5)
            if self._on_home():
                return True

        # Signed out? Use the credentials the run already carries — the same source
        # the business roles use, so nothing app-specific is hardcoded here.
        if self._looks_signed_out():
            if self._sign_in_consumer(r, notes):
                acted = True
        return acted

    def _looks_signed_out(self) -> bool:
        blob = _norm(" ".join(str(e.get("label") or "") for e in self._idb_els()))
        return (any(m in blob for m in ("signin", "login", "password"))
                and not self._on_home())

    def _sign_in_consumer(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Sign the consumer in using the run's configured credentials."""
        creds = (self.credentials or {}).get("consumer") or {}
        email, password = creds.get("email"), creds.get("password")
        if not (email and password):
            notes.append("[warn] first run — signed out and no consumer credentials "
                         "configured; cannot sign in")
            return False
        try:
            from appium.webdriver.common.appiumby import AppiumBy
            fields = r.d.find_elements(AppiumBy.IOS_PREDICATE,
                                       'type == "XCUIElementTypeTextField" OR '
                                       'type == "XCUIElementTypeSecureTextField"')
            if len(fields) < 2:
                notes.append("[warn] first run — sign-in form not recognised")
                return False
            fields[0].send_keys(email)
            fields[1].send_keys(password)
            self._hide_keyboard(r, notes)
            btn = next((e for e in self._idb_els()
                        if _norm(e.get("label")) in ("signin", "login", "submit")), None)
            if btn:
                self._idb_tap(btn["cx"], btn["cy"])
            time.sleep(4)
            notes.append(f"[ok] first run — signed in as {email}")
            return True
        except Exception as e:
            notes.append(f"[warn] first run — sign-in failed: {str(e)[:120]}")
            return False

    def _consumer_home(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Reach the Home tab. The app resumes on its last screen (often the Wallet
        with a booking). The fuzzy resolver can't reach the bottom tab in this app's
        huge tree, so tap `homeTab` DIRECTLY by accessibility id (verified), backing
        out of any blocking sub-screen, and confirm Home via idb."""
        # A FRESHLY INSTALLED app does not resume anywhere — it starts at first run:
        # the onboarding carousel, signed out. Every later step then hunts for
        # controls on a screen that is not there. This is not hypothetical: the
        # platform now installs apps automatically, so the first run after any deploy
        # lands here. Clear it before looking for Home.
        self._first_run_preamble(r, notes)

        tapped = False
        for _ in range(3):
            # Clear a leftover booking-confirmed dialog FIRST. It has no back or
            # close control, and the screen behind it still reports screenBackBtn —
            # so the generic back-out below "succeeds" against a covered button and
            # nothing moves. `orderLater` is a GenericElement that Appium's
            # collapsed snapshot never exposes, so go through idb.
            self._clear_logbox()
            leftover = next((e for e in self._idb_els()
                             if e.get("label") in ("orderLater", "pickUpOrderConfirm")), None)
            if leftover and not self._on_home():
                notes.append("[ok] @consumer_home — dismissed leftover booking dialog")
                self._idb_tap(leftover["cx"], leftover["cy"])
                time.sleep(1.5)
            try:
                # idb FIRST. The bottom tab is a Button labelled "Home"; a stale
                # hidden "homeTab" node also matches by id, and clicking that
                # navigates nowhere while still setting tapped=True — so the real
                # tab never got pressed and the run carried on from the Wallet.
                tab = next((e for e in self._idb_els()
                            if (e.get("label") or "").strip() == "Home"), None)
                if tab:
                    self._idb_tap(tab["cx"], tab["cy"])
                    tapped = True
                    time.sleep(1.5)
                    if self._on_home():
                        notes.append("[ok] @consumer_home — Home reached")
                        return True
                els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "homeTab")
                if not els:
                    # This build exposes the bottom tab as a Button labelled "Home",
                    # not as an id "homeTab". With only the id lookup, nothing was
                    # ever tapped — and the best-effort fallback below then reported
                    # "Home reached" while the app sat on Wallet, so the next step
                    # hunted for a restaurant card that is not on that screen.
                    els = r.d.find_elements(
                        AppiumBy.IOS_PREDICATE,
                        'type == "XCUIElementTypeButton" AND (name == "Home" OR label == "Home")')
                if els:
                    els[0].click(); tapped = True
                else:
                    tab = next((e for e in self._idb_els()
                                if (e.get("label") or "").strip() == "Home"), None)
                    if tab:
                        self._idb_tap(tab["cx"], tab["cy"]); tapped = True
            except Exception:
                pass
            time.sleep(2.0)
            if self._on_home():
                notes.append("[ok] @consumer_home — Home reached")
                return True
            # A sub-screen (open booking) may block the tab — back out, then retry.
            # `orderLater` is last: the booking-confirmed dialog has NO back or close
            # control, so its only exit is one of its two choices. A run that died
            # after BOOK NOW leaves it on screen, and every later run then failed at
            # "Home tab not found" until someone dismissed it by hand.
            # An open booking detail on the Wallet swallows tab taps. Its close
            # control is named "<DinerName>Close" — the list here hardcoded
            # "NooluNagaClose", so it never matched Roopa's "RoopaDClose" and the
            # run sat on the Wallet while reporting it had tapped Home. Match the
            # PATTERN, not one diner.
            closer = next((e for e in self._idb_els()
                           if (e.get("label") or "").strip().endswith("Close")), None)
            if closer:
                self._idb_tap(closer["cx"], closer["cy"])
                time.sleep(1.2)
                continue
            for back in ("screenBackBtn", "walletBackBtn"):
                try:
                    b = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, back)
                    if b:
                        b[0].click(); time.sleep(0.8); break
                except Exception:
                    pass

        # idb couldn't CONFIRM Home, but the tab responded — proceed best-effort. The
        # very next step (tap a restaurant card) fails loudly if we're not actually on
        # Home, so a genuine problem still surfaces; a flaky idb read no longer false-fails.
        if tapped:
            notes.append("[ok] @consumer_home — tapped Home tab (idb didn't confirm; proceeding)")
            return True
        notes.append("[FAIL] @consumer_home — Home tab not found on screen")
        return False

    def _to_checkout(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Open the cart (if not already there) and tap CHECKOUT → Stripe sheet.
        Robust to either landing on the menu or straight on the cart."""
        def on_cart():
            try:
                return bool(r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "cartCheckout"))
            except Exception:
                return False
        if not on_cart():
            for cid in ("cartImage", "homeCartIcon"):
                try:
                    els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cid)
                    if els:
                        els[0].click(); time.sleep(1.8)
                        break
                except Exception:
                    pass
        try:
            els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "cartCheckout")
            if els:
                els[0].click(); time.sleep(2.5)
                notes.append("[ok] @to_checkout — opened checkout")
                return True
        except Exception as e:
            notes.append(f"[FAIL] @to_checkout: {str(e).splitlines()[0][:100]}")
            return False
        notes.append("[FAIL] @to_checkout — cartCheckout not found")
        return False

    def _pay_stripe(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Stripe checkout sheet (TEST MODE): the saved card (Visa ••••4242) is
        preselected, so just tap the blue 'Pay € X' button — NOT 'Pay with link'."""
        bill = self._read_bill_total(r)
        try:
            els = r.d.find_elements(
                AppiumBy.IOS_PREDICATE,
                'label CONTAINS[c] "Pay €" OR label CONTAINS[c] "Pay â‚¬" '
                'OR name CONTAINS[c] "Pay €"')
            for e in els:
                lbl = (e.get_attribute("label") or e.get_attribute("name") or "")
                if "link" in lbl.lower():          # skip the green "Pay with link"
                    continue
                if not e.is_displayed():
                    continue
                e.click(); time.sleep(3.0)
                notes.append(f"[ok] @pay_stripe — tapped '{lbl.strip()}' (bill €{bill})")
                return True
        except Exception as e:
            notes.append(f"[FAIL] @pay_stripe: {str(e).splitlines()[0][:120]}")
            return False
        notes.append("[FAIL] @pay_stripe — 'Pay €' button not found")
        return False

    def _got_it(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Dismiss the 'Pre-Order Confirmed' dialog (button text 'YES, GOT IT' is
        not its id). Non-fatal — the order is already paid/placed by this point."""
        for _ in range(3):
            # The control's accessibility id is `pickUpOrderConfirm`; "YES, GOT IT"
            # is only its VISIBLE text, and it is a GenericElement that Appium's
            # collapsed snapshot never reports. So the predicate below matched
            # nothing, this returned "no confirmation dialog present", and the
            # full-screen modal stayed up — blocking every later run on that
            # device until someone dismissed it by hand.
            btn = next((e for e in self._idb_els()
                        if (e.get("label") or "").strip() == "pickUpOrderConfirm"), None)
            if btn:
                self._idb_tap(btn["cx"], btn["cy"]); time.sleep(1.2)
                notes.append("[ok] @got_it — dismissed Pre-Order Confirmed")
                return True
            try:
                for e in r.d.find_elements(
                        AppiumBy.IOS_PREDICATE,
                        'label CONTAINS[c] "got it" OR name CONTAINS[c] "got it"'):
                    if e.is_displayed():
                        e.click(); time.sleep(1.2)
                        notes.append("[ok] @got_it — dismissed Pre-Order Confirmed")
                        return True
            except Exception:
                pass
            time.sleep(1.0)
        notes.append("[ok] @got_it — no confirmation dialog present")
        return True

    def _scroll_into_view(self, r, label: str, tries: int = 8) -> bool:
        """Bring a bookings-board card into the viewport before clicking it.

        MEASURED on the phone: the diner's 12:00 card sat at y=1216 on an 852pt
        screen — below the fold, is_displayed()==False — so the click landed on
        nothing and the reservation never opened, which is what
        '@open_reservation timed out after 150s' actually was.

        WDA's own "mobile: scroll" toVisible fails here with "max scroll count
        reached": the timeline is a plain RN ScrollView, not a cell-based list.
        Appium's rect DOES track the live scroll position though, so compute the
        swipe from it and verify after each one. Two swipes covered 1216 -> 396.
        """
        # Every Appium call here is BOUNDED. _swipe_element and appium_click already
        # are; this one was not, and it is the only unbounded path @open_reservation
        # can take. A wedged WDA parks in find_elements/rect on this app's huge tree
        # with no ceiling of its own, so the step burned its full 240s and was killed
        # by the segment watchdog -- reported as "step hung", which names the symptom
        # and hides that a single resolve was the thing stuck.
        import concurrent.futures as _fut

        def _bounded(fn, secs: float = 30.0, default=None):
            ex = _fut.ThreadPoolExecutor(max_workers=1)
            try:
                return ex.submit(fn).result(timeout=secs)
            except Exception:
                return default
            finally:
                # wait=False: never block on a worker that is still stuck in WDA.
                ex.shutdown(wait=False)

        W = _bounded(lambda: r.d.get_window_size())
        if not W:
            return False
        safe = (label or "").replace('"', "")
        for _ in range(tries):
            rc = _bounded(lambda: (r.d.find_elements(
                AppiumBy.IOS_PREDICATE, f'label == "{safe}"') or [None])[0].rect)
            if not rc:
                return False
            top, h = rc["y"], rc["height"]
            if 90 <= top and top + h <= W["height"] - 90:
                return True                       # comfortably on screen
            dy = (top + h / 2) - W["height"] * 0.45
            step = max(-420, min(420, dy))        # cap per swipe so we cannot overshoot wildly
            from_y = W["height"] * (0.72 if step > 0 else 0.28)
            to_y = max(80, min(W["height"] - 80, from_y - step))
            if _bounded(lambda: r.d.execute_script(
                    "mobile: dragFromToForDuration",
                    {"duration": 0.6, "fromX": W["width"] // 2,
                     "fromY": int(from_y), "toX": W["width"] // 2,
                     "toY": int(to_y)}) or True, 45.0) is None:
                return False
            time.sleep(1.0)
        return False

    # ── horizontal card search (one hour row) ───────────────────────────────
    # The board is a DAY CALENDAR whose every hour row is its own
    # <ScrollView horizontal={true}> (Business App/Screens/Home/index.js:1190), with
    # flexShrink:0 cards laid side by side. Measured on the iPad (viewport 1210 wide):
    #     RoopaDcardCompleted      x=208
    #     RoopaDcardCompleted      x=700
    #     tanishcardReserved       x=1192   <- past the right edge
    #     tanishcardInProgress     x=1684   <- past the right edge
    #     NooluNagacardInProgress  x=2176   <- past the right edge
    # idb and Appium BOTH report live (scroll-adjusted) coordinates, so a card outside
    # the viewport is still DISCOVERED — it just cannot be clicked, because a click on
    # an off-screen element silently does nothing. Hence: discover with the existing
    # logic, then bring the chosen card into the viewport before clicking it.
    #
    # Two gesture facts, both measured, both non-obvious:
    #   • 'mobile: dragFromToForDuration' does NOT scroll this row horizontally (0pt
    #     movement at any duration). 'mobile: swipe' with an ELEMENT does — one swipe
    #     moved the row 680pt.
    #   • The swipe surface must be an element ACTUALLY INSIDE the viewport. Swiping on
    #     the RoopaDcardCompleted instance at x=-472 was a no-op in BOTH directions —
    #     indistinguishable from "end of row" unless the surface is validated first.
    #     That is why _pick_swipe_surface refuses the target when the target is the
    #     thing that is off-screen, and why no-progress is only believed after a
    #     confirmed-visible surface produced no movement.
    #: A swipe surface needs a real finger-sized patch on screen, not one stray pixel.
    _MIN_SWIPE_SURFACE_PX = 200
    #: Cards within this many points of the target's y count as its hour row.
    _ROW_Y_TOLERANCE = 40

    @staticmethod
    def _card_visible_px(card: dict, viewport_w: float) -> float:
        """How much of *card* lies inside the viewport horizontally."""
        x, w = card.get("x") or 0, card.get("w") or 0
        return max(0.0, min(x + w, viewport_w) - max(x, 0.0))

    @classmethod
    def _row_cards(cls, cards: List[dict], target: dict) -> List[dict]:
        """The cards sharing *target*'s hour row (same horizontal ScrollView)."""
        ty = target.get("cy")
        if ty is None:
            return list(cards)
        return [c for c in cards
                if abs((c.get("cy") or 0) - ty) <= cls._ROW_Y_TOLERANCE]

    @classmethod
    def _pick_swipe_surface(cls, cards: List[dict], target: dict, viewport_w: float,
                            min_visible: Optional[float] = None) -> Optional[dict]:
        """The card to perform the gesture ON: the one most inside the viewport.

        NEVER returns a card that is not genuinely on screen, and never returns the
        target while the target is the off-screen one — a gesture on an off-screen
        element reports success and moves nothing, which reads as a boundary.
        """
        floor = cls._MIN_SWIPE_SURFACE_PX if min_visible is None else min_visible
        row = cls._row_cards(cards, target)
        usable = [c for c in row if cls._card_visible_px(c, viewport_w) >= floor]
        if not usable:
            return None
        return max(usable, key=lambda c: cls._card_visible_px(c, viewport_w))

    @classmethod
    def _swipe_direction(cls, target: dict, viewport_w: float) -> str:
        """Finger direction that reveals *target*.

        XCUITest reads direction as the way the FINGER travels, so 'left' reveals
        content off to the RIGHT. Decide on the edge that is actually clipped, not on
        x alone: a card at x=1004 on a 1210 viewport has its LEFT edge on screen while
        its right half hangs off, and still needs the row to move left. Testing
        `x >= viewport_w` sent that case the wrong way and the loop swung back and
        forth without ever converging.
        """
        x, w = target.get("x") or 0, target.get("w") or 0
        return "left" if x + w > viewport_w else "right"

    @classmethod
    def _target_in_viewport(cls, target: dict, viewport_w: float) -> bool:
        """Fully inside the viewport horizontally — a partly-clipped card can still
        take a click on the wrong half, so require the whole width."""
        x, w = target.get("x") or 0, target.get("w") or 0
        return x >= 0 and x + w <= viewport_w

    def _find_target_card(self, cards: List[dict], label: str,
                          diner_key: str = "") -> Optional[dict]:
        """The live element for *label*, matched by IDENTITY rather than by the whole
        string: the status suffix moves mid-flow (Reserved -> InProgress -> Completed,
        and the phone spells it '<Name><Status>Card'), so a scroll loop pinned to the
        exact label would lose its target the moment the board re-rendered."""
        exact = [c for c in cards if (c.get("label") or "").strip() == label]
        if exact:
            return exact[0]
        want, _ = self._split_card(label)
        want = (want or diner_key or "").strip().lower()
        if not want:
            return None
        for c in cards:
            who, _st = self._split_card((c.get("label") or "").strip())
            if who.strip().lower() == want:
                return c
        return None

    def _cards_on_board(self, els: Optional[List[dict]] = None) -> List[dict]:
        """Every booking card idb can see, on screen or scrolled out of it."""
        return [e for e in (self._idb_els() if els is None else els)
                if "card" in ((e.get("label") or "") + (e.get("id") or "")).lower()
                and (e.get("w") or 0) > 60]

    def _scroll_card_into_view_h(self, r: ScenarioRunner, label: str, notes: List[str],
                                 diner_key: str = "", slot: str = "", tries: int = 8) -> bool:
        """Horizontally scroll the target's hour row until the target is in the viewport.

        Returns True when the target is (or becomes) fully visible. Reports the search
        as it goes so a failure says which cards were seen and what the scroll did.
        """
        try:
            viewport_w = float(r.d.get_window_size()["width"])
        except Exception:
            return False

        def _fmt(cards):
            return [f"{(c.get('label') or '').strip()}@x={int(c.get('x') or 0)}"
                    for c in sorted(cards, key=lambda c: c.get("x") or 0)]

        cards = self._cards_on_board()
        target = self._find_target_card(cards, label, diner_key)
        if target is None:
            notes.append(f"    [card search] target={label!r} not on the board")
            return False
        row = self._row_cards(cards, target)
        notes.append(f"    [card search] target={label!r}")
        if slot:
            notes.append(f"    [card search] slot={slot!r}")
        notes.append(f"    [card search] visible cards: {_fmt(row)}")
        if self._target_in_viewport(target, viewport_w):
            notes.append("    [card search] target already in the viewport")
            return True
        notes.append("    [card search] target not visible — horizontal scroll required")

        stalled = 0
        for i in range(1, tries + 1):
            surface = self._pick_swipe_surface(cards, target, viewport_w)
            if surface is None:
                notes.append(f"    [card search] scroll {i} — no card is far enough inside "
                             f"the viewport to swipe on safely; refusing to gesture on an "
                             f"off-screen element")
                return False
            direction = self._swipe_direction(target, viewport_w)
            before_x = surface.get("x") or 0
            if not self._swipe_element(r, surface, direction):
                notes.append(f"    [card search] scroll {i} — swipe {direction} on "
                             f"{(surface.get('label') or '')!r} could not be dispatched")
                return False
            time.sleep(1.2)
            cards = self._cards_on_board()
            moved_el = self._find_target_card(cards, (surface.get("label") or "").strip())
            row = self._row_cards(cards, target) if target else cards
            notes.append(f"    [card search] scroll {i} — new visible cards: {_fmt(row)}")
            target = self._find_target_card(cards, label, diner_key) or target
            if self._target_in_viewport(target, viewport_w):
                notes.append(f"    [card search] found "
                             f"{(target.get('label') or '').strip()!r} in the viewport")
                return True
            # Only NOW is no-progress meaningful: the gesture went to a surface we had
            # already confirmed was visible, so nothing moving is the row's own limit.
            after_x = (moved_el or {}).get("x", before_x)
            if abs(after_x - before_x) < 1:
                stalled += 1
                if stalled >= 2:
                    notes.append(f"    [card search] scroll {i} — the row did not move on a "
                                 f"confirmed-visible surface twice; end of row reached")
                    return False
            else:
                stalled = 0
        notes.append(f"    [card search] target still off-screen after {tries} scrolls")
        return False

    def _swipe_element(self, r: ScenarioRunner, card: dict, direction: str) -> bool:
        """'mobile: swipe' on the Appium element matching *card*.

        Element-scoped on purpose: dragFromToForDuration does not move this row at all
        (measured 0pt), and a screen-level swipe has no way to say WHICH hour row it
        means. The instance is matched back by x so a repeated label cannot hand us the
        off-screen twin.
        """
        import concurrent.futures as _fut
        safe = (card.get("label") or "").strip().replace('"', "")
        if not safe:
            return False

        def _do():
            els = r.d.find_elements(AppiumBy.IOS_PREDICATE, f'label == "{safe}"')
            if not els:
                return False
            el = els[0]
            for cand in els:                      # the instance actually on screen
                try:
                    if abs(cand.rect["x"] - (card.get("x") or 0)) < 3:
                        el = cand
                        break
                except Exception:
                    continue
            r.d.execute_script("mobile: swipe", {"direction": direction, "element": el.id})
            return True
        try:
            with _fut.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_do).result(timeout=45)
        except Exception:
            return False

    def _open_reservation(self, r: ScenarioRunner, notes: List[str],
                          statuses: tuple = ("reserved", "confirmationpending"),
                          what: str = "@open_reservation") -> bool:
        """Open the diner's reservation in the waiter 'My Bookings' timeline.

        Hard-won facts about this screen (all verified on-device):
        • It's a DAY CALENDAR: the diner's card lives in its booked-HOUR row (18:20 -> 18:00).
        • The bookings list only populates a few seconds AFTER a date is (re)selected, and the
          app sometimes wedges (unhandled promise rejections) showing an empty shell — a
          RELAUNCH recovers it.
        • idb reports every card at its CONTENT-space y (e.g. the 18:00 card at y≈2412 on an
          834-tall screen) — the whole 24h timeline is in the tree at fixed layout coords that
          do NOT change when you scroll. So an idb coordinate-tap CANNOT reach an off-screen
          card, and idb swipes don't reliably scroll this list.
        So: idb only DETECTS the target card's exact label (it sees the full content tree);
        the TAP is an Appium element click, which makes WDA auto-scroll the card into view.
        Card id/label pattern is '<Name>card<Status>' (e.g. 'RoopaDcardReserved'); this feeds
        the ASSIGN flow so we want the diner's RESERVED (assignable) card — never a stale
        InProgress/Expired one — disambiguated to the booked hour when several diner cards exist."""
        from datetime import datetime as _dt
        import concurrent.futures as _fut

        # A leftover "Select A Table" sheet from an earlier run collapses this
        # screen's tree to a handful of elements, so the card scan below sees
        # "0 card element(s)" and the whole consumer -> waiter handoff fails with
        # what looks like a missing booking. Clear it before reading the board.
        self._dismiss_table_modal(r, notes)

        # The bookings list renders a few seconds AFTER the board appears, so a
        # scan fired immediately reads an empty board. Keep this cheap: each
        # _idb_els() is a subprocess, and this step has a 150s ceiling that a
        # 20-iteration poll blows on its own.
        for _ in range(5):
            if any("card" in (e.get("label") or "").lower() for e in self._idb_els()):
                break
            time.sleep(2.0)
        else:
            notes.append("[warn] booking board still empty — no cards rendered")

        name = CONSUMER_NAME.lower()
        slot = getattr(self, "_booked_slot", "") or ""           # e.g. '18:20'
        ms = re.match(r"^(\d{1,2})", slot)
        hour_lbl = f"{int(ms.group(1)):02d}:00" if ms else ""    # booked hour row, e.g. '18:00'
        bundle = self.business_bundle

        def hour_y(els):
            for e in els:
                if e["label"].strip() == hour_lbl:
                    return e["cy"]
            return None

        def open_events_for_hour(els) -> bool:
            """Tap the hour's 'Events N' badge to list that hour's bookings.

            The board lays an hour's bookings out SIDEWAYS in a horizontal strip, so a
            card beyond the second or third sits off-screen and could only be reached
            by swiping across the row -- slow, and it fails outright when the swipe
            surface is itself off-screen (measured: a target at x=2668 on a ~1200px
            viewport, then a 240s hang).

            The badge beside each hour is the app's own answer to that: it toggles
            selectEventTime to the hour (Screens/Home/index.js addCountfunc) and lists
            that hour's events vertically, where every one of them is reachable without
            a single horizontal gesture.

            The badge carries no accessibility id -- it is a TouchableOpacity whose two
            Text children render as 'Events <count>' -- so it is matched on that text,
            anchored to the hour row's y so the right hour's badge is tapped.
            """
            if not hour_lbl:
                return False
            y = hour_y(els)
            if y is None:
                return False
            # The badge renders just BELOW its hour label, not level with it —
            # measured at a consistent +50px on the iPad, which is outside the row
            # tolerance used for cards. So take the badge whose own nearest hour
            # label is the one we want, rather than the badge nearest the label.
            all_hours = [(str(e.get("label") or "").strip(), e.get("cy") or 0)
                         for e in els
                         if re.match(r"^\d{1,2}:00$", str(e.get("label") or "").strip())]
            badges = [e for e in els if _norm(e.get("label")).startswith("events")]
            b = None
            for cand in badges:
                cy = cand.get("cy") or 0
                nearest = min(all_hours, key=lambda h: abs(h[1] - cy), default=None)
                if nearest and nearest[0] == hour_lbl:
                    b = cand
                    break
            if b is None:
                return False
            # The badge sits BELOW its hour label, so scrolling the LABEL into view
            # does not guarantee the badge is on screen -- measured: 13:00 label at
            # y=1658 with its 'Events 9' badge at y=1706 on an 834pt screen, both far
            # below the fold. The previous version tapped that stale coordinate, hit
            # empty space, and the run then clicked a board card instead of a sidebar
            # row and sat in "no meaningful UI change" for 240s.
            try:
                _h = float(r.d.get_window_size()["height"])
            except Exception:
                _h = 0.0
            if _h and not (0.05 * _h <= b["cy"] <= 0.92 * _h):
                notes.append(f"    · the {hour_lbl} events badge is off-screen "
                             f"(y={b['cy']:.0f} of {_h:.0f}) — scrolling it into view")
                for _ in range(8):
                    # 'mobile: swipe' must be anchored on an ELEMENT that is really
                    # inside the viewport (see _MIN_SWIPE_SURFACE_PX above): the
                    # elementless form is a measured no-op on this board -- 10
                    # consecutive swipes left the 13:00 badge at y=1730 exactly.
                    # An hour label currently on screen is a reliable anchor.
                    # MEASURED: 'up' scrolls the calendar TOWARDS LATER HOURS
                    # (one swipe moved 13:00 from y=1679 to y=1089). A row BELOW
                    # the fold is reached by swiping UP, not down -- the inverted
                    # version was a silent no-op, which is exactly why this used to
                    # report a successful scroll while 13:00 never moved.
                    if not self._swipe_calendar(r, "up" if b["cy"] > _h else "down"):
                        notes.append("    · the calendar did not scroll")
                        return False
                    time.sleep(1.0)
                    fresh = self._idb_els()
                    b2 = None
                    hs = [(str(e.get("label") or "").strip(), e.get("cy") or 0)
                          for e in fresh
                          if re.match(r"^\d{1,2}:00$", str(e.get("label") or "").strip())]
                    for cand in [e for e in fresh
                                 if _norm(e.get("label")).startswith("events")]:
                        near = min(hs, key=lambda h: abs(h[1] - (cand.get("cy") or 0)),
                                   default=None)
                        if near and near[0] == hour_lbl:
                            b2 = cand
                            break
                    if b2 is None:
                        continue
                    b = b2
                    if 0.05 * _h <= b["cy"] <= 0.92 * _h:
                        break
                else:
                    notes.append(f"    · could not bring the {hour_lbl} events badge "
                                 f"on screen")
                    return False
            try:
                self._idb_tap(b["cx"], b["cy"])
            except Exception as e:
                notes.append(f"    · events badge tap failed: {type(e).__name__}")
                return False
            notes.append(f"    · opened the {hour_lbl} events list "
                         f"({(b.get('label') or '').strip()!r}) — no horizontal scrolling")
            time.sleep(2.0)
            # The badge is a TOGGLE (addCountfunc: `selectEventTime === el ? null : el`).
            # If a previous hour's list was already open, this tap CLOSES it and the
            # board is back to its horizontal strips. Confirm the sidebar is actually
            # up, and tap once more if it is not.
            for _ in range(2):
                # Wait for the ROWS, not merely for the panel. The list fetches its
                # bookings, and MEASURED on the iPad they land ~3.9s after the tap --
                # well past the old flat 2.0s wait. Returning early made
                # _click_sidebar_row scan an empty list, find nothing and fall back to
                # the board, where two bookings share one 'RoopaDcardReserved' label
                # and the wrong one gets opened. That is the whole 240s hang.
                for _ in range(16):               # ~12s, polled every 0.75s
                    els_now = self._idb_els()
                    if any(_SIDEBAR_ROW_RE.search((e.get("label") or "").strip())
                           for e in els_now):
                        return True
                    if self._events_sidebar_up(els_now) and any(
                            "orders not found" in (e.get("label") or "").lower()
                            for e in els_now):
                        return True               # genuinely empty hour, not a race
                    time.sleep(0.75)
                try:
                    self._idb_tap(b["cx"], b["cy"])
                except Exception:
                    return False
                time.sleep(2.0)
            return self._events_sidebar_up(self._idb_els())

        def scroll_to_hour(els) -> bool:
            """Bring the booked hour's row into view VERTICALLY before looking for a card.

            My Bookings is a time-of-day calendar: each hour is a row, and each row is
            its own horizontal strip. The board opens on the current hour, so a booking
            an hour or two later sits in a row that is off-screen DOWNWARDS -- and the
            horizontal search then found the target at x=2668 on a ~1200px viewport and
            tried to swipe across five intervening cards to reach it. Swiping a long
            way sideways is slow, fails on cards that are themselves off-screen, and
            was the step that hung for 240s.

            Scrolling the calendar to the hour first puts the row (and usually the card)
            in the viewport, which is how a person would do it: find the time, then the
            event under that time.
            """
            if not hour_lbl:
                return False
            # Tell _swipe_calendar which row we are aiming at, so it can cover the
            # distance in ONE drag instead of stepping ~590pt per 9.3s swipe.
            self._scroll_target_hour = hour_lbl
            for _ in range(6):
                els = self._idb_els()
                y = hour_y(els)
                if y is not None:
                    try:
                        h = float(r.d.get_window_size()["height"])
                    except Exception:
                        return True
                    # Comfortably inside the visible area, not under the header/footer.
                    if 0.12 * h <= y <= 0.80 * h:
                        return True
                    # 'up' moves towards LATER hours (measured, see _swipe_calendar).
                    direction = "up" if y > 0.80 * h else "down"
                else:
                    direction = "up"        # not rendered yet: later hours are below
                # Element-anchored: the elementless form does not move this list
                # (measured — ten swipes, zero movement), which is why this used to
                # report "scrolled the calendar to the 13:00 row" while 13:00 was
                # still at y=1658 on an 834pt screen.
                if not self._swipe_calendar(r, direction):
                    break
                time.sleep(1.0)
            y_final = hour_y(self._idb_els())
            if y_final is None:
                return False
            try:
                h2 = float(r.d.get_window_size()["height"])
            except Exception:
                return True
            return 0.05 * h2 <= y_final <= 0.92 * h2

        def bookings_loaded(els):
            """The list is up when we can see hour labels or any booking card."""
            return any(re.match(r"^\d{1,2}:\d{2}$", e["label"].strip()) for e in els) \
                or any("card" in (e["id"] + e["label"]).lower() for e in els)

        # Statuses whose card is OPENABLE. For the ASSIGN flow (the default) that's:
        #  • 'reserved'            — a CONSUMER-booked reservation (waiter accepts it), and
        #  • 'confirmationpending' — a WAITER-created booking (e.g. the kitchen demo makes its
        #    own order); it needs the waiter to open it and assign a table.
        # The SERVE flow (@open_order) passes ('inprogress',) instead: by then the order has
        # been sent to the kitchen, so the card has moved on from Reserved and only the
        # in-progress one is the right ticket to serve and settle.
        ASSIGNABLE = statuses

        # Card labels are '<Name>card<Status>'. The STATUS SUFFIX MOVES during the
        # flow — Reserved -> InProgress -> Completed — so a locator pinned to one
        # status stops existing while the booking is still perfectly real. And the
        # same diner accumulates several cards across runs (SnehaGunaga alone had
        # Expired/InProgress/Completed/Reserved on the board), so the name on its
        # own is ambiguous. Split the label so status can be reasoned about
        # explicitly instead of being substring-matched out of the whole string.
        self._last_card_diag = ""

        def pick_label(els):
            """Exact AXLabel of the card to open, or None (diagnostic in
            self._last_card_diag)."""
            label, diag = self._choose_card(els, name, ASSIGNABLE, hour_y(els),
                                            CONSUMER_NAME, hour_lbl)
            self._last_card_diag = diag
            if label and diag:
                notes.append(f"    · {diag}")
            return label

        def appium_click(label: str) -> bool:
            """Click the card by exact label — WDA auto-scrolls it into view. Bounded so a
            slow WDA snapshot on this big tree can't hang the run."""
            safe = label.replace('"', '')
            def _do():
                els = r.d.find_elements(AppiumBy.IOS_PREDICATE, f'label == "{safe}"')
                if not els:
                    # The label came from an idb snapshot taken moments ago. The board
                    # re-renders on its own (it polls the backend), so a card that was
                    # there can be mid-rerender for one WDA query and back immediately
                    # after -- measured: this reported "no element matched that exact
                    # label" for a card idb had just listed, and a query seconds later
                    # found sixteen. One short retry turns that transient into a
                    # non-event instead of failing the whole segment.
                    time.sleep(1.5)
                    els = r.d.find_elements(AppiumBy.IOS_PREDICATE, f'label == "{safe}"')
                if not els:
                    return "no element matched that exact label"
                # WHICH of them. The board shows 8 cards with this SAME label laid out
                # sideways (x=208 ... x=4144), and the events sidebar adds its own copy
                # of each. Taking [0] always picked the board's leftmost card -- a
                # different booking from the one chosen, and off in a row the sidebar is
                # covering, so the tap changed nothing and the step sat in "no
                # meaningful UI change" for 240s.
                # When the sidebar is up it is the authority: it lists exactly this
                # hour's bookings, vertically, so prefer the element inside it.
                els[0].click()
                return ""
            # Report WHY, not just that it failed. "(WDA click)" cannot tell a 30s
            # timeout from a stale element from a label that no longer resolves, and
            # those need different fixes -- the first is a wedged WDA, the second a
            # board that re-rendered under us, the third a status suffix that moved.
            ex = _fut.ThreadPoolExecutor(max_workers=1)
            try:
                why = ex.submit(_do).result(timeout=30)
                self._last_click_error = why
                return not why
            except _fut.TimeoutError:
                self._last_click_error = ("WDA did not answer within 30s "
                                          "(wedged resolve on this tree)")
                return False
            except Exception as e:
                self._last_click_error = f"{type(e).__name__}: {str(e)[:90]}"
                return False
            finally:
                ex.shutdown(wait=False)

        # Find the diner's assignable card. The card's PRESENCE is the real "loaded" signal —
        # the previous version gated on a separate "bookings_loaded" check and relaunched the app
        # 3x, which FALSE-NEGATIVED (relaunch reset the screen mid-load) even though the RESERVED
        # card was right there. So: (re)select today's date to trigger the fetch, then poll for
        # the card itself; only relaunch ONCE, and only if nothing shows.
        today = f"{_dt.now().strftime('%a').upper()} {_dt.now().day}"   # e.g. 'THU 6'
        if hour_lbl:
            notes.append(f"    · booked slot '{slot}' → target {hour_lbl} row")

        def _select_today_and_find():
            # There is deliberately NO date tap here any more.
            #
            # MEASURED on this build: tapping the date cell OPENS the 'Select A
            # Table' sheet. An idb tap on 'THU 20' at (137,185) — the only element
            # at that point — put the sheet up in 0.9s and took the board's 42
            # cards with it. That is what made @open_reservation and @open_order
            # report "0 card element(s) seen ... on screen: ['applyTableBtn']" on
            # runs 3a040e54, 11f294ec and a7ce6034.
            #
            # The tap only ever existed to force a refetch, and it is not needed:
            # measured over 75s of pure observation after a launch, the board
            # renders its cards on its own (42 cards by t=18.8s) with no input.
            # So just wait for them — and clear the sheet if it turns up anyway,
            # because while it is open the board is not readable at all.
            scrolled = False
            # HARD DEADLINE. Every helper below is individually bounded, but the loop
            # around them was not: 14 iterations x (idb scan + scroll + badge wait +
            # sidebar poll) can exceed the 240s step ceiling on its own, and then the
            # segment watchdog kills the step with "step hung" -- which names the
            # symptom and throws away every note explaining what was actually tried.
            # Stop early and keep the diagnosis.
            _deadline = time.time() + 0.55 * STEP_TIMEOUT
            for _ in range(14):                     # ~35s for the diner's card to appear/sync
                if time.time() > _deadline:
                    notes.append(f"    · giving up the card search after "
                                 f"{0.55 * STEP_TIMEOUT:.0f}s so the step can report "
                                 f"why, rather than being killed as 'hung'")
                    return None
                if self._table_modal_up():
                    notes.append("    · table sheet was covering the board — dismissing it")
                    self._dismiss_table_modal(r, notes)
                # Bring the booked hour into view before choosing a card. Done once the
                # board has actually rendered (scrolling an empty list does nothing) and
                # only once, so a re-poll does not walk the calendar away from the row.
                els_now = self._idb_els()
                if not scrolled and hour_lbl and bookings_loaded(els_now):
                    # LATCH ON SUCCESS, NOT ON ATTEMPT.
                    #
                    # The table sheet can render a beat AFTER this loop starts, so
                    # iteration 1 sees a clean board, scrolls against it, and then
                    # the sheet covers everything -- the scroll silently achieves
                    # nothing. Latching unconditionally meant the one wasted attempt
                    # was the only attempt: the run reported "could not bring the
                    # 20:00 row into view" and fell back to the horizontal search
                    # even though 20:00 was reachable (measured: y=518 on an 834pt
                    # screen, comfortably inside the band).
                    #
                    # The give-up path is the deadline above, not a single try.
                    if scroll_to_hour(els_now):
                        scrolled = True
                        notes.append(f"    · scrolled the calendar to the {hour_lbl} row")
                    else:
                        notes.append(f"    · could not bring the {hour_lbl} row into view "
                                     f"— retrying (the table sheet can cover the board)")
                        continue
                    # Then open that hour's events list. An hour's bookings are laid
                    # out sideways, so the list is the only way to reach the 4th or
                    # 5th one without swiping across the row.
                    if open_events_for_hour(self._idb_els()):
                        # The list is open and every row states its own booking
                        # window, so open the one that matches the booked slot
                        # directly. This is the only place the right booking can be
                        # NAMED: on the board all nine 13:00 bookings render the
                        # same 'RoopaDcardReserved' label, so picking among them is
                        # a guess -- which is how a run clicked the leftmost card
                        # and then sat in "no meaningful UI change" for 240s.
                        if self._click_sidebar_row(slot, statuses, notes):
                            return _OPENED_VIA_SIDEBAR
                        # The list is open and does NOT hold the booking. Falling
                        # through to the board would pick among cards that all read
                        # 'RoopaDcardReserved' and open whichever came first -- a
                        # guess, and the reason a run opened someone else's booking
                        # and then hung. Say what the list actually offered instead.
                        offered = [(e.get("label") or "").strip()
                                   for e in self._idb_els()
                                   if _SIDEBAR_ROW_RE.search((e.get("label") or ""))]
                        if offered:
                            notes.append(f"    · no {slot} booking in the "
                                         f"{hour_lbl} events list; it holds: "
                                         + "; ".join(o[:44] for o in offered[:8]))
                lbl = pick_label(self._idb_els())
                if lbl:
                    return lbl
                time.sleep(2.5)
            return None

        label = _select_today_and_find()
        # The sidebar row was tapped directly: the reservation is already opening, so
        # there is no board card to locate or click. Skip straight to verification.
        if label is _OPENED_VIA_SIDEBAR:
            # Verify against markers that exist ONLY on the reservation, not on the
            # sidebar. 'closeEventModal' is deliberately excluded: it is the events
            # list's OWN close button, so treating it as "opened" would report
            # success the instant the list appeared.
            SIDEBAR_SAFE = ("selectAllItemsBtn", "addItemsBtn", "assignToBtn",
                            "sendToKitchenBtn", "AssignTableBtn", "closeModal")
            for _ in range(12):
                time.sleep(1.5)
                seen = {e["id"] for e in self._idb_els()} | \
                       {e["label"] for e in self._idb_els()}
                if any(m in seen for m in SIDEBAR_SAFE) or self._table_modal_up():
                    notes.append(f"    · {what} — opened the {slot} booking from the "
                                 f"events list")
                    return True
            notes.append(f"    · {what} — tapped the {slot} row in the events list but "
                         f"the reservation did not open; falling back to the board")
            label = None
        if not label:
            notes.append("    · card not found — relaunching business app once and retrying")
            try:
                r.d.terminate_app(bundle); time.sleep(1.5)
                r.d.activate_app(bundle); time.sleep(8)
            except Exception as e:
                notes.append(f"    · relaunch note: {type(e).__name__}")
            label = _select_today_and_find()
        if not label:
            # Say WHY nothing matched, not just that nothing did: a card can be on screen and
            # still be rejected (wrong status, or w<=60 when it sits outside the horizontally
            # scrolled viewport). Without this the failure note and the '↳ on screen' dump
            # contradict each other and the real cause can't be told apart from a guess.
            seen = [(e["label"].strip() or e["id"].strip(), e["w"]) for e in self._idb_els()
                    if "card" in (e["label"] + e["id"]).lower()]
            narrow = [f"{l}(w={w:g})" for l, w in seen if w <= 60]
            notes.append(f"[FAIL] {what} — no {'/'.join(statuses)} card "
                         f"for diner '{CONSUMER_NAME}' on {today} near {hour_lbl or '?'} "
                         f"(already assigned, or backend not synced)")
            # WHICH cards were rejected and why — a status that moved on and an
            # ambiguous pick look identical in the old note, and they need
            # different fixes.
            if getattr(self, "_last_card_diag", ""):
                notes.append(f"    ↳ [RESERVATION] {self._last_card_diag}")
            notes.append(f"    ↳ {len(seen)} card element(s) seen; "
                         f"rejected-for-width: {narrow or 'none'}; "
                         f"labels: {sorted({l for l, _ in seen})}")
            return False
        # The click landing is NOT the reservation opening. Measured on run
        # 3a040e54: this reported "[ok] opened 'RoopaDcardReserved'", and the
        # screen at the next failure was still the bookings board — every later
        # step then ran against the board and 'click selectAllItemsBtn' burned its
        # full 150s. Confirm the Order Summary is actually up, and retry the open
        # once before believing it.
        #
        # Markers are the controls the following steps depend on; the table sheet
        # counts too, because a reservation with a room opens straight onto it.
        # Markers that the reservation actually opened. The phone lands straight on
        # its assign-a-table screen ('Please assign a Table' + T<N>AssignAnyBtn +
        # AssignTableBtn + closeModal), which none of the iPad markers cover — so
        # a correctly-opened reservation was being reported as never opening.
        OPENED = ("selectAllItemsBtn", "addItemsBtn", "assignToBtn",
                  "closeEventModal", "sendToKitchenBtn",
                  "AssignTableBtn", "closeModal")
        # ...but 'addItemsBtn' ALSO sits on the ADD NEW ITEM sheet, which is a
        # different screen that happens to share the marker. A run that mis-tapped
        # its way onto that sheet therefore satisfied opened() and every later step
        # ran against the sheet. These ids exist ONLY on the sheet, so their presence
        # means the order summary is NOT what is in front of us.
        NOT_ORDER_SCREEN = ("addNewItemClose", "addNewItemInput", "addNewItemAll")

        def opened() -> bool:
            els = self._idb_els()
            seen = {e["id"] for e in els} | {e["label"] for e in els}
            if any(m in seen for m in NOT_ORDER_SCREEN):
                return False
            return any(m in seen for m in OPENED) or self._table_modal_up()

        for attempt in (1, 2):
            # The card is usually BELOW THE FOLD (the timeline runs 00:00-23:00),
            # and a click on an off-screen element does nothing. Scroll first.
            scrolled = self._scroll_into_view(r, label)
            if not scrolled:
                notes.append(f"    · {what} — could not scroll '{label[:36]}' into view; "
                             f"clicking anyway")
            # ...and the same card can be outside the row's HORIZONTAL scroll, which the
            # vertical helper above cannot reach. Additive: a target already in the
            # viewport returns immediately, and a failure here only annotates — the
            # click still runs and reports its own outcome.
            # LAST RESORT only. Opening the hour's events list (above) lays that
            # hour's bookings out vertically, so the target is normally already
            # reachable and swiping the row sideways is both unnecessary and the
            # slowest thing this step can do. Check first, and only swipe if the card
            # really is still outside the viewport.
            _cards = self._cards_on_board()
            _tgt = self._find_target_card(_cards, label, name)
            try:
                _vw = float(r.d.get_window_size()["width"])
            except Exception:
                _vw = 0.0
            _reachable = bool(_tgt) and (not _vw or self._target_in_viewport(_tgt, _vw))
            # NEVER swipe sideways while the hour's events list is open. The list is
            # a VERTICAL sidebar, so a horizontal swipe on it reaches nothing; worse,
            # the swipe lands on the board BEHIND it and scrolls the wrong surface.
            # Measured: the run that reported "scroll 1/2/3 — new visible cards"
            # identical three times was swiping a row that the sidebar was covering,
            # then clicked anyway 220s later. Appium's own .click() auto-scrolls the
            # sidebar vertically, which is all that is needed here.
            _sidebar = self._events_sidebar_up(self._idb_els())
            if _sidebar:
                notes.append(f"    · {what} — events list is open; selecting from it "
                             f"vertically (no horizontal scrolling)")
            elif not _reachable and not self._scroll_card_into_view_h(
                    r, label, notes, diner_key=name, slot=hour_lbl or slot):
                notes.append(f"    · {what} — '{label[:36]}' could not be brought into the "
                             f"viewport horizontally; clicking anyway")
            if not appium_click(label):
                # A WEDGED WDA is not a missing card. The card search above already
                # reported this one at a known x ('RoopaDcardReserved@x=208') — idb
                # sees it and can tap it by coordinate without WDA answering at all.
                # Without this, a 30s WDA stall failed the whole segment while the
                # card sat in plain view, and the kitchen/serve segments after it
                # then ran against the wrong screen.
                _err = getattr(self, "_last_click_error", "") or "WDA click failed"
                _hit = next((e for e in self._idb_els()
                             if e["label"].strip() == label and e["w"] > 0 and e["h"] > 0),
                            None)
                if _hit and not self._occluding(_hit, self._idb_els()):
                    self._idb_tap(_hit["cx"], _hit["cy"])
                    time.sleep(0.8)
                    notes.append(f"    [card search] WDA failed ({_err}); tapped "
                                 f"{label[:40]!r} by idb coordinate instead")
                else:
                    notes.append(f"[FAIL] {what} — found '{label[:40]}' but could not open it: "
                                 f"{_err}")
                    return False
            else:
                notes.append(f"    [card search] clicked target {label[:40]!r}")
            for _ in range(6):                    # ~9s for the summary to render
                time.sleep(1.5)
                if opened():
                    # The click landing is NOT the reservation opening — opened() is what
                    # proves it, by the controls the following steps depend on.
                    notes.append("    [card search] reservation opened successfully")
                    notes.append(f"[ok] {what} — opened '{label[:40]}' (auto-scrolled) at "
                                 f"slot '{slot or '?'}'"
                                 + ("" if attempt == 1 else f" (attempt {attempt})"))
                    return True
            if attempt == 1:
                notes.append(f"    · {what} — clicked '{label[:40]}' but the reservation did "
                             f"not open; retrying once")

        els = self._idb_els()
        onscreen = [e["id"] or e["label"] for e in els if (e["id"] or e["label"])][:16]
        notes.append(f"[FAIL] {what} — clicked '{label[:40]}' twice and the reservation never "
                     f"opened (no {'/'.join(OPENED[:3])} and no table sheet). Still on: "
                     f"{onscreen}")
        return False

    def _wait_form(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Wait for the create-booking form to actually RENDER before typing into it.
        The form opens a beat after 'addNewEvent' (debug build), so the very next
        'type roopa in firstName' can fire against a screen that has no firstName field
        yet -> 'No input matches'. Poll idb (fast) for any form field to appear, up to
        ~20s. Non-fatal: if it never shows, continue and let the step self-heal."""
        FORM_IDS = ("firstName", "lastName", "mobileInputBtn", "anyBtn", "saveBtn")

        def form_open():
            els = self._idb_els()
            ids = {e["id"] for e in els} | {e["label"] for e in els}
            return any(fid in ids for fid in FORM_IDS)

        for i in range(10):                       # ~20s (2s idb read + settle each loop)
            if form_open():
                notes.append(f"[ok] @wait_form — form rendered ({i * 2}s)")
                time.sleep(0.4)                   # tiny settle for the first frame
                return True
            time.sleep(2)

        # The form is NOT open. This used to return True and say "step may
        # self-heal", so every later step ran against the booking board and the
        # segment died on 'type roopa in firstName' after 150s with no clue why.
        # A tap completing is not the same thing as the form opening — verify the
        # state transition, retry the opening tap ONCE through Appium (an element
        # click, which beat this on the iPad where a coordinate tap did not), and
        # otherwise fail with the real reason plus what is actually on screen.
        notes.append("[warn] @wait_form — form did not open; retrying addNewEvent via Appium")
        try:
            for el in r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "addNewEvent"):
                try:
                    el.click()
                except Exception:
                    continue
                for _ in range(6):                # ~12s for the form to render
                    time.sleep(2)
                    if form_open():
                        notes.append("[ok] @wait_form — form rendered after Appium retry")
                        return True
                break
        except Exception as e:
            notes.append(f"    · @wait_form — Appium retry errored: {type(e).__name__}")

        els = self._idb_els()
        onscreen = [e["id"] or e["label"] for e in els if (e["id"] or e["label"])][:18]
        over = next((e for e in els if e["label"].strip() == "addNewEvent"), None)
        blocker = self._occluding(over, els) if over else None
        notes.append(
            "[FAIL] @wait_form — the New Appointment form never opened. "
            + (f"addNewEvent is covered by {blocker['label'][:40]!r}. " if blocker else "")
            + f"On screen: {onscreen}")
        return False

    # ── consumer: accept a waiter-created appointment ───────────────────────
    # Read off the Consumer app, not guessed:
    #   App/Screens/Wallet/Upcoming.js:810  a business-created invite renders with
    #       cardLabel = el.eventFromBusiness ? `${el.restaurant.name}InviteCard`
    #                                        : `${el.user_id.username}InviteCard`
    #     and its onPress does setState({idVal: el, invVis: true}) — i.e. the card
    #     OPENS the invitation modal, it does not accept anything by itself.
    #   App/Components/Modal/index.js:9332  InvitaionScreenModal's ACCEPT button is
    #       accessibilityLabel="eventAccept"  (DECLINE is "eventDecline",
    #     the ✕ is "inviteclose"); the pair only renders while !btnAcceptStatus.
    #   App/Screens/Wallet/Upcoming.js:218   accept() POSTs
    #       /appointments/api/acceptInvitation, sets status 'Reserved' and invVis:false.
    #   App/Screens/Wallet/index.js:463      dynamicUpdatingAppointments(id, …, 'invite')
    #     then MOVES the row: out of `invitation` (so the InviteCard disappears) and
    #     into `upcoming` as '<Restaurant>Card', toasting 'Appointment updated
    #     successfully'. THAT move is the acceptance; a tap that merely lands proves
    #     nothing, so this step verifies the move and fails loudly if it never happens.
    _INVITE_SUFFIX = "InviteCard"
    _ACCEPT_ID = "eventAccept"
    _ACCEPT_TOAST = "Appointment updated successfully"
    # For a "1 hr" slot accept() also raises the booking-confirmed modal
    # (AcceptInvitaionOrder → preOrderBooking / orderLater). Flows 5/6 have the WAITER
    # add the items afterwards, so take orderLater — the same choice flow 3/4 make.
    _POST_ACCEPT_IDS = ("orderLater", "preOrderBooking", "appointmentId")

    def _accept_appointment(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Open the pending wallet invitation and ACCEPT it, verifying the transition.

        Previously there was no handler for this step at all: it fell through to the
        fuzzy resolver, which hunted a non-existent element for the full STEP_TIMEOUT
        (150s) and then reported a generic miss. That is a MISSING IMPLEMENTATION, not
        a wallet loading problem — @wait_screen:wallet has already proved the screen
        is up by the time this runs.
        """
        def labels() -> List[str]:
            return [e["label"] for e in self._idb_els() if e["label"]]

        def invites(ls: List[str]) -> List[str]:
            return [l for l in ls if l.endswith(self._INVITE_SUFFIX)]

        self._clear_logbox()
        before = labels()
        cards = invites(before)
        if not cards:
            # Nothing pending. Distinguish "already accepted" from "never arrived":
            # an accepted invite is in the upcoming list as '<Restaurant>Card'.
            notes.append(
                "[FAIL] accept appointment — no pending appointment on the wallet "
                f"(no '<name>{self._INVITE_SUFFIX}'). The waiter-created appointment "
                f"never reached this account, or it was already accepted. "
                f"On screen: {before[:18]}")
            return False
        card = cards[0]

        # 1. Open the invitation modal. The card is the only way in — its onPress sets
        #    invVis. Retry the tap: a LogBox toast can eat it, exactly as it does for
        #    BOOK NOW in @book_appointment.
        opened = False
        for attempt in range(1, 4):
            el = next((e for e in self._idb_els() if e["label"] == card), None)
            if el is None:
                break
            self._idb_tap(el["cx"], el["cy"])
            for _ in range(5):                       # ~7.5s for the modal to render
                time.sleep(1.5)
                if self._ACCEPT_ID in labels():
                    opened = True
                    break
            if opened:
                if attempt > 1:
                    notes.append(f"    · accept appointment — modal opened on attempt {attempt}")
                break
            self._clear_logbox()
        if not opened:
            notes.append(
                f"[FAIL] accept appointment — tapped the pending appointment {card!r} but the "
                f"invitation modal never opened (no {self._ACCEPT_ID!r} on screen). "
                f"On screen: {labels()[:18]}")
            return False

        # 2. Accept, then VERIFY the state actually moved. A landed tap is not acceptance.
        btn = next((e for e in self._idb_els() if e["label"] == self._ACCEPT_ID), None)
        if btn is None:
            notes.append(f"[FAIL] accept appointment — {self._ACCEPT_ID!r} vanished before it "
                         f"could be tapped. On screen: {labels()[:18]}")
            return False
        self._idb_tap(btn["cx"], btn["cy"])

        accepted = False
        post_modal = False
        for _ in range(12):                          # ~24s: this is a network round-trip
            time.sleep(2)
            now = labels()
            if any(i in now for i in self._POST_ACCEPT_IDS):
                post_modal = accepted = True
                break
            if self._ACCEPT_ID not in now and (card not in now or self._ACCEPT_TOAST in now):
                accepted = True
                break
        if not accepted:
            notes.append(
                "[FAIL] accept appointment — pending appointment found, but acceptance action "
                f"did not transition to the expected state: {self._ACCEPT_ID!r} is still on "
                f"screen and {card!r} never left the invitation list "
                f"(/appointments/api/acceptInvitation did not land). "
                f"On screen: {labels()[:18]}")
            return False

        # 3. The booking-confirmed modal, when the slot is "1 hr". The waiter adds the
        #    items in the next segment, so decline the pre-order and carry on.
        if post_modal:
            ok, note, _ = self._smart_click(r, "click orderLater")
            notes.append(f"[{'ok' if ok else 'warn'}] accept appointment — booking-confirmed "
                         f"modal: orderLater — {note}")
            time.sleep(2)

        # 4. Final proof: the invitation is gone from the pending list.
        self._clear_logbox()
        after = labels()
        if card in after:
            notes.append(
                "[FAIL] accept appointment — pending appointment found, but acceptance action "
                f"did not transition to the expected state: {card!r} is STILL in the pending "
                f"list after ACCEPT. On screen: {after[:18]}")
            return False
        notes.append(f"[ok] accept appointment — {card!r} accepted; it left the pending list"
                     + (" (booking-confirmed modal dismissed via orderLater)" if post_modal else ""))
        return True

    def _handle_special(self, r, step: str, notes: List[str]) -> bool:
        if step == "@wait_form":
            return self._wait_form(r, notes)
        if step == "@got_it":
            return self._got_it(r, notes)
        if step == "@save_appointment":
            return self._save_appointment(r, notes)
        if step == "@open_reservation":
            return self._open_reservation(r, notes)
        if step == "@open_order":
            # Serve/settle segments run AFTER the kitchen role-switch, which drops the iPad
            # back on the bookings board — the order screen they assume is open is not. Re-open
            # the diner's now-InProgress ticket first (same machinery, later status).
            return self._open_reservation(r, notes, statuses=("inprogress",), what="@open_order")
        if step == "@to_checkout":
            return self._to_checkout(r, notes)
        if step == "@pay_stripe":
            return self._pay_stripe(r, notes)
        if step == "@consumer_home":
            return self._consumer_home(r, notes)
        if step == "@assign_table":
            return self._assign_table(r, notes)
        if step == "@ensure_order_items":
            return self._ensure_order_items(r, notes)
        if step == "@kitchen_ready":
            return self._kitchen_ready(r, notes)
        if step == "@hide_keyboard":
            return self._hide_keyboard(r, notes)
        if step == "@book_appointment":
            return self._book_appointment(r, notes)
        if step == "@first_time_slot":
            return self._first_time_slot(r, notes)
        if step == "@add_all_products":
            return self._add_all_products(r, notes)
        if step == "@logout_business":
            self._logout_business(r); return True
        if step == "@logout_consumer":
            # Confirmed IDs: menuTab opens the drawer; menuLogout signs out.
            if r._resolve(["menuTab"]):
                r.run_one("click menuTab", 0); time.sleep(1.0)
            if r._resolve(["menuLogout"]):
                return r.run_one("click menuLogout", 0).ok
            return self._tap_text_contains(r, "logout")
        if step == "@accept_appointment":
            return self._accept_appointment(r, notes)
        if step.startswith("@wait_screen:"):
            return self._await_screen(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@pay:"):
            return self._pay(r, step.split(":", 1)[1], notes)
        notes.append(f"[FAIL] unknown token {step}")
        return False

    # -- per-segment execution ----------------------------------------------
    # The SAME control carries different accessibility ids in the tablet and phone
    # builds of the Business app. Measured in App/Screens/Event/OrderSummary.js vs
    # App/MobileScreens/Event/OrderSummary.js:
    #     select all   tablet 'selectAll'      phone 'selectAllItemsBtn'
    #     unselect     tablet 'unSelectAll'    phone 'unSelectItemsBtn'
    # The flows hardcode the PHONE spelling, so on the iPad 'click selectAllItemsBtn'
    # hunted for a control that build does not contain and burned its whole 150s
    # timeout every run — that is what killed segment 2 on the tablet. Treat the two
    # spellings as one id and try whichever the running build actually has.
    # Plain-English steps that have a DEDICATED handler. Without this they fall
    # through to _smart_click's fuzzy resolver, which hunts an element that does not
    # exist for the full STEP_TIMEOUT (150s) and then reports a generic miss. Kept as
    # an ALIAS rather than rewriting the flow blocks because flows edited in the
    # dashboard are stored in the DB with this plain-English wording.
    _PLAIN_STEP_TOKENS = {"accept the appointment": "@accept_appointment"}

    # ONE map, shared with ScenarioRunner (which the Scenarios page runs through) so a
    # tablet/phone id pair fixed in one runner cannot stay broken in the other.
    _ID_ALIASES = ScenarioRunner.ID_ALIASES

    @classmethod
    def _id_candidates(cls, ident: str):
        """The id as written, then any known equivalent in the other build."""
        return ScenarioRunner.id_candidates(ident)

    def _time_slot_committed(self, r: ScenarioRunner, lbl: str) -> bool:
        """Did tapping the time chip actually set selectedTime?

        The chip shows its selection ONLY as a background colour
        (selectedTime === el ? '#d6d6d6' : '#eee', AddNewEventModal/index.js:1187),
        and accessibility does not expose that -- measured: `selected` stays
        "false" and the idb tree is byte-identical before and after a successful
        selection. So the selection itself is not directly observable.

        What IS observable is the chip's POSITION. The form's content is taller than
        the sheet, so the slot row renders BELOW saveBtn and is clipped: all three
        matches report displayed=False and an idb coordinate tap there hits nothing.
        Appium's .click() auto-scrolls the chip into the sheet first -- measured
        17:35Btn moving y=793 -> y=645, above saveBtn at y=710 -- and that scroll is
        the proof the click reached a real, hit-testable element rather than a
        clipped one.

        So: the slot counts as committed once its chip is genuinely on screen and
        above the save control.
        """
        try:
            els = r.d.find_elements(AppiumBy.IOS_PREDICATE, f'label == "{lbl}Btn"')
        except Exception:
            return True          # cannot tell -- do not fail a booking on that
        if not els:
            return True
        save_y = None
        for e in self._idb_els():
            if (e.get("label") or "").strip() == "saveBtn":
                save_y = e.get("cy")
                break
        for e in els:
            try:
                if not e.is_displayed():
                    continue
                rect = e.rect or {}
            except Exception:
                continue
            cy = (rect.get("y") or 0) + (rect.get("height") or 0) / 2
            if save_y is None or cy <= save_y:
                return True
        return False

    def _dismiss_logbox_viewer(self, udid: str = "") -> bool:
        """Close the EXPANDED LogBox (the full-screen stack-trace view).

        Different shape from the collapsed toast: the toast is a full-width 48pt
        strip dismissed by a ✕ at its right edge, while the viewer covers the whole
        screen and carries a Dismiss/Minimize pair along the bottom (measured on the
        phone: Dismiss (0,804,197,48), Minimize (197,804,196,48)). _logbox_strip
        only matches the strip, so the viewer slipped past it — and while it is up
        nothing else on the screen is reachable, which is how 'click sendItemsBtn'
        burned its full 150s timeout.
        """
        els = self._idb_els(udid)
        labels = {e["label"].strip() for e in els}
        if not ({"Dismiss", "Minimize"} <= labels):
            return False                       # the pair identifies the viewer
        # MINIMIZE, not Dismiss. Measured: this debug build stacks ~28 logs and
        # Dismiss closes only the CURRENT one — the header counts down
        # "Log 8 of 29" -> "8 of 28" and the viewer still covers the screen, so
        # clearing it that way would take 28 taps. Minimize collapses the whole
        # viewer to its bottom toast in one tap (n=56 -> 15 elements) and the
        # screen underneath becomes reachable again.
        btn = next((e for e in els if e["label"].strip() == "Minimize"), None)
        if not btn:
            return False
        self._idb_tap(btn["cx"], btn["cy"], udid)
        time.sleep(1.2)
        return not ({"Dismiss", "Minimize"} <= {e["label"].strip() for e in self._idb_els(udid)})

    # The ADD NEW ITEM sheet, by the ids that exist ONLY while it is up.
    _ADD_ITEM_SHEET = ("addNewItemClose", "addNewItemInput", "addNewItemAll")

    def _dismiss_add_item_sheet(self, udid: str = "") -> bool:
        """Close a stray ADD NEW ITEM sheet. Returns True only if it WAS up and is now gone.

        This sheet is modal: while it is open nothing behind it is reachable, so a step
        that opened it by accident (a fuzzy heal of serveItemsBtn -> addItemsBtn did
        exactly that) does not merely fail itself — it takes every following step in the
        segment down with it, each burning the full STEP_TIMEOUT. Closing it puts the
        order summary back in front and lets the segment carry on.
        """
        els = self._idb_els(udid)
        ids = {e["id"] for e in els} | {e["label"].strip() for e in els}
        if not any(m in ids for m in self._ADD_ITEM_SHEET):
            return False
        btn = next((e for e in els
                    if e["id"] == "addNewItemClose"
                    or e["label"].strip() == "addNewItemClose"), None)
        if not btn:
            return False
        self._idb_tap(btn["cx"], btn["cy"], udid)
        time.sleep(1.2)
        after = self._idb_els(udid)
        ids_after = {e["id"] for e in after} | {e["label"].strip() for e in after}
        return not any(m in ids_after for m in self._ADD_ITEM_SHEET)

    def _smart_click(self, r: ScenarioRunner, step: str):
        """For a 'click <id>' step, tap the element DIRECTLY by accessibility id —
        fast and reliable on this app's huge tree (the fuzzy resolver + depth-capped
        snapshot misses deep cards like NylaiKitchen2). Falls back to the fuzzy
        resolver for plain-English steps or when the id isn't found directly.
        Returns (ok, short_note, flaky) — flaky=True when it only passed on a retry."""
        m = re.match(r'^\s*click\s+([A-Za-z][\w]*)\s*$', step)
        ident = m.group(1) if m else None
        if ident is None:
            # A step written the way a PERSON says it — "click order later" — names the
            # id `orderLater`, but the single-word regex above rejects anything with a
            # space. It then fell through to the fuzzy resolver, which is exactly what
            # the idb allow-list below exists to AVOID on this modal: the run hung the
            # full 150s on a screen where ORDER LATER was plainly visible.
            # Camel-case the words and let it use the same fast paths as the exact id.
            mw = re.match(r'^\s*click\s+([A-Za-z][A-Za-z0-9 ]*?)\s*$', step)
            if mw:
                parts = mw.group(1).split()
                if len(parts) > 1:
                    ident = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
        if ident:
            # idb FAST-PATH — ONLY for allow-listed ids (clean on-screen modal buttons whose
            # Appium full-tree snapshot hangs; e.g. preOrderBooking hung a run ~30 min). Not
            # used for anything else: it's too blunt for cards (it once matched a same-named
            # text label and tapped the WRONG restaurant). idb dumps in ~1-2s; tap the centre.
            if ident in _IDB_CLICK_IDS:
                # These buttons appear only AFTER a backend round-trip — preOrderBooking shows
                # once bookAppoitment's booking is CONFIRMED by the server. A single check right
                # after booking usually misses it (not rendered yet), so POLL ~18s for it to
                # appear, then tap by coordinate. (idb dumps in ~1-2s.)
                for _attempt in range(12):
                    try:
                        els = self._idb_els()
                        sw = max((e["w"] for e in els), default=1600)   # Application frame == screen
                        sh = max((e["h"] for e in els), default=1600)
                        for e in els:
                            if (e["id"] == ident or e["label"].strip() == ident) \
                                    and e["w"] > 0 and e["h"] > 0 \
                                    and 0 <= e["cx"] <= sw and 0 <= e["cy"] <= sh:
                                self._idb_tap(e["cx"], e["cy"]); time.sleep(0.8)
                                return True, f"tapped {ident} (idb, {_attempt + 1} try)", False
                    except Exception:
                        pass
                    time.sleep(1.5)
                # never rendered — fall through to Appium (bounded by the per-step timeout)
            # 1) Appium by EXACT accessibility id. Do NOT gate on is_displayed() — React Native
            #    elements routinely report is_displayed()==False while being perfectly tappable,
            #    so gating here SKIPPED real buttons (e.g. anyBtn) and let the fuzzy resolver
            #    mis-heal to a similarly-named one (anyBtn -> allBtn). Just click; WDA scrolls it
            #    into view and raises if it's genuinely not there (then we fall through).
            for cand in self._id_candidates(ident):
                try:
                    found_any = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cand)
                except Exception:
                    continue
                for e in found_any:
                    try:
                        e.click(); time.sleep(0.6)
                        note = (f"tapped {cand} by id" if cand == ident
                                else f"tapped {cand} by id (this build's name for {ident})")
                        return True, note, False
                    except Exception:
                        continue
            # 2) idb EXACT-id coordinate tap (on-screen). Catches elements Appium's depth-capped
            #    snapshot misses, and taps the RIGHT element by its real id — never a fuzzy
            #    near-match. Skips off-screen ids (Appium above already auto-scrolls to those).
            try:
                els = self._idb_els()
                sw = max((e["w"] for e in els), default=1600)
                sh = max((e["h"] for e in els), default=1600)
                cands = self._id_candidates(ident)
                for e in els:
                    if (e["id"] in cands or e["label"].strip() in cands) \
                            and e["w"] > 0 and e["h"] > 0 \
                            and 0 <= e["cx"] <= sw and 0 <= e["cy"] <= sh:
                        # A coordinate tap hits whatever is TOPMOST at that pixel,
                        # so check the target is actually exposed before firing.
                        # MEASURED: addNewEvent sits at (700,698,50,50) on the iPad
                        # under a full-width LogBox toast at (10,708,1190,48). The
                        # tap landed on the toast, LogBox expanded, and this branch
                        # still returned True — "tap dispatched" reported as "tap
                        # worked". The New Appointment form never opened and the
                        # next step burned its whole 150s timeout.
                        over = self._occluding(e, els)
                        if over:
                            self._clear_logbox(udid)
                            els = self._idb_els()
                            e = next((x for x in els
                                      if x["id"] in cands or x["label"].strip() in cands), None)
                            over = self._occluding(e, els) if e else None
                            if e is None or over:
                                what = (over or {}).get("label", "an overlay")
                                return (False,
                                        f"{ident} is covered by {what[:40]!r} — refusing a blind "
                                        f"coordinate tap (it would hit the overlay, not {ident})",
                                        False)
                        self._idb_tap(e["cx"], e["cy"]); time.sleep(0.6)
                        return True, f"tapped {ident} (idb exact-id)", False
            except Exception:
                pass
            # Both id paths missed. The EXPANDED LogBox may be covering the screen —
            # it hides every other element, so the id looks absent when it is merely
            # obscured. Clear it and try the same ids once more before falling
            # through to the (slow, fuzzy) resolver.
            if self._dismiss_logbox_viewer():
                for cand in self._id_candidates(ident):
                    try:
                        again = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cand)
                    except Exception:
                        continue
                    for e in again:
                        try:
                            e.click(); time.sleep(0.6)
                            return True, f"tapped {cand} by id (after clearing LogBox)", False
                        except Exception:
                            continue
            # ...or a stray ADD NEW ITEM sheet is covering the order summary. Same
            # shape of problem as the LogBox viewer: the id is not missing, it is
            # behind a modal. Close it and try the real id once more, BEFORE handing
            # the step to the fuzzy resolver — healing against a modal's contents is
            # what opened this sheet in the first place.
            if self._dismiss_add_item_sheet():
                for cand in self._id_candidates(ident):
                    try:
                        again = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cand)
                    except Exception:
                        continue
                    for e in again:
                        try:
                            e.click(); time.sleep(0.6)
                            return True, (f"tapped {cand} by id (after closing the "
                                          f"ADD NEW ITEM sheet)"), False
                        except Exception:
                            continue

        # Fast-path for 'type <val> in <fieldId>': type directly by accessibility id (the same
        # mechanism login uses) instead of the fuzzy resolver + its retry/backoff, which is slow
        # on this huge tree. Falls through to the resolver if the id isn't found this way.
        mt = re.match(r'^\s*type\s+(.+?)\s+in\s+([A-Za-z][\w]*)\s*$', step)
        if mt:
            _val, _field = mt.group(1), mt.group(2)
            try:
                if _fill_field(r.d, _field, _val):
                    time.sleep(0.3)
                    return True, f'typed "{_val}" into "{_field}" (direct id)', False
            except Exception:
                pass
        res = r.run_one(step, 0)
        flaky = False
        # Retry-with-backoff — a step that fails once then passes is FLAKY, not a
        # clean pass and not a fail. Surfacing that is how a suite earns trust.
        for delay in (1.5, 2.5):
            if res.ok:
                break
            time.sleep(delay)
            res = r.run_one(step, 0)
            if res.ok:
                flaky = True
        raw = res.action if res.ok else (res.detail or res.action or "failed")
        return res.ok, (str(raw).splitlines()[0] if raw else "")[:140], flaky

    def _run_segment(self, seg: Dict[str, Any]) -> bool:
        """Run one role's segment. Returns True if it PASSed, False if it did not.

        The caller uses the return value to stop the flow: segments are even MORE
        stateful than the steps inside them (segment N+1 acts on the app state
        segment N left behind), so continuing past a failure does not just waste
        time — it invents results. A real run failed @open_reservation in the
        waiter segment (the order was never sent to the kitchen) and the kitchen
        segment then reported PASS, because it found a STALE queued order from an
        earlier run and marked it Ready. A green segment after a red one is a lie,
        and it also logged out of waiter mid-diagnosis and destroyed the screen
        the failure needed to be read from."""
        import concurrent.futures as _fut
        role = seg["role"]
        notes: List[str] = []
        started = time.time()
        status = "PASS"
        flaky_steps = 0
        # WHERE + evidence: the first step that fails wins — we screenshot the screen at THAT
        # step (not the end of the segment) and remember which step it was, so the report can
        # say exactly where it broke and show that step's screen.
        fail_shot = None
        fail_step = None
        total_steps = len(seg["steps"])
        try:
            r = self._session_for(role)
            if role in ("waiter", "kitchen"):
                if not self._ensure_business_account(r, role, notes):
                    notes.append("[where] failed at step: 'login' (could not sign in)")
                    self._persist(seg, "FAIL", notes, time.time() - started,
                                  screenshot=self._capture_screenshot())
                    return False
            for step_idx, step in enumerate(seg["steps"], 1):
                # Stop was pressed. Check BEFORE starting a step, not during: a step
                # can run for STEP_TIMEOUT (240s), and starting one now means the
                # user waits that out after asking to stop.
                if self.cancelled:
                    notes.append(f"[stopped] stopped by user before step "
                                 f"{step_idx}/{total_steps}: '{step}'")
                    self._persist(seg, "STOPPED", notes, time.time() - started)
                    return False
                # Show the step as "currently running" before it executes so the
                # Live Steps panel says what it's doing right now, not just what's done.
                notes.append(f"▶ {step}")
                self._persist(seg, "running", notes, time.time() - started)
                notes.pop()

                # Execute the step under a hard per-step timeout. Appium can hang for
                # MINUTES resolving a missing id on this app's huge tree (a wedged
                # 'preOrderBooking' once hung a run 21 min). Bound it: if a step exceeds
                # STEP_TIMEOUT, fail the segment fast instead of stalling the whole run.
                def _exec_step():
                    _step = self._PLAIN_STEP_TOKENS.get(step, step)
                    if _step.startswith("@"):
                        _ok = self._handle_special(r, _step, notes)
                        if not _ok:
                            notes.append(f"    ↳ on screen: {self._visible_ids(r)}")
                        return _ok, False
                    _ok, _note, _flaky = self._smart_click(r, step)   # direct-id tap, else fuzzy
                    _tag = "flaky" if (_ok and _flaky) else ("ok" if _ok else "FAIL")
                    notes.append(f"[{_tag}] {step} — {_note}")
                    if _flaky and _ok:
                        notes.append(f"    ⚡ flaky: '{step}' failed once, passed on retry")
                    if not _ok:                 # self-document the real screen for tuning
                        notes.append(f"    ↳ on screen: {self._visible_ids(r)}")
                    return _ok, (_flaky and _ok)
                # NOTE: do NOT use `with ThreadPoolExecutor()` — its __exit__ calls
                # shutdown(wait=True), which BLOCKS on the hung worker thread and defeats the
                # timeout entirely (a step hung 28 min despite result(timeout=…) firing).
                # Manage it manually and shutdown(wait=False) so a hung step is abandoned.
                _watch = self._start_step_watchdog(step, seg)
                _ex = _fut.ThreadPoolExecutor(max_workers=1)
                try:
                    ok, step_flaky = _ex.submit(_exec_step).result(timeout=STEP_TIMEOUT)
                    _wnote = self._finish_step_watchdog(_watch, step, ok)
                    if _wnote:
                        notes.append(f"    {_wnote}")
                    if step_flaky:
                        flaky_steps += 1
                    _ex.shutdown(wait=False)
                except _fut.TimeoutError:
                    _ex.shutdown(wait=False)   # abandon the hung worker; don't block on it
                    _wnote = self._finish_step_watchdog(_watch, step, False)
                    if _wnote:
                        notes.append(f"    {_wnote}")
                    ok = False
                    status = "FAIL"
                    notes.append(f"[FAIL] {step} — timed out after {STEP_TIMEOUT}s (step hung; "
                                 f"aborting segment). Screen: {self._visible_ids(r)}")
                    if fail_shot is None:      # capture the screen AT this failing step
                        fail_step = f"{step} (step {step_idx}/{total_steps})"
                        fail_shot = self._capture_screenshot()
                        notes.extend(self._collect_evidence(role))
                    self.on_event({"type": "step", "role": role, "step": step, "ok": False})
                    self._persist(seg, status, notes, time.time() - started)
                    break
                self.on_event({"type": "step", "role": role, "step": step, "ok": ok})
                if not ok:
                    status = "FAIL"
                    if fail_shot is None:      # first failing step — screenshot it now
                        fail_step = f"{step} (step {step_idx}/{total_steps})"
                        fail_shot = self._capture_screenshot()
                        notes.extend(self._collect_evidence(role))
                    # STOP HERE. Steps in a segment are sequential and stateful — each one acts
                    # on the screen the previous one left. Once a step fails, every later step
                    # runs against the wrong screen: they either no-op with a misleading '[ok]
                    # … continuing' or hunt an absent id for the full STEP_TIMEOUT. A real run
                    # burned 150s that way AFTER @open_reservation had already failed, and the
                    # notes made a dead segment look like it was still making progress.
                    # Timeouts and crashes already break; a plain failure must too.
                    notes.append("    ↳ aborting segment — later steps act on the wrong screen")
                    self._persist(seg, status, notes, time.time() - started)
                    break
                # Persist progress after every step so Live Steps updates in real time.
                self._persist(seg, "running", notes, time.time() - started)
                crash = r.app_crash()   # read ONCE — a second call can race a Metro
                if crash:               # fast-refresh clearing the red box → "crashed: None"
                    status = "FAIL"
                    notes.append(f"[FAIL] app crashed: {crash}")
                    if fail_shot is None:
                        fail_step = f"{step} (step {step_idx}/{total_steps}) — app crashed"
                        fail_shot = self._capture_screenshot()
                        # The crash path is where the .ips report matters most.
                        notes.extend(self._collect_evidence(role))
                    break
        except Exception as e:
            status = "FAIL"
            notes.append(f"[FAIL] segment error: {e}")
            if fail_shot is None:
                fail_shot = self._capture_screenshot()
                notes.extend(self._collect_evidence(role))
        # Flaky summary — a green run with retries is not the same as a clean one.
        if flaky_steps:
            notes.append(f"[flaky] {flaky_steps} step(s) passed only on retry — "
                         f"unstable, worth stabilising")
        # WHERE it broke: name the exact failing step (the screenshot above is that step's screen).
        if status == "FAIL" and fail_step:
            notes.append(f"[where] failed at step: '{fail_step}'")
        self._persist(seg, status, notes, time.time() - started, screenshot=fail_shot)
        return status == "PASS"

    def _collect_evidence(self, role: str) -> List[str]:
        """Why the app failed, in its own words — gathered ONCE at the failing step.

        Three sources, each already reduced to the lines a human reads first:
          · the app's JS console (Metro log)  — this is where
            "Invariant Violation: Module AppRegistry is not a registered callable
            module" sat unread for hours on 2026-09-02 while every run reported only
            "element not found";
          · the device log for the app over the last minute;
          · any crash report for it in the last 10 minutes.

        Returns `[evidence]`-tagged note lines. Never raises and never blocks a run:
        evidence that fails to collect must not become a second failure. Deliberately
        bounded — a raw dump attached to a run is the same "app crashed" problem with
        more scrolling.
        """
        import glob
        from automation.evidence.js_console import read_js_console
        from automation.evidence.device_log import capture as capture_device_log
        from automation.evidence.crash_report import recent_for_app

        out: List[str] = []
        udid = getattr(self, "_cur_udid", "") or self.devices.get(role) or ""
        # The process name the device log and crash files use is the bundle's last
        # component (org.vyapy.sarls.vyabusinessipadstaging -> vyabusinessipadstaging),
        # which matches "VyaBusinessiPad…" case-insensitively.
        bundle = (self.consumer_bundle if role == "consumer" else self.business_bundle) or ""
        proc = bundle.rsplit(".", 1)[-1] if bundle else "Vya"

        try:
            # METRO_LOG_PATH if set, else the most recently written bundler log —
            # each app repo runs its own on its own port.
            path = os.getenv("METRO_LOG_PATH") or ""
            if not path:
                logs = sorted(glob.glob("/tmp/metro*.log"), key=os.path.getmtime, reverse=True)
                path = logs[0] if logs else ""
            for line in read_js_console(path, max_lines=12):
                out.append(f"    [evidence] js: {line[:220]}")
        except Exception:
            pass

        try:
            for line in capture_device_log(udid, proc, window="90s", max_lines=8):
                out.append(f"    [evidence] device: {line[:220]}")
        except Exception:
            pass

        try:
            for c in recent_for_app(proc, within_seconds=600):
                out.append(f"    [evidence] CRASH {c.get('app')} — {c.get('reason')} "
                           f"({c.get('signal')})")
                for fr in (c.get("frames") or [])[:4]:
                    out.append(f"    [evidence]   at {fr[:200]}")
        except Exception:
            pass

        if not out:
            out.append("    [evidence] none found (no JS errors, device errors or crash "
                       "reports in the failure window)")
        return out

    def _capture_screenshot(self) -> Optional[str]:
        """Grab the current screen as a self-contained data-URI PNG, for failure evidence
        in the rich report. Tries idb first (works even when the WDA/Appium session is
        wedged or dead — the common failure case), then falls back to `simctl io screenshot`.
        Never raises; returns None if neither produces a real image."""
        import base64, os as _os, tempfile, subprocess as _sp
        udid = getattr(self, "_cur_udid", "") or self.devices.get("consumer") or DEFAULT_CONSUMER_UDID

        def _grab(cmd) -> Optional[bytes]:
            path = None
            try:
                fd, path = tempfile.mkstemp(suffix=".png")
                _os.close(fd)
                _sp.run(cmd + [path], timeout=15, capture_output=True)
                with open(path, "rb") as f:
                    raw = f.read()
                return raw if raw and raw[:8] == b"\x89PNG\r\n\x1a\n" else None
            except Exception:
                return None
            finally:
                if path:
                    try:
                        _os.remove(path)
                    except Exception:
                        pass

        raw = (_grab([_IDB, "screenshot", "--udid", udid])
               or _grab(["xcrun", "simctl", "io", udid, "screenshot"]))
        if raw:
            return "data:image/png;base64," + base64.b64encode(raw).decode()
        return None

    # ── passive UI-loading watchdog (additive; can never fail a step) ────────
    # Observes each step from a side thread and, only when there is EVIDENCE of a
    # problem, records one note. Elapsed time alone is never an issue: a step that
    # simply takes a while is not reported. idb/simctl ONLY — an Appium session is
    # not safe for concurrent commands and this runs while the step is mid-command.
    # Disable entirely with UI_WATCHDOG=0; window via UI_WATCHDOG_SECONDS.
    def _await_screen(self, name: str, notes: List[str]) -> bool:
        """Precise adopter API: wait for a NAMED screen and report what happened.

        Additive and non-fatal by default — it records evidence and lets the
        existing step/retry logic decide the outcome. Returns True when the screen
        loaded (fast or slow), False only when it demonstrably did not.
        """
        try:
            exp = _uih.SCREENS.get(name)
            if exp is None:
                return True                      # unknown screen -> never block
            ctx = {"device": (getattr(self, "_cur_udid", "") or "")[:8],
                   "env": getattr(self, "env", "?")}
            rep = _uih.monitor_ui_loading(
                lambda: [e["label"] for e in self._idb_els() if e.get("label")],
                exp, sleep_s=5.0, context=ctx)
            if rep.result == _uih.LoadResult.SUCCESS:
                return True
            notes.append(f"    {rep.to_note()}")
            if rep.result in (_uih.LoadResult.SLOW_SUCCESS, _uih.LoadResult.RECOVERED,
                              _uih.LoadResult.MONITOR_UNAVAILABLE):
                return True                      # loaded late, or we simply could not observe
            return False
        except Exception:
            return True                          # monitoring must never block a flow

    def _watchdog_window(self) -> float:
        try:
            return float(os.getenv("UI_WATCHDOG_SECONDS", "30"))
        except Exception:
            return 30.0

    def _start_step_watchdog(self, step: str, seg) -> Optional[dict]:
        # EVERYTHING inside the try, including the kill-switch read. A missing
        # import here once raised NameError on the very first step of every run —
        # a monitoring feature must not be able to take the automation down, so
        # nothing in this method is allowed to escape.
        try:
            if os.getenv("UI_WATCHDOG", "1") == "0":
                return None
            import threading as _th
            w = {"stop": _th.Event(), "states": [], "spinners": [], "spinner_all": True,
                 "samples": 0, "errors": 0, "t0": time.time(), "first": [], "last": []}

            def _loop():
                while not w["stop"].is_set():
                    try:
                        labels = [e["label"] for e in self._idb_els() if e.get("label")]
                        w["samples"] += 1
                        st = _uih.normalise_state(labels)
                        if not w["states"]:
                            w["first"] = labels[:40]
                        w["last"] = labels[:40]
                        w["states"].append(st)
                        sp = _uih.find_spinners(labels)
                        w["spinners"] = sp
                        if not sp:
                            w["spinner_all"] = False
                    except Exception:
                        w["errors"] += 1
                    w["stop"].wait(6.0)      # 6s: each idb dump already costs ~2-3s

            t = _th.Thread(target=_loop, name="ui-watchdog", daemon=True)
            w["thread"] = t
            t.start()
            return w
        except Exception:
            return None                      # monitoring must never break a run

    def _finish_step_watchdog(self, w: Optional[dict], step: str, ok: bool) -> Optional[str]:
        """Stop the watchdog and return ONE note, or None. Never raises."""
        if not w:
            return None
        try:
            w["stop"].set()
            waited = time.time() - w["t0"]
            window = self._watchdog_window()
            if waited < window:
                return None                  # short step: nothing to say
            if ok:
                # It worked. Only worth noting that it was slow.
                return (f"[SLOW LOAD] {step} — completed after {waited:.1f}s "
                        f"(over the {window:.0f}s watchdog window)")
            if w["samples"] and w["errors"] >= w["samples"]:
                return (f"[MONITOR UNAVAILABLE] {step} — could not sample the UI "
                        f"({w['errors']}/{w['samples']} reads failed); this is a MONITORING "
                        f"fault, not an app loading failure")
            uniq = len(set(w["states"]))
            # EVIDENCE REQUIRED — never report on elapsed time alone.
            if w["spinner_all"] and w["spinners"]:
                return (f"[STUCK LOADING] {step} — a loading indicator stayed up for "
                        f"{waited:.1f}s: {w['spinners'][:3]}; expected state never reached")
            if uniq <= 1 and w["samples"] >= 2:
                return (f"[NO UI PROGRESS] {step} — no meaningful UI change across "
                        f"{w['samples']} samples over {waited:.1f}s; on screen: {w['last'][:12]}")
            return None                      # UI was moving: leave it to the step's own report
        except Exception:
            return None

    def _persist(self, seg, status: str, notes: List[str], secs: float,
                 screenshot: Optional[str] = None) -> None:
        side = "consumer" if seg["role"] == "consumer" else "business"
        with SessionLocal() as db:
            row = (db.query(ScenarioResult)
                   .filter_by(run_id=self.run_id, scenario_num=seg["num"]).first())
            if row is None:
                row = ScenarioResult(run_id=self.run_id, scenario_num=seg["num"])
                db.add(row)
            row.scenario_name = seg["name"]
            row.status = status
            row.consumer_status = status if side == "consumer" else "N/A"
            row.business_status = status if side == "business" else "N/A"
            row.reasons = notes
            # The report's "failed at" cell reads `error` — include the explicit [where]
            # marker (which exact step broke) alongside the [FAIL] reasons (how it happened).
            row.error = "; ".join(n for n in notes
                                  if "FAIL" in n or n.startswith("[where]")) or None
            row.launch_time = round(secs, 1)
            if screenshot:                       # only set on the final failure capture
                row.screenshot = screenshot
            db.commit()
        self.on_event({"type": "segment_result", "num": seg["num"],
                       "name": seg["name"], "status": status})

    def _preflight(self) -> None:
        """Bring up everything a run needs BEFORE any segment: the target sims booted
        and the Appium server listening. Previously a run just assumed both were up and
        every segment died with 'Connection refused (127.0.0.1:4723)' when they weren't."""
        import os
        import shutil
        import subprocess
        import urllib.request
        # 1. Boot each distinct sim this flow uses (consumer + waiter + kitchen).
        udids = {u for u in (self.devices.get("consumer"), self.devices.get("waiter"),
                             self.devices.get("kitchen"), DEFAULT_CONSUMER_UDID) if u}
        for udid in udids:
            try:
                subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True,
                               text=True, timeout=60)
            except Exception as e:
                logger.warning("preflight boot %s: %s", udid, e)
        # A crashed launchd_sim ("Unable to boot … launchd_sim may have crashed") is
        # cleared by launching Simulator.app, which reinitialises the subsystem.
        try:
            subprocess.run(["open", "-a", "Simulator"], capture_output=True, timeout=15)
        except Exception:
            pass
        for udid in udids:
            for _ in range(20):
                out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"],
                                     capture_output=True, text=True).stdout
                if udid in out:
                    break
                subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True)
                time.sleep(1.5)

        # 2. Ensure Appium is listening on 4723 (start it if not).
        def _appium_ready() -> bool:
            try:
                with urllib.request.urlopen(f"{APPIUM_URL}/status", timeout=3) as r:
                    return b'"ready":true' in r.read(256)
            except Exception:
                return False
        if not _appium_ready():
            appium_bin = _appium_binary()
            logger.info("preflight: starting Appium on 4723 (%s)", appium_bin)
            os.makedirs(os.path.join("logs", "appium"), exist_ok=True)
            log_path = os.path.join("logs", "appium", f"appium-preflight-{self.run_id}.log")
            # Appium's shebang is `#!/usr/bin/env node`; a detached process may not have
            # node on PATH → "env: node: No such file or directory". node lives in the
            # SAME bin dir as appium, so put that dir on PATH.
            env = dict(os.environ)
            env["PATH"] = os.path.dirname(appium_bin) + os.pathsep + env.get("PATH", "")
            env["NODE_OPTIONS"] = "--max-old-space-size=4096"
            with open(log_path, "ab") as lf:
                subprocess.Popen(
                    [appium_bin, "--address", "127.0.0.1", "--port", "4723",
                     "--relaxed-security", "--log-timestamp"],
                    stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                    start_new_session=True, env=env,
                )
            for _ in range(40):
                if _appium_ready():
                    break
                time.sleep(1)
        self.on_event({"type": "log", "message":
                       f"preflight: sims booted ({len(udids)}), Appium "
                       f"{'ready' if _appium_ready() else 'NOT ready'}"})

        # 3. The apps this run drives must actually BE on their devices, in the
        # variant this env means. A missing or wrong-variant app does not fail
        # loudly — the run opens a session, the app renders its chrome, and every
        # step then fails against a screen that never loads. That reads as "the
        # tests are broken" when the real answer is "the wrong app is installed".
        # Check it here, once, and say so plainly.
        #
        # A missing app is now DEPLOYED rather than reported: the platform already
        # knows how to build and install, so telling the user to go do it by hand was
        # withholding a capability it has. Each role carries its own device, so two
        # apps landing on two different simulators is the ordinary path here.
        #
        # Only the apps THIS flow's segments actually drive. A consumer-only flow
        # (the Quick demo is one segment, role "consumer") never opens the B-App, so
        # demanding it blocked a run that would otherwise pass — and sent the user off
        # to build an app the scenario was never going to launch. Roles map to apps
        # the same way _session_for/_prewarm_sessions map them: consumer -> C-App,
        # every other role (waiter, kitchen) -> the B-App on the shared business device.
        from automation.projects import deployment as _deploy
        roles = {seg["role"]
                 for seg in (getattr(self, "flow", None) or {}).get("segments", [])}
        required = []
        if "consumer" in roles:
            required.append(_deploy.AppRequirement(
                role="consumer",
                bundle_id=self.consumer_bundle,
                device_id=self.devices.get("consumer") or DEFAULT_CONSUMER_UDID))
        if roles - {"consumer"}:
            required.append(_deploy.AppRequirement(
                role="business",
                bundle_id=self.business_bundle,
                device_id=self._business_udid()))
        try:
            results = _deploy.prepare_scenario(
                required,
                on_log=lambda m: self.on_event({"type": "log",
                                                "message": f"preflight: {m}"}))
        except _deploy.DeploymentBlocked as e:
            # Already fully formed (app, bundle, device, reason, action) — re-wrapping
            # it would only bury the part that says what to do.
            raise RuntimeError(str(e)) from e
        except subprocess.TimeoutExpired:
            # A slow simctl must not fail the run. simctl is shared with Xcode and
            # goes to its knees under concurrent builds -- measured on this machine at
            # >120s for three get_app_container calls. Treating "simctl did not
            # answer" as "the app is not installed" turns a busy machine into a run
            # that never executed a step, which is exactly what it used to do. The
            # session will surface a genuinely missing app a few seconds later.
            self.on_event({"type": "log",
                           "message": "preflight: could not verify the required apps "
                                      "— simctl did not answer in time (busy machine); "
                                      "continuing, the session will report a missing app"})
            self._prewarm_sessions()
            return
        passed = all(r.installed for r in results)
        self.on_event({"type": "log",
                       "message": _deploy.preflight_report(results, passed=passed)})
        if not passed:
            raise RuntimeError(_deploy.preflight_report(results, passed=False))
        # Pre-warm every device session NOW, in parallel — the expensive bit is the WDA
        # build (~60s per device). Doing it lazily meant the iPad's WDA built at the first
        # C-App -> B-App switch, stalling the handoff. Building consumer (:8100) and
        # business (:8101) WDAs simultaneously here makes segment switches near-instant.
        self._prewarm_sessions()

    def _prewarm_sessions(self) -> None:
        """Create (and thus build WDA for) every device this flow uses, in parallel."""
        import concurrent.futures as _fut
        targets = {}   # udid -> (bundle, wda_port, needs_metro)
        for seg in self.flow["segments"]:
            if seg["role"] == "consumer":
                targets[self.devices.get("consumer") or DEFAULT_CONSUMER_UDID] = \
                    (self.consumer_bundle, 8100, False)
            else:  # waiter + kitchen share the business device
                targets[self._business_udid()] = (self.business_bundle, 8101, True)

        def _warm(udid, bundle, wda, needs_metro):
            try:
                if needs_metro and not self._biz_metro_ready:
                    # Pass the ACTUAL bundle. Called bare it defaults to the PROD
                    # bundle, so it writes RCT_jsLocation onto
                    # org.vyapy.sarls.vyabusinessipad and leaves the *staging*
                    # bundle unset — then returns True (8082 is up) and marks
                    # _biz_metro_ready, which makes _session_for skip the correct
                    # staging call. On a device whose staging default was never
                    # persisted the app then loads the wrong bundle for the whole run.
                    self._biz_metro_ready = ensure_business_metro(udid, self.business_bundle)
                if udid not in self._sessions:
                    d = webdriver.Remote(APPIUM_URL, options=_options(udid, bundle, wda))
                    d.activate_app(bundle)
                    self._wait_app_ready(d, bundle)
                    self._sessions[udid] = d
                    self._runners[udid] = ScenarioRunner(d, bundle, screenshot_dir=None)
                return True
            except Exception as e:
                logger.warning("prewarm session %s failed (will create lazily): %s", udid, e)
                return False

        if not targets:
            return
        # Bounded + non-blocking teardown: a WDA build can wedge, and `with ...` /
        # result() with no timeout would hang PREFLIGHT forever. Cap each build; whatever
        # doesn't warm in time is created lazily later (safe fallback), and shutdown(wait=
        # False) so a wedged build thread never blocks the run.
        ex = _fut.ThreadPoolExecutor(max_workers=len(targets))
        futs = {ex.submit(_warm, u, *v): u for u, v in targets.items()}
        ok = 0
        for f in list(futs):
            try:
                # Allow room for a fresh sim's first-ever WDA COMPILE (~3min), not just a launch
                # — matches wdaLaunchTimeout (360s). Otherwise we abandon the build mid-compile and
                # the lazy retry races the half-built WDA (the RemoteDisconnected on the phone).
                if f.result(timeout=380):
                    ok += 1
            except Exception:
                pass
        ex.shutdown(wait=False)
        self.on_event({"type": "log",
                       "message": f"preflight: pre-warmed {ok}/{len(targets)} device session(s) "
                                  f"(WDA built up front — C-App→B-App switch is now instant)"})

    def run(self) -> None:
        # Local, like every other method here — subprocess is not a module-level
        # import in this file, and the crash-reporting below names TimeoutExpired.
        import subprocess
        self.on_event({"type": "start", "run_id": self.run_id, "flow": self.flow["id"]})
        crashed = None
        # Make this run stoppable. Registered BEFORE preflight — that is where a
        # run spends its first few minutes (WDA can take ~3min to compile), and a
        # Stop pressed during it must not be silently dropped.
        with _ACTIVE_FLOW_RUNS_LOCK:
            _ACTIVE_FLOW_RUNS[self.run_id] = self._cancel
        try:
            self._preflight()
            segments = self.flow["segments"]
            for i, seg in enumerate(segments):
                if self.cancelled:
                    for skipped in segments[i:]:
                        self._persist(skipped, "SKIPPED",
                                      ["[skipped] not run — the run was stopped by the user."],
                                      0.0)
                    break
                if self._run_segment(seg):
                    continue
                if self.cancelled:
                    # Stopped mid-segment, not a real failure. _run_segment has
                    # already persisted that segment as STOPPED; mark the rest.
                    for skipped in segments[i + 1:]:
                        self._persist(skipped, "SKIPPED",
                                      ["[skipped] not run — the run was stopped by the user."],
                                      0.0)
                    break
                # STOP THE FLOW. A cross-app segment hands the next one a state:
                # the waiter sends the order the kitchen then marks Ready. Once a
                # segment fails that handoff never happened, so every later segment
                # runs against the wrong state — and can still report PASS off a
                # STALE artefact from an earlier run (measured: a failed waiter
                # segment, then a kitchen segment that marked an old queued order
                # Ready and went green). Record the rest as SKIPPED rather than
                # dropping them, so the report shows they never ran instead of
                # leaving gaps a reader fills in as "fine".
                for skipped in segments[i + 1:]:
                    self._persist(
                        skipped, "SKIPPED",
                        [f"[skipped] not run — segment {seg['num']} "
                         f"({seg['name']}) failed, and this segment depends on the "
                         f"app state it was supposed to leave behind."],
                        0.0,
                    )
                self.on_event({"type": "log",
                               "message": f"flow stopped at segment {seg['num']} "
                                          f"({seg['name']}) — {len(segments) - i - 1} "
                                          f"later segment(s) skipped"})
                break
        except Exception as e:                       # preflight/setup crash, etc.
            crashed = e
            logger.exception("flow run crashed before/while running segments")
        finally:
            # Deregister FIRST: once the thread is unwinding it can no longer be
            # stopped, and a stale entry would make a finished run look stoppable.
            with _ACTIVE_FLOW_RUNS_LOCK:
                _ACTIVE_FLOW_RUNS.pop(self.run_id, None)
            # Quitting the drivers is what actually frees the simulators. This runs
            # on the stop path too — that is the whole reason cancellation is
            # cooperative rather than killing the thread.
            for d in self._sessions.values():
                try:
                    d.quit()
                except Exception:
                    pass
            with SessionLocal() as db:
                run = db.query(TestRun).filter_by(id=self.run_id).first()
                if run is not None:
                    rows = db.query(ScenarioResult).filter_by(run_id=self.run_id).all()
                    # Reconcile any segment still marked 'running'. _run_segment
                    # writes 'running' BEFORE each step so Live Steps can show what
                    # is executing right now, and that row is only overwritten when
                    # the step returns. If the run is stopped (or the thread dies)
                    # while a step is in flight -- 'open app' can take minutes -- the
                    # row is stranded at 'running' forever. Measured: a stopped run
                    # whose header read STOPPED while its segment row still showed a
                    # RUNNING spinner at 0.0s, which reads as "Stop did nothing".
                    # Nothing else ever revisits these rows, so it has to happen here.
                    terminal = "STOPPED" if self.cancelled else "FAIL"
                    why = ("stopped by user while this step was still running"
                           if self.cancelled else
                           "the run ended while this step was still running")
                    for row in rows:
                        if (row.status or "").lower() in ("running", "queued"):
                            row.status = terminal
                            # Clear the '▶' in-flight marker on the step that never
                            # finished. The UI spins on that marker, so leaving it
                            # keeps 'running: open app' animating under a badge that
                            # now says STOPPED.
                            kept = [n for n in (row.reasons or []) if not n.lstrip().startswith("▶")]
                            stalled = [n.lstrip()[1:].strip()
                                       for n in (row.reasons or []) if n.lstrip().startswith("▶")]
                            if stalled:
                                kept.append(f"[{terminal.lower()}] {stalled[-1]} — {why}")
                            else:
                                kept.append(f"[{terminal.lower()}] {why}")
                            row.reasons = kept
                            if row.consumer_status == "running":
                                row.consumer_status = terminal
                            if row.business_status == "running":
                                row.business_status = terminal
                    # A crash, or a run that never produced ANY segment result, is a
                    # FAILURE — never report a false 'passed' just because no row said FAIL.
                    if self.cancelled:
                        # The user stopped this run. It is neither a pass nor a
                        # failure, and it must NOT be reported as one: this block
                        # used to overwrite the 'stopped' that the Stop button had
                        # just written, so a stopped run reappeared minutes later
                        # as passed/failed and looked like it had never stopped.
                        run.status = "stopped"
                    elif crashed is not None or not rows:
                        run.status = "failed"
                    else:
                        run.status = "failed" if any(r.status == "FAIL" for r in rows) else "passed"
                    run.job_state = run.status
                    # Persist WHY. This wrote status and nothing else, so a run that
                    # died in preflight -- before any segment could produce a row --
                    # reached the dashboard as "FAILED" with no RCA, no timeline and
                    # no message, and the summary guessed it was "currently running".
                    # The reason was in the backend log the whole time and never got
                    # to the person reading the report.
                    if crashed is not None and hasattr(run, "error_message"):
                        detail = str(crashed).strip() or type(crashed).__name__
                        if isinstance(crashed, subprocess.TimeoutExpired):
                            # Name the tool, not the Python exception: this is
                            # almost always simctl being starved by concurrent
                            # builds, not the run doing anything wrong.
                            detail = (f"{type(crashed).__name__}: a simulator command "
                                      f"did not return in time — {detail}")
                        run.error_message = f"{type(crashed).__name__}: {detail}"[:2000]
                    elif not rows and hasattr(run, "error_message"):
                        run.error_message = ("The run produced no scenario results — it "
                                             "failed before any segment executed.")
                    run.completed_at = datetime.utcnow()
                    db.commit()
                # Auto-prune: keep failure screenshots for only the most recent runs.
                try:
                    keep_ids = [row.id for row in
                                db.query(TestRun.id)
                                  .order_by(TestRun.created_at.desc())
                                  .limit(SCREENSHOT_KEEP_RUNS)]
                    if keep_ids:
                        (db.query(ScenarioResult)
                           .filter(ScenarioResult.screenshot.isnot(None),
                                   ~ScenarioResult.run_id.in_(keep_ids))
                           .update({ScenarioResult.screenshot: None},
                                   synchronize_session=False))
                        db.commit()
                except Exception as e:
                    logger.debug("screenshot prune skipped: %s", e)
            self.on_event({"type": "done", "run_id": self.run_id})


def start_flow_run(flow_id: str, env: str = "prod",
                   business_device: str = "tablet") -> str:
    """Create a run row and kick off a flow in the background. Returns run_id.

    env: "prod" (old Vya apps) or "staging" (STG-* apps on vya.xorstack.com).
    business_device: which device runs the B-app roles (waiter + kitchen) —
      "tablet" (default, the iPad) or "phone" (the iPhone 16 Pro, where the
      business app is also installed). The consumer role always stays on the
      iPhone. This is the phone/tablet switch surfaced in the Run modal."""
    # resolve_flow (not FLOWS) so a flow edited in the dashboard actually RUNS its
    # edited steps — otherwise the editor would silently have no effect on runs.
    flow = resolve_flow(flow_id)
    if not flow:
        raise ValueError(f"Unknown flow: {flow_id}")
    env = env if env in ENV_BUNDLES else "prod"
    cfg = cfgmod.load_config()
    devices = dict(cfg.get("devices", {}))
    if (business_device or "tablet").lower() == "phone":
        # Point the waiter + kitchen sessions at a DEDICATED iPhone 16 (base) — a separate
        # sim from the consumer's iPhone 16 Pro. Sharing one device with the consumer caused
        # WDA session collisions ("Session does not exist"); this iPhone has its own WDA and
        # the staging B-app installed. (Kitchen derives from the waiter udid via _business_udid,
        # but set both for a clear device label.)
        phone_udid = DEFAULT_BUSINESS_PHONE_UDID
        devices["waiter"] = phone_udid
        devices["kitchen"] = phone_udid
        # Make sure that dedicated iPhone sim is booted (it's normally shut down).
        try:
            import subprocess as _sp
            _sp.run(["xcrun", "simctl", "boot", phone_udid],
                    capture_output=True, text=True, timeout=60)
        except Exception:
            pass  # already booted -> simctl returns non-zero; the session create will surface real issues
    credentials = cfg.get("credentials", {})
    run_id = str(uuid.uuid4())
    env_label = "Staging" if env == "staging" else "Old Vya"
    with SessionLocal() as db:
        database.insert_test_run(db, {
            "id": run_id, "project_id": "bd34a47c-c099-4d36-ac61-810edfff31ca",
            "test_suite": f"Cross-app flows (iOS · {env_label})",
            "test_name": f"[{env_label}] {flow['name']}",
            "status": "running", "job_state": "running",
            "started_at": datetime.utcnow(), "created_at": datetime.utcnow(),
            "device_name": f"C:{(devices.get('consumer') or '')[:6]} "
                           f"B:{(devices.get('waiter') or '')[:6]}",
            "platform": "iOS", "bot_type": "ios-crossapp-flow",
        })
    runner = FlowRunner(run_id, flow, devices, credentials, env=env)
    threading.Thread(target=runner.run, name=f"flow-{flow_id}-{env}-{run_id[:8]}",
                     daemon=True).start()
    return run_id
