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
from typing import Any, Callable, Dict, List, Optional, Tuple

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy

from automation.database.config import SessionLocal
from automation.database import database
from automation.database.models import ScenarioResult, TestRun
from automation.intelligence.scenario_runner import ScenarioRunner
from automation.scenarios.cross_app_orchestrator import (
    APPIUM_URL, CONSUMER_BUNDLE, BUSINESS_BUNDLE, ENV_BUNDLES,
    DEFAULT_CONSUMER_UDID, DEFAULT_BUSINESS_UDID, DEFAULT_BUSINESS_PHONE_UDID,
    DEFAULT_PARTICIPANT_UDID,
    _options, _fill_field, ensure_business_metro, ensure_app_metro,
    _business_metro_target, apply_flow_speed_settings,
)
from automation.scenarios import cross_app_config as cfgmod
from automation.scenarios import ui_health as _uih
from automation.scenarios import idb_driver as _idbd
from automation.scenarios import idb_fast as _idb_fast

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
# When one profile pays for the others (@pay_for_all), staging ignores a non-cash
# payment that EQUALS the bill (server bug: paidAmount 0, likely an unrounded float
# sum; see known_issues.md) but settles any amount above it. The user decided
# 2026-10-27 to pay 1 cent over so the flow can pass; the exact-amount bug stays
# reported to the server team.
PAY_FOR_OTHERS_OVER_BY = 0.01


# ── Reusable segment step blocks ───────────────────────────────────────────
_C_BOOK_PREFIX = [
    "open app",
    "@consumer_home",                 # app resumes on its last screen → back out to Home
    "click NylaiKitchen2",            # restaurant card is listed directly on Home
    "select Any",                     # dining area — NylaiKitchen2 shows slots under 'Any'
    "@duration:not_sure",             # duration slider → Not Sure (asked for every scenario)
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
    "@consumer_checkout",             # this booking's Wallet card → scroll → Checkout
    "@consumer_epay",                 # E-Payment → Stripe 'Pay € X' (saved test card) → confirmed
]

# Consumer: rate + review a finished order from the Wallet's Finished tab.
_C_LEAVE_REVIEW = [
    "open app",
    "click walletTab",                # bottom tab (confirmed id)
    "@leave_review",                  # Finished tab → star prompt → Review screen → Submit
]

# Waiter: create the appointment for the diner (flows 5/6). Mirrors the verified
# 'Business: Book event (waiter)' block, with roopa's details.
_W_CREATE_APPT = [
    "@waiter_board",                  # addNewEvent lives on the bookings board only
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
    "?click Yes",                     # optional: this build confirms with a toast, no dialog
]

# Waiter: settle in the B-App via E-Payment (amount>bill, verify change), close.
_W_PAY_CLOSE = [
    "@pay:epay",                      # E-Payment: enter amount > bill → verify change
    "click closeTableBtn",
]

# Waiter: just close the table (used when the consumer paid in the C-App).
_W_CLOSE = [
    "@waiter_close_table",            # open THIS booking (paid in the C-App) → Close Table
]

# Waiter: serve → notify → settle by a SPECIFIC method (cash / voucher) → close.
# Ported from the bot's PAY scenarios; reuses the verified @pay:<method> handler.
_W_SERVE_PAY_CASH = _W_SERVE_NOTIFY + ["@pay:cash", "click closeTableBtn"]
_W_SERVE_PAY_VOUCHER = _W_SERVE_NOTIFY + ["@pay:voucher", "click closeTableBtn"]


# ── Guests + VOID / COMP / SPLIT + Pay For ──────────────────────────────────
# Consumer: book for 4 -- the diner plus 3 invited guests -- and pre-order nothing.
# The adult '+' opens My Contacts; each 'Guest' tap adds one; Invite sets Persons.
_C_BOOK_WITH_GUESTS = [
    "open app",
    "@consumer_home",
    "click NylaiKitchen2",
    "@invite_guests:3",               # adult + → Guest ×3 → Invite (Persons 1 → 4)
    "select Any",
    "@duration:not_sure",             # slide the duration bar to Not Sure
    "@first_time_slot",
    "@book_appointment",
    "@order_later",                   # no pre-order: just reserve the table
]

# ── Full event (flow_full_event) ─────────────────────────────────────────────
# Phase 1: the host books for 7 -- 5 guests plus the participant, a real account
# (Noolu Naga, in the host phone's Contacts) -- and both diners pre-order NAMED
# products covering the scenario's mix. Menu facts read from staging 2026-10-29
# (NylaiKitchen2): Penne Pollo €17 (3 %, no options) · Spaghetti Bolognese €12
# (3 %) · hyderabad chicken biriyani €15 (17 %, no options) · mushroom biryani
# €8.02 (17 %, sizes) · Pizza Margheritas €10 (3 %, sizes + the compulsory 'hu'
# modifier: veg +2.28 @14 %, non veg +1.03 @3 %) · Tagliatelle dello Chef €17.5
# (14 %, optional add-ons: cashew +12 @3 %, pista +1 @14 %).
# No modifier on this menu carries 17 %: the 17 % bracket comes from the products.
# The DRINKS section's tiles did not render in the C-App menu (MEASURED 2026-10-29:
# its header is there, no tile), so the drinks are added by the waiter (Phase 2).
_C_FULL_EVENT_HOST_PREORDER = [
    "@before:14:15",                  # the booking must fall before 15:00 (pre-order menu)
    "open app",
    "@consumer_home",
    "click NylaiKitchen2",
    "@invite_guests:5+Noolu Naga",    # Guest ×5 + the participant → Persons 1 → 7
    "select Any",
    "@duration:not_sure",
    "@first_time_slot",
    "@book_appointment",
    "@pre_order",                     # the booking card's Menu
    "@c_add:Penne Pollo",                              # no options, 1x, 3 %
    "@c_add:hyderabad chicken biriyani*3",             # no options, 3x, 17 %
    "@c_add:mushroom biryani*2[Large]",                # variant, 2x, 17 %
    "@c_add:Pizza Margheritas*2[large, veg]",          # variant + 14 % modifier, 2x
    "@c_add:Tagliatelle dello Chef[pista]",            # 14 % dish + 14 % modifier
    "@to_checkout",
    "@pay_stripe",
    "@got_it",
]

# The participant, on their own iPhone: accept the host's invitation, then
# pre-order their own mix and pay it.
_P_FULL_EVENT_ACCEPT_PREORDER = [
    "open app",
    "@consumer_home",
    "click walletTab",
    "@wait_screen:wallet",
    "accept the appointment",
    "@pre_order",                     # the event card's Menu (it carries the host's name)
    "@c_add:Spaghetti Bolognese*2",                    # no options, 2x, 3 %
    "@c_add:Pizza Margheritas[small, non veg]",        # variant + 3 % modifier
    "@c_add:mushroom biryani*3[medium]",               # variant, 3x, 17 %
    "@c_add:Tagliatelle dello Chef[cashew]",           # 14 % dish + 3 % modifier
    "@to_checkout",
    "@pay_stripe",
    "@got_it",
]

# Phase 2, the waiter (iPad): open the event, assign a table, and add products by
# hand for the seven people -- quantities 2, 3 and 5, assigned and split, mixed:
# Coca-Cola® Light 1.0l (14 %, no options; the B-App lists no 'Coca-Cola® 1.0lh',
# MEASURED 2026-10-29) ×5 split five ways and ×2 assigned to the two diners (each gets
# 2: an Assign gives every person the line); a 3-unit pizza for one guest and a
# 3-unit biryani split two ways, which Phase 6 comps one unit of;
# Pizza Margheritas ×2 with a variant + 14 % modifier; mushroom biryani ×3 (17 %,
# variant) split three ways; Pizza Salamees ×5 (14 %) with 3 % extras; Penne Pollo
# ×3 (3 %) split two ways.
_W_FULL_EVENT_TABLE_ADD = [
    "@open_event",                    # reserved -- or in progress on a resumed run
    "@assign_table",
    "@w_add:Coca-Cola® Light 1.0l*5 > split: Guest 1, Guest 2, Guest 3, Guest 4, Guest 5",
    "@w_add:Coca-Cola® Light 1.0l*2 > assign: Roopa D, Noolu Naga",      # 2 EACH
    "@w_add:Pizza Margheritas*2[large, veg] > assign: Guest 2, Guest 3",
    "@w_add:Pizza Margheritas*3[small, non veg] > assign: Guest 4",     # comp 1 of 3 later
    "@w_add:mushroom biryani*3[sharing] > split: Roopa D, Noolu Naga, Guest 4",
    "@w_add:Pizza Salamees*5[Spinach, Scampi] > assign: Guest 5",
    "@w_add:Penne Pollo*3 > split: Guest 1, Guest 2",                   # voided whole later
    "@w_add:hyderabad chicken biriyani*3 > split: Guest 3, Guest 5",    # comp 1 of a split
]

# Phase 3, the waiter, BEFORE anything goes to the kitchen: change quantities in the
# profiles' cards (+1, -1), remove a product, split one by swipe, void a split item,
# then send everything to the kitchen.
_W_FULL_EVENT_MODIFY_SEND = [
    "@open_event",
    "@w_qty:Guest 5/Pizza Salamees =6",               # 5 → 6 (an increase)
    "@w_qty:Guest 2/Pizza Margheritas =1",            # 2 → 1 (a decrease)
    "@w_qty:Guest 3/Pizza Margheritas remove",        # gone from Guest 3
]
# Phase 4, after SEND (the kitchen has not prepared anything yet): move a line to
# another person and share another, on the grid view; void ONE unit of a split line.
_W_FULL_EVENT_AFTER_SEND = [
    "@open_event",
    "@w_move:Pizza Margheritas@Guest 2 > Noolu Naga",          # Assign after SEND
    "@w_gsplit:Coca-Cola® Light 1.0l@Noolu Naga > Guest 3",     # Split after SEND
    "@w_void:Pizza Salamees@Guest 5=5",                        # 6/2 → 5/2: one unit
    # A line moved or split after SEND can be a NEW, unsent line (MEASURED 2026-10-29):
    # send what is unsent, so the kitchen readies everything.
    "@send_if_any",
]
# Phase 5: the kitchen readies everything; before serving, one more split -- of an
# item WITHOUT options this time (Phase 3's swipe-split was a pizza with extras).
# Phase 6, after serving: four comps.
_W_FULL_EVENT_SPLIT_SERVE = [
    "@open_order",
    "@w_split:Coca-Cola® Light 1.0l@Roopa D > Guest 2",          # no options
    "@select_all_items",              # the prepared lines (Select All takes those first)
    "click serveItemsBtn",
    "@send_if_any",                   # the split's lines, if they came back unsent
]
_W_FULL_EVENT_SERVE_REST = [
    "@open_order",
    "@select_all_items",
    "click serveItemsBtn",
]
_W_FULL_EVENT_COMPS = [
    "@open_order",
    "@w_comp:Pizza Salamees@Guest 4*all:100",          # 1: a split item with options, whole
    "@w_comp:Pizza Margheritas@Guest 4*1:10",          # 2: unsplit, options, 1 of 3
    "@w_comp:hyderabad chicken biriyani@Guest 3*1:25",  # 3: 3 split between 2, 1 unit
    "@w_comp:Coca-Cola® Light 1.0l@Guest 1*1:15",      # 4: an item without options
]
_W_FULL_EVENT_SPLIT_VOID_SEND = [
    "@open_event",
    "@w_split:Pizza Salamees@Guest 5 > Guest 4",      # 6 → 6/2 each (swipe SPLIT)
    "@w_void:Penne Pollo@Guest 1*all",                # Guest 1's share of the 3/2 split
    "@select_all_items",
    "@send_to_kitchen",
]

# Phase 7: before Notify Payment, both diners order more from their own phones --
# dishes each has not had yet, so every later step names one line: the host Pizza
# Prosciutto e Funghi ×2 (14 %, +Spinach) and Spaghetti Bolognese (3 %, no
# options); the participant Pizza Parma e Rucola ×2 (3 %, +Salami) and Penne Pollo
# ×2. The waiter gives some to guests, sends, then re-assigns a host dish and splits
# a participant dish (new, unsent lines -- sent again); the kitchen readies them; the
# waiter serves and splits one more consumer dish.
_C_FULL_EVENT_MORE = [
    "open app",
    "@consumer_home",
    "click walletTab",
    "@wait_screen:wallet",
    "@pre_order",                     # the event card's Menu (the table is assigned)
    "@c_add:Pizza Prosciutto e Funghi*2[Spinach]",
    "@c_add:Spaghetti Bolognese",     # (Tagliatelle al Salmone does not show in the C-App)
    "@c_confirm_order",
]
_P_FULL_EVENT_MORE = [
    "open app",
    "@consumer_home",
    "click walletTab",
    "@wait_screen:wallet",
    "@pre_order",
    "@c_add:Pizza Parma e Rucola*2[Salami]",
    "@c_add:Penne Pollo*2",
    "@c_confirm_order",
]
_W_FULL_EVENT_MORE_ASSIGN_SEND = [
    "@open_order",
    "@w_move:Spaghetti Bolognese@Roopa D > Guest 1",           # to another guest
    "@select_all_items",
    "@send_to_kitchen",
    "@w_move:Pizza Prosciutto e Funghi@Roopa D > Guest 3",     # re-assign after SEND
    "@w_gsplit:Pizza Parma e Rucola@Noolu Naga > Guest 4",     # split after SEND
    "@send_if_any",                   # moved / split lines may come back unsent
]
_W_FULL_EVENT_MORE_SERVE_SPLIT = [
    "@open_order",
    "@select_all_items",
    "click serveItemsBtn",
    # One more split after serving -- of a consumer dish that was NOT moved: a moved
    # line's SPLIT opened nothing after serving (MEASURED 2026-10-29, two tries).
    "@w_split:Penne Pollo@Noolu Naga > Guest 5",
]

# A REHEARSAL of flow_full_event for the afternoon/evening: the restaurant's pre-order
# menu after 15:00 has none of Phase 1's dishes (MEASURED 2026-10-29), so segments 1-2
# pre-order dishes that menu allows; every later segment is the real flow's, unchanged
# (the waiter's menu and table ordering carry all of their dishes all day).
_C_FULL_EVENT_HOST_PREORDER_EVE = [s for s in _C_FULL_EVENT_HOST_PREORDER
                                   if not s.startswith(("@before:", "@c_add:"))]
_i = _C_FULL_EVENT_HOST_PREORDER_EVE.index("@to_checkout")
_C_FULL_EVENT_HOST_PREORDER_EVE[_i:_i] = [
    "@c_add:Tagliatelle al Monti",                     # no options, 3 %
    "@c_add:Tagliatelle dello Chef*2[pista]",          # 14 % dish + 14 % modifier, 2x
    "@c_add:Pizza Salamees*3[Spinach]",                # 14 % dish + 3 % modifier, 3x
    "@c_add:Spaghetti Aglio e Olio[Insalata con Pollo, cashew]",   # variant + modifier
]
_P_FULL_EVENT_ACCEPT_PREORDER_EVE = [s for s in _P_FULL_EVENT_ACCEPT_PREORDER
                                     if not s.startswith("@c_add:")]
_i = _P_FULL_EVENT_ACCEPT_PREORDER_EVE.index("@to_checkout")
_P_FULL_EVENT_ACCEPT_PREORDER_EVE[_i:_i] = [
    "@c_add:Spaghetti alla Puttanesca*2",              # no options, 2x
    "@c_add:Tagliatelle dello Chef[cashew]",           # 14 % dish + 3 % modifier
]

# Waiter: assign a table, add items, VOID one, add a dish for EVERY profile, SPLIT
# one of those with another profile, then send everything to the kitchen.
_W_ASSIGN_VOID_SPLIT_SEND = [
    "@open_reservation",
    "@assign_table",
    "@ensure_order_items",            # order-later booking: the order is empty → add items
    "@void_item",                     # swipe ← VOID → Entry Error → Apply
    "@add_item_for_all",              # ADD → dish → ASSIGN / SPLIT → every profile → Assign
    "@split_item",                    # swipe ← SPLIT → one profile → Apply
    "@select_all_items",
    "@send_to_kitchen",
]

# Waiter, after the kitchen: serve all, COMP one item, notify, one profile pays for
# everyone, then settles the whole bill by E-Payment and closes the table.
_W_SERVE_COMP_PAYFOR_CLOSE = [
    "@open_order",
    "@select_all_items",
    "click serveItemsBtn",
    "@comp_item",                     # swipe ← COMP → Birthday → 10 % → Apply
    "@notify_payment",                # NOTIFY PAYMENT → Yes
    "@pay_for_all",                   # profile → Pay For → every profile → Apply
    "@pay:epay",                      # profile → E-Payment → bill total → Input → Confirm
    "@close_table",                   # wait for Close Table (it follows the payment), close
]


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
# The hour's events list is open and does NOT list the booking: the board is stale.
_BOARD_STALE = object()


def _row_status(label: str) -> str:
    """The status WORD of an events-list / My Orders row, normalised.

    Rows read '4776 <icon> SERVE 17:05 - 18:05 <icon> I2 17:00': the status sits
    between the ticket number and the time window. Matching statuses as substrings
    of the whole label is wrong here -- 'reserved' CONTAINS 'serve', so a Reserved
    booking at the same time would pass for one the kitchen has made ready."""
    m = _SIDEBAR_ROW_RE.search(label or "")
    head = (label or "")[:m.start()] if m else (label or "")
    return _norm(re.sub(r"^\s*\d+", "", head))


def _status_ok(label: str, statuses) -> bool:
    """Whole-word status match; a status may be the start of the word
    ('payment' matches 'PAYMENT DONE' -> 'paymentdone')."""
    st = _row_status(label)
    return any(st == want or st.startswith(want) for want in statuses)


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


_NOT_GIVEN = object()


def _bounded_call(fn: Callable[[], Any], secs: float = 20.0, default: Any = None,
                  late: Any = _NOT_GIVEN) -> Any:
    """fn() with a ceiling; *default* on error, and on timeout too unless *late* is
    given (then *late*, so a caller can tell "WDA did not answer" from "it failed").

    A wedged WDA parks in an Appium call on this app's huge tree with no ceiling of
    its own. Measured 2026-10-17: @open_order sat silent after "target 11:00 row"
    until the 240s step watchdog killed it (runs 100df69f, adbb3f49, 30c434a6,
    e0685fc6). Manual executor + shutdown(wait=False), never `with` (AGENTS.md #5).

    A timed-out call is NOT cancelled: Appium queues each session's commands and
    runs it once WDA frees up. So after a timeout, send nothing more that could
    act on the screen late -- and never relaunch the app through Appium (see
    FlowRunner._relaunch_business).
    """
    import concurrent.futures as _fut
    ex = _fut.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=secs)
    except _fut.TimeoutError:
        return default if late is _NOT_GIVEN else late
    except Exception:
        return default
    finally:
        ex.shutdown(wait=False)


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
    """Locate the `idb` executable (shared resolver: scenarios/idb_path.py).

    shutil.which() alone fails in the flow's daemon process (PATH lacks the pip
    user-bin), which made every idb call throw FileNotFoundError and silently
    broke the fast idb steps."""
    from automation.scenarios.idb_path import idb_binary
    return idb_binary()


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
    "flow_guests_void_comp_split": {
        "id": "flow_guests_void_comp_split",
        "name": "Guests → void / split / comp → pay for all in B-App",
        "description": "Consumer books for 4 (invites 3 guests from the Persons +, no "
                       "pre-order). Waiter assigns a table, adds items, VOIDs one (Entry "
                       "Error), adds a dish for every profile, SPLITs one with another "
                       "profile and sends all to the kitchen; kitchen readies; waiter "
                       "serves, COMPs one item (Birthday, 10 %), notifies payment, one "
                       "profile pays for everyone, settles by E-Payment and closes.",
        "segments": [
            _seg("1", "C-App: book for 4 (3 invited guests), no pre-order", "consumer",
                 _C_BOOK_WITH_GUESTS),
            _seg("2", "Waiter: assign + add + void + assign to all + split + send", "waiter",
                 _W_ASSIGN_VOID_SPLIT_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + comp + notify + pay for all + E-Payment + close",
                 "waiter", _W_SERVE_COMP_PAYFOR_CLOSE),
        ],
    },
    # The user's 12-phase end-to-end scenario as ONE flow (automation/knowledge/
    # full_event_map.md). Built phase by phase; each phase is proven on the
    # simulators before the next is added.
    "flow_full_event": {
        "id": "flow_full_event",
        "name": "Full event: host + participant + 5 guests → orders → kitchen → pay → invoices",
        "description": "The full event, end to end. (1) The host books for 7 (5 guests "
                       "+ the participant Noolu Naga, invited from Contacts) and pre-orders "
                       "named products (no options, variants, 3/14 % modifiers, 1x-3x); the "
                       "participant accepts on their own iPhone and pre-orders; both pay by "
                       "card. (2) The waiter assigns a table and adds products for all 7, "
                       "assigned and split, quantities 2/3/5. (3) Quantity +/-, a removal, a "
                       "swipe-split, a split item voided whole, SEND. (4) After SEND: a line "
                       "moved, a line shared, one unit of a split voided. (5) Kitchen "
                       "readies all; a split of an item without options; serve. (6) Four "
                       "comps. (Later phases are added as they are proven.)",
        "segments": [
            _seg("1", "Host (C-App): book for 7 (5 guests + participant), pre-order, pay",
                 "consumer", _C_FULL_EVENT_HOST_PREORDER),
            _seg("2", "Participant (C-App, own iPhone): accept, pre-order, pay",
                 "participant", _P_FULL_EVENT_ACCEPT_PREORDER),
            _seg("3", "Waiter: open the event, assign a table, add for all 7 (assign/split)",
                 "waiter", _W_FULL_EVENT_TABLE_ADD),
            _seg("4", "Waiter: quantities +1 / -1, remove a product", "waiter",
                 _W_FULL_EVENT_MODIFY_SEND),
            _seg("5", "Waiter: swipe-split, void a split item, send all to the kitchen",
                 "waiter", _W_FULL_EVENT_SPLIT_VOID_SEND),
            _seg("6", "Waiter, after SEND: move a line, share a line, void one unit",
                 "waiter", _W_FULL_EVENT_AFTER_SEND),
            _seg("7", "Kitchen: mark every item ready", "kitchen", ["@kitchen_ready"]),
            _seg("8", "Waiter: split an item without options, serve, send the split's lines",
                 "waiter", _W_FULL_EVENT_SPLIT_SERVE),
            _seg("9", "Kitchen: ready the split's lines", "kitchen", ["@kitchen_ready"]),
            _seg("10", "Waiter: serve the rest", "waiter", _W_FULL_EVENT_SERVE_REST),
            _seg("11", "Waiter: four comps (whole / one unit; split / unsplit; options / none)",
                 "waiter", _W_FULL_EVENT_COMPS),
            _seg("12", "Host (C-App): order more during the event", "consumer",
                 _C_FULL_EVENT_MORE),
            _seg("13", "Participant (C-App): order more during the event", "participant",
                 _P_FULL_EVENT_MORE),
            _seg("14", "Waiter: give new dishes to guests, send, re-assign, split, send",
                 "waiter", _W_FULL_EVENT_MORE_ASSIGN_SEND),
            _seg("15", "Kitchen: ready the new dishes", "kitchen", ["@kitchen_ready"]),
            _seg("16", "Waiter: serve the new dishes, then one more split", "waiter",
                 _W_FULL_EVENT_MORE_SERVE_SPLIT),
            _seg("17", "Waiter: notify payment for all seven", "waiter",
                 ["@open_order", "@notify_payment"]),
            _seg("18", "Host (C-App): pay for two guests, coupon, tip, E-Payment", "consumer",
                 ["open app", "@consumer_home", "@consumer_checkout",
                  "@c_pay_for:Guest 1, Guest 2", "@c_coupon", "@c_tip:2", "@consumer_epay"]),
            _seg("19", "Waiter: participant pays for two guests (discount, tip, E-Payment); "
                       "the rest by cash", "waiter",
                 ["@open_order", "@w_pay_for:Noolu Naga > Guest 3, Guest 4",
                  "@w_discount:Noolu Naga", "@w_tip:Noolu Naga 3", "@pay:epay",
                  "@w_payer:Guest 5", "@pay:cash"]),
            _seg("20", "Waiter: the event invoice, close the table", "waiter",
                 ["@w_invoice", "@close_table"]),
            _seg("21", "Host (C-App): review the restaurant, open the invoice", "consumer",
                 ["open app", "@consumer_home", "@c_rate:5", "@c_invoice"]),
            # The participant's invoice is not opened (the user's decision, 2026-10-29).
            _seg("22", "Participant (C-App): skip the rating", "participant",
                 ["open app", "@consumer_home", "@c_skip_review"]),
        ],
    },
    # Short, single-app flow for LIVE demos: fast + low-risk (no cross-app handoff, no
    # payment). Proves the engine really drives the app end-to-end in ~2-3 minutes.
    "flow_book_demo": {
        "id": "flow_book_demo",
        "name": "Quick demo — Consumer books a table (~2 min)",
        "description": "The Consumer opens the app, picks NylaiKitchen2, Any · Not Sure, taps a "
                       "time slot, and books. One app, ~7 fast exact-id steps — the safe "
                       "thing to run live in front of people (completes right at booking).",
        "segments": [
            _seg("1", "C-App: book a table (Any · Not Sure)", "consumer", _C_BOOK_PREFIX),
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
        "description": "Self-contained: the diner books with ORDER LATER, the waiter assigns a "
                       "table, adds the items and sends them to the kitchen, then the kitchen "
                       "selects THIS run's ticket, marks it Ready and closes it. (It used to act "
                       "only on an order already queued, and failed in every Run all where the "
                       "kitchen was empty; made self-contained at the user's request, 2026-10-27.)",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: assign + add items + send to kitchen", "waiter", _W_ASSIGN_SEND),
            _seg("3", "Kitchen: mark the order ready", "kitchen", _K_READY),
        ],
    },
    # ── AI Agent drafts ──
    "cc_leave_review": {
        "id": "cc_leave_review",
        "name": "[Draft] Consumer leaves a review (stars, feedback, submit)",
        "description": "Consumer app, Wallet → Finished tab → Screens/Wallet/Review.js: "
                       "opens a finished order that has not been reviewed yet, picks a "
                       "rating (star2, the screen's own real id), writes feedback text "
                       "and taps Submit. Proves Submit works for a low star count — "
                       "Review.js gates submitReview only on submitting/success, never "
                       "on the rating, so this is documented app behaviour (no minimum "
                       "star rating is enforced), not something to fix.",
        "segments": [
            _seg("1", "C-App: rate + review a finished order", "consumer", _C_LEAVE_REVIEW),
        ],
    },
    "cc_orders_filter_status": {
        "id": "cc_orders_filter_status",
        "name": "[Draft] Waiter filters My Orders by status (Reserved)",
        "description": "Consumer books with ORDER LATER (no pre-order). On the "
                       "waiter's Home board, the My Orders panel "
                       "(Screens/Orders/index.js) is filtered to 'Reserved' via the "
                       "status Filter sheet — a magnus Overlay whose options carry "
                       "no accessibility id (locator_map.md's Overlay trap), so they "
                       "are picked by OCR text, the same machinery @void_item/"
                       "@comp_item/@split_item already use — then Apply, and the "
                       "narrowed list's 'N result(s) shown for' line is checked to "
                       "prove the filter actually took effect, not just that the "
                       "sheet closed.",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: filter My Orders by status", "waiter", [
                "@waiter_board",
                "click orderFilterBtn",
                "@filter_orders:Reserved",
            ]),
        ],
    },
    "cc_decline_invite": {
        "id": "cc_decline_invite",
        "name": "[Draft] Consumer declines a waiter-created invitation (Wallet)",
        "description": "The waiter creates an appointment for roopa D (the flow5 block). In "
                       "the C-App Wallet → Upcoming (Screens/Wallet/Upcoming.js) the "
                       "consumer opens THIS run's invitation (the modal header shows the "
                       "booked slot) and taps DECLINE (eventDecline, Components/Modal/"
                       "index.js). Proves the invitation stops being pending: it leaves "
                       "the list, or stays only as a row without ACCEPT/DECLINE. Never "
                       "declines an invitation for another slot (shared account).",
        "segments": [
            _seg("1", "Waiter: create appointment (roopa D)", "waiter", _W_CREATE_APPT),
            _seg("2", "C-App: decline the invitation (wallet)", "consumer", [
                "open app",
                "click walletTab",
                "@wait_screen:wallet",
                "@decline_appointment",
            ]),
        ],
    },
    "cc_book_two_hours": {
        "id": "cc_book_two_hours",
        "name": "[Draft] Consumer books a table for 2 hours (duration slider)",
        "description": "Every other flow books 'Not Sure'. Consumer Reservation screen "
                       "(Screens/StoreView/Reservation.js): Any dining area, the duration "
                       "slider set to '2 hr' (duration2), the first slot that fits two "
                       "hours, book, Order later. Then the Wallet event for this run's "
                       "slot (Wallet/UpcomingCard.js) must print '2 hr' — the duration "
                       "the server stored, not just the one tapped.",
        "segments": [
            _seg("1", "C-App: book Any · 2 hr + order later, check the Wallet", "consumer", [
                "open app",
                "@consumer_home",
                "click NylaiKitchen2",
                "select Any",
                "@duration:2",
                "@first_time_slot",
                "@book_appointment",
                "@order_later",
                "click walletTab",
                "@wallet_event_shows:2 hr",
            ]),
        ],
    },
    "cc_book_indoor_one_hour": {
        "id": "cc_book_indoor_one_hour",
        "name": "[Draft] Consumer books an Indoor table for 1 hour",
        "description": "Every other flow books the 'Any' dining area. Consumer Reservation "
                       "screen (Screens/StoreView/Reservation.js): the 'Indoor' dining "
                       "area, the duration slider at '1 hr' (duration1), the first slot, "
                       "book, Order later if asked. Then the Wallet event for this run's "
                       "slot (Wallet/UpcomingCard.js) must print 'Indoor .1 hr' — the area "
                       "and duration the server stored.",
        "segments": [
            _seg("1", "C-App: book Indoor · 1 hr, check the Wallet", "consumer", [
                "open app",
                "@consumer_home",
                "click NylaiKitchen2",
                "select Indoor",
                "@duration:1",
                "@first_time_slot",
                "@book_appointment",
                "@order_later",
                "click walletTab",
                "@wallet_event_shows:Indoor .1 hr",
            ]),
        ],
    },
    "cc_reset_table": {
        "id": "cc_reset_table",
        "name": "[Draft] Waiter resets an assigned table (My Bookings ⋮ → Reset table)",
        "description": "Consumer books with ORDER LATER (nothing ordered, so Util/"
                       "canResetTable.js allows a reset). The waiter opens the booking and "
                       "assigns a table (In Progress), then on the My Bookings board "
                       "(Screens/Home/index.js) opens THIS booking card's ⋮ menu "
                       "(Components/BookingCard, no id), picks Reset table (resetTableBtn, "
                       "Components/Modal/EventAction/index.js), Confirm (confirmEventBtn) "
                       "and 'Reset' on the app's Alert. Proves it: the 'Table has been "
                       "reset' toast, the card leaving the board's Table view, then the "
                       "booking reopens as Reserved onto the 'Select a table' sheet.",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: assign a table, reset it, check Reserved", "waiter", [
                "@open_reservation",
                "@assign_table",
                "@reset_table",
                "@open_reservation",
                "@see:Select a table",
            ]),
        ],
    },
    "cc_cancel_event": {
        "id": "cc_cancel_event",
        "name": "[Draft] Waiter cancels an appointment it created (My Bookings ⋮ → Cancel)",
        "description": "The waiter creates an appointment for roopa D (the flow5 block): a "
                       "business-created, Confirmation Pending event with nothing in the "
                       "kitchen, so Cancel is enabled (Components/Modal/EventAction/index.js "
                       ":74-80). On the My Bookings board (Screens/Home/index.js) it opens "
                       "THIS appointment's ⋮, picks 'Cancel the event' (cancelEvent) and "
                       "Confirm (confirmEventBtn). Proves it on both sides: the 'Event has "
                       "been cancelled successfully!' toast and the card leaving the pending "
                       "cards, then the consumer's Wallet invitation for this slot shows "
                       "'Event Cancelled' (vya-consumer Screens/Wallet/Upcoming.js:1108).",
        "segments": [
            _seg("1", "Waiter: create appointment (roopa D), cancel it from ⋮", "waiter",
                 _W_CREATE_APPT + ["@cancel_event"]),
            _seg("2", "C-App: the invitation shows Event Cancelled", "consumer", [
                "open app",
                "click walletTab",
                "@wait_screen:wallet",
                "@wallet_invite_cancelled",
            ]),
        ],
    },
    "cc_edit_event": {
        "id": "cc_edit_event",
        "name": "[Draft] Waiter edits a pending appointment's time; the invitation follows",
        "description": "The waiter creates an appointment for roopa D (the flow5 block): "
                       "Confirmation Pending, no waiter yet, so 'Edit the event' is enabled "
                       "(Components/Modal/EventAction/index.js:175). From its ⋮ on the My "
                       "Bookings board: editEvent → Confirm → the Edit Appointment panel "
                       "(Components/EditEventModal) → the next time chip (<HH:mm>Btn) → "
                       "Update (saveEditBtn) → 'Reservation updated'. Then the consumer's "
                       "Wallet invitation must be at the NEW time: @decline_appointment "
                       "opens only the invitation whose modal shows that slot, and declines "
                       "it (which also leaves no pending invitation on the shared account).",
        "segments": [
            _seg("1", "Waiter: create appointment (roopa D), edit its time", "waiter",
                 _W_CREATE_APPT + ["@edit_event_time"]),
            _seg("2", "C-App: the invitation is at the new time (decline it)", "consumer", [
                "open app",
                "click walletTab",
                "@wait_screen:wallet",
                "@decline_appointment",
            ]),
        ],
    },
    "cc_modify_table": {
        "id": "cc_modify_table",
        "name": "[Draft] Waiter moves a seated booking to another table (Modify Table)",
        "description": "Consumer books with ORDER LATER. The waiter opens the booking and "
                       "assigns a table (In Progress). On the Event screen the header's table "
                       "(modifyTable, Screens/Event/PaymentDetails.js:4913 — open only before "
                       "payment is notified) opens the 'Modify Table' sheet "
                       "(Components/Modal/EventTableSelect, assignRoom status 'modified'); "
                       "it picks a free table other than the current one and Confirms "
                       "(AssignTableBtn). Proves the move: the sheet closes and the header "
                       "now prints the new table.",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: assign a table, then move it to another", "waiter", [
                "@open_reservation",
                "@assign_table",
                "@modify_table",
            ]),
        ],
    },
    "cc_remind_payment": {
        "id": "cc_remind_payment",
        "name": "[Draft] Waiter reminds the diner to pay, then settles the bill",
        "description": "Consumer books with ORDER LATER; the waiter assigns a table, adds "
                       "the items and sends them; the kitchen readies them; the waiter "
                       "serves and notifies payment. Once notified, the iPad's order "
                       "summary turns that button into REMIND PAYMENT (same id "
                       "notifyPaymentBtn, Screens/Event/OrderSummary.js:1328-1350, shown "
                       "while appointment.reminder and unpaid). Tapping it posts "
                       "reminderData (Screens/Event/index.js:1033) and toasts 'Payment "
                       "Notified'. Proves it: REMIND PAYMENT is on screen after the notify, "
                       "the reminder toast shows after the tap, and the bill still settles "
                       "by E-Payment (bill check) and the table closes.",
        "segments": [
            _seg("1", "C-App: book + order later", "consumer", _C_ORDER_LATER),
            _seg("2", "Waiter: assign + add items + send to kitchen", "waiter", _W_ASSIGN_ADDALL_SEND),
            _seg("3", "Kitchen: mark items ready", "kitchen", _K_READY),
            _seg("4", "Waiter: serve + notify, remind payment, pay (B-App) + close", "waiter",
                 _W_SERVE_NOTIFY + [
                     "@see:REMIND PAYMENT",
                     "click notifyPaymentBtn",
                     "@see:Payment Notified",
                 ] + _W_PAY_CLOSE),
        ],
    },
}

# The rehearsal (see _C_FULL_EVENT_HOST_PREORDER_EVE): flow_full_event with only its
# two pre-order segments on the after-15:00 menu.
FLOWS["flow_full_event_eve"] = {
    **FLOWS["flow_full_event"],
    "id": "flow_full_event_eve",
    "name": "[Rehearsal] Full event on the evening menu (pre-orders swapped)",
    "description": "flow_full_event for a run after 15:00, when the restaurant's pre-order "
                   "menu has none of its Phase 1 dishes: the host and participant pre-order "
                   "evening dishes; every later segment is the real flow's.",
    "segments": [dict(seg, steps=list(_C_FULL_EVENT_HOST_PREORDER_EVE)) if seg["num"] == "1"
                 else dict(seg, steps=list(_P_FULL_EVENT_ACCEPT_PREORDER_EVE))
                 if seg["num"] == "2" else seg
                 for seg in FLOWS["flow_full_event"]["segments"]],
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
        {"step": "@waiter_board", "help": "Waiter: go Home to the bookings board"},
        {"step": "@assign_table", "help": "Assign a table on the Select A Table sheet"},
        {"step": "@ensure_order_items", "help": "Add items only if the order is empty"},
        {"step": "@kitchen_ready", "help": "Mark a queued order Ready"},
        {"step": "@select_all_items", "help": "Select every item on the order (Select All)"},
        {"step": "@send_to_kitchen", "help": "Tap SEND and confirm the order went to the kitchen"},
        {"step": "@order_later", "help": "After booking: order later (nothing to do from the Wallet)"},
        {"step": "@pre_order", "help": "After booking: open the menu to pre-order"},
        {"step": "@consumer_epay", "help": "After Checkout: E-Payment → Stripe 'Pay €' → confirmed"},
        {"step": "@consumer_checkout", "help": "Wallet: this booking's card → scroll → Checkout"},
        {"step": "@leave_review", "help": "Wallet Finished tab: rate + review a finished order"},
        {"step": "@decline_appointment", "help": "Wallet: DECLINE this run's pending invitation (by slot)"},
        {"step": "@wallet_event_shows:2 hr", "help": "Wallet: this run's event (TODAY, booked slot) prints the text"},
        {"step": "@duration:not_sure", "help": "Slide the duration bar to Not Sure (or @duration:1/2/3)"},
        {"step": "@invite_guests:3", "help": "Reservation form: Persons + → add N guests → Invite"},
        {"step": "@c_add:Pizza Margheritas*2[large, veg]",
         "help": "C-App menu: add a NAMED product, N units, with these variants / modifiers"},
        {"step": "@open_event", "help": "Waiter: open THIS run's booking at any stage (reserved … payment)"},
        {"step": "@w_qty:Guest 5/Pizza Salamees +1",
         "help": "Waiter, before SEND: a profile's quantity of a dish +N / -N / remove"},
        {"step": "@w_split:Pizza Salamees@Guest 5 > Guest 4",
         "help": "Waiter: swipe the owner's line of a dish → SPLIT with the people named"},
        {"step": "@w_void:Penne Pollo@Guest 1*all",
         "help": "Waiter: swipe the owner's line → VOID (Entry Error) N units, or all"},
        {"step": "@w_move:Pizza Margheritas@Guest 2 > Noolu Naga",
         "help": "Waiter, after SEND: move the owner's line to another person (grid → Assign)"},
        {"step": "@w_gsplit:Coca-Cola® Light 1.0l@Noolu Naga > Guest 3",
         "help": "Waiter, after SEND: share the owner's line with others (grid → Split)"},
        {"step": "@w_comp:Pizza Margheritas@Guest 4*1:10",
         "help": "Waiter, after Serve: swipe the owner's line → COMP (Birthday) N units at pct %"},
        {"step": "@c_confirm_order", "help": "C-App, table assigned: the cart's CONFIRM ORDER (no payment)"},
        {"step": "@w_add:Coca-Cola® Light 1.0l*5 > split: Guest 1, Guest 2",
         "help": "Waiter ADD: a NAMED product × N (options in [ ]) → Assign / Split to the named profiles"},
        {"step": "@void_item", "help": "Swipe an order row → VOID → Entry Error → Apply"},
        {"step": "@add_item_for_all", "help": "ADD a dish → ASSIGN / SPLIT → every profile → Assign"},
        {"step": "@split_item", "help": "Swipe an order row → SPLIT → one profile → Apply"},
        {"step": "@comp_item", "help": "Swipe a served row → COMP → Birthday → 10 % → Apply"},
        {"step": "@notify_payment", "help": "NOTIFY PAYMENT → Yes"},
        {"step": "@pay_for_all", "help": "Profile card → Pay For → every profile → Apply"},
        {"step": "@waiter_close_table", "help": "Waiter: open this booking (paid in the C-App) → Close Table"},
        {"step": "@close_table", "help": "Wait for Close Table after payment, close, confirm"},
        {"step": "@pay:epay", "help": "Settle by E-Payment"},
        {"step": "@pay:cash", "help": "Settle by cash"},
        {"step": "@pay:voucher", "help": "Settle by food voucher"},
        {"step": "@filter_orders:Reserved", "help": "My Orders: filter by status (OCR-tap, e.g. Reserved/Order/Serve)"},
        {"step": "@reset_table", "help": "My Bookings: this run's card ⋮ → Reset table → Confirm → Reset"},
        {"step": "@modify_table", "help": "Event screen: header table → Modify Table → another free table → Confirm"},
        {"step": "@edit_event_time", "help": "My Bookings: this run's pending appointment ⋮ → Edit the event → next time → Update"},
        {"step": "@cancel_event", "help": "My Bookings: this run's pending appointment ⋮ → Cancel the event → Confirm"},
        {"step": "@wallet_invite_cancelled", "help": "Wallet: this run's invitation (booked slot) reads 'Event Cancelled'"},
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
        {"step": "@see:Invoice", "help": "Assert text is on screen NOW (idb, then OCR; ~1-12s) — catches toasts"},
    ],
}

_ROLES = ("consumer", "participant", "waiter", "kitchen")
# The roles that drive the Consumer app. The participant is a second diner account
# on its own iPhone (DEFAULT_PARTICIPANT_UDID); every C-App handler acts on the
# phone of the segment that runs it (FlowRunner._consumer_udid).
_CONSUMER_ROLES = ("consumer", "participant")
_PARTICIPANT_WDA_PORT = 8102     # consumer 8100, business 8101: one WDA per sim


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


# Which run holds each simulator. Appium allows ONE session per device, and a
# run's preflight resets the app (Metro location -> terminate). A second run on a
# busy simulator therefore kills the first mid-step: InvalidSessionIdException,
# or "app crashed" on a step that had just passed. Measured both ways -- a Retry
# pressed during a waiter demo, and a dashboard run started while a script
# (scripts/dryrun.py, its own process) was finishing one.
#
# So the claim is an OS file lock per simulator, not an in-process dict: it holds
# across the backend, scripts and agents, and the OS drops it if a process dies,
# so a crash can never leave a simulator locked.
DEVICE_LOCK_DIR = os.path.expanduser("~/.vya-platform/device-locks")
DEVICE_WAIT_TIMEOUT = 20 * 60
_HELD: Dict[str, List[int]] = {}
_HELD_LOCK = threading.Lock()


def _try_lock(udid: str, run_id: str):
    """(fd, None) when claimed, else (None, owner run id)."""
    import fcntl
    os.makedirs(DEVICE_LOCK_DIR, exist_ok=True)
    fd = os.open(os.path.join(DEVICE_LOCK_DIR, f"{udid}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            owner = os.pread(fd, 64, 0).decode(errors="ignore").strip() or "another run"
        except OSError:
            owner = "another run"
        os.close(fd)
        return None, owner
    os.ftruncate(fd, 0)
    os.pwrite(fd, run_id.encode(), 0)
    return fd, None


def _unlock(fds: List[int]) -> None:
    import fcntl
    for fd in fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def acquire_devices(run_id: str, udids, cancel: "threading.Event",
                    on_wait=lambda owner, udid: None,
                    timeout: float = DEVICE_WAIT_TIMEOUT) -> bool:
    """Claim every simulator in *udids* for *run_id*, all or none.

    Blocks while another run (in any process) holds one of them. Returns False
    if cancelled or timed out, with nothing claimed."""
    want = sorted({u for u in udids if u})
    deadline = time.time() + timeout
    announced = set()
    while True:
        fds, busy = [], None
        for u in want:
            fd, owner = _try_lock(u, run_id)
            if fd is None:
                busy = (u, owner)
                break
            fds.append(fd)
        if busy is None:
            with _HELD_LOCK:
                _HELD.setdefault(run_id, []).extend(fds)
            return True
        _unlock(fds)                       # never sit on a partial claim
        if cancel.is_set() or time.time() >= deadline:
            return False
        if busy not in announced:
            announced.add(busy)
            on_wait(busy[1], busy[0])
        cancel.wait(2.0)


def release_devices(run_id: str) -> None:
    with _HELD_LOCK:
        fds = _HELD.pop(run_id, [])
    _unlock(fds)


class FlowRunner:
    """Execute one flow's segments in order, switching device/app/account."""

    def __init__(self, run_id: str, flow: Dict[str, Any],
                 devices: Dict[str, str], credentials: Dict[str, Dict[str, str]],
                 on_event: Optional[Callable[[dict], None]] = None,
                 env: str = "prod", resume: Optional[Dict[str, Any]] = None):
        self.run_id = run_id
        # Resume a failed run where it stopped: segments before `start_at` passed in
        # run `from_run` and are recorded here as carried over, not run again.
        # `booked_slot` is the one piece of state a later segment needs from an
        # earlier one (@open_reservation finds the diner's booking by it).
        self._resume = dict(resume or {})
        self._start_at = int(self._resume.get("start_at") or 0)
        if self._resume.get("booked_slot"):
            self._booked_slot = self._resume["booked_slot"]
        if self._resume.get("booked_ticket"):
            self._booked_ticket = self._resume["booked_ticket"]
        self.flow = flow
        self.devices = devices
        self.credentials = credentials
        # Setup (device lock, app check, WDA sessions) runs before any segment --
        # 103s in a measured run -- and its log events were dropped (the runner is
        # started without a callback), so Live Steps said "No steps recorded yet"
        # while the devices were visibly working. They now land on segment 1.
        self._setup_notes: List[str] = []
        self._setup_stage_at: Optional[Tuple[str, float]] = None
        _sink = on_event or (lambda e: None)

        def _on_event(e: dict) -> None:
            _sink(e)
            if e.get("type") == "log" and self._setup_stage_at is not None:
                self._setup_log(str(e.get("message") or ""))
        self.on_event = _on_event
        # Which app pair to drive: "prod" (old Vya) or "staging" (STG-* apps).
        self.env = env if env in ENV_BUNDLES else "prod"
        self.consumer_bundle = ENV_BUNDLES[self.env]["consumer"]
        self.business_bundle = ENV_BUNDLES[self.env]["business"]
        self._sessions: Dict[str, Any] = {}
        self._runners: Dict[str, ScenarioRunner] = {}
        self._biz_account: Optional[str] = None
        self._biz_metro_ready = False
        self._con_metro_ready: set = set()     # C-App phones pointed at their Metro
        # Set by request_flow_stop() when the user presses Stop. Checked between
        # steps and segments; never kills the thread (see _ACTIVE_FLOW_RUNS).
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- session management --------------------------------------------------
    def _flow_udids(self) -> List[str]:
        """The simulators this flow will open sessions on."""
        return sorted({self._role_udid(seg["role"]) for seg in self.flow.get("segments", [])})

    def _participant_udid(self) -> str:
        return (getattr(self, "devices", None) or {}).get("participant") \
            or DEFAULT_PARTICIPANT_UDID

    def _role_udid(self, role: str) -> str:
        """The simulator a segment of *role* runs on."""
        if role == "participant":
            return self._participant_udid()
        if role == "consumer":
            return (getattr(self, "devices", None) or {}).get("consumer") \
                or DEFAULT_CONSUMER_UDID
        return self._business_udid()

    def _consumer_udid(self) -> str:
        """The C-App phone the CURRENT segment drives: the participant's iPhone in
        a participant segment, else the consumer's. C-App handlers use this, never
        devices['consumer'] directly, so the same steps work for either diner."""
        return self._role_udid("participant" if getattr(self, "_cur_role", "") == "participant"
                               else "consumer")

    def _business_udid(self) -> str:
        # Waiter and kitchen MUST share the iPad (account switch). Kitchen is
        # sometimes misconfigured onto the iPhone — force it to the waiter iPad.
        return self.devices.get("waiter") or DEFAULT_BUSINESS_UDID

    def _session_for(self, role: str) -> ScenarioRunner:
        if role in _CONSUMER_ROLES:
            udid, bundle, wda = (self._role_udid(role), self.consumer_bundle,
                                 _PARTICIPANT_WDA_PORT if role == "participant" else 8100)
            if udid not in self._con_metro_ready and udid not in self._sessions:
                # Only before a session exists: the setup may terminate the app,
                # which would kill a session the prewarm already attached.
                # Point this device's consumer app at its environment's Metro
                # (staging :8084). A simulator it never ran on has no
                # RCT_jsLocation and falls back to :8081 -> "No bundle URL".
                # Both diners' phones share that one Metro.
                if ensure_app_metro(udid, self.consumer_bundle):
                    self._con_metro_ready.add(udid)
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
            apply_flow_speed_settings(d)
            d.activate_app(bundle)
            self._wait_app_ready(d, bundle, udid=udid)   # poll instead of a blind sleep(10)
            # Debug builds stack LogBox warnings over the UI; clear them once here
            # so the first real step doesn't tap into an overlay.
            # CONSUMER ONLY: the dismiss control's coordinates were verified on the
            # phone. The iPad's layout differs and an unverified tap there is a way
            # to break a segment that otherwise works, so leave the business app to
            # its own (already working) handling.
            self._cur_udid = udid
            if role in _CONSUMER_ROLES:
                self._clear_logbox(udid)
            self._sessions[udid] = d
            self._runners[udid] = ScenarioRunner(d, bundle, screenshot_dir=None)
        return self._runners[udid]

    @staticmethod
    def _wait_app_ready(d, bundle: str, timeout: float = 12.0, udid: str = "") -> None:
        """Return as soon as the app is foreground AND has rendered something tappable,
        instead of blindly waiting 10s. Debug builds fetch the JS bundle on launch, so
        the first render can lag — but it's usually ~1-2s, not 10.

        With a udid the render check is one idb screen read (~1s) rather than an
        Appium predicate over the whole tree (~5s a poll); Appium stays the fallback."""
        from appium.webdriver.common.appiumby import AppiumBy
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if udid:
                    # idb only. An empty read here means a busy device, not "no idb":
                    # falling through to the Appium whole-tree predicate below cost
                    # 5-45s a poll on this app, for a wait that ends at `timeout`
                    # regardless. Just look again.
                    if d.query_app_state(bundle) == 4:
                        els = _idbd.describe_all(udid)
                        if any(e.get("type") in ("StaticText", "Button")
                               and (e.get("AXLabel") or "").strip() for e in els):
                            time.sleep(0.4)
                            return
                    time.sleep(0.5)
                    continue
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
                raw = _idb_fast.subprocess_run([_IDB, "ui", "describe-all", "--udid", udid],
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
        # FAST PATH: idb, verified taps -- Menu, then Log Out, then wait for the
        # sign-in screen. (The note below that "an idb tap does NOT navigate" dates
        # from the landscape-iPad rotation bug: every idb tap then landed ~240pt off.
        # With the rotation measured, the tap lands.) ~6s against ~40s of Appium
        # lookups, including five probes for a confirm dialog this build never shows.
        udid = self._business_udid()
        ok_m, how_m = _idbd.tap(udid, ["menuBtn", "menuTab", "Menu"])
        trail.append(f"idb menu tap: {ok_m} ({how_m})")
        if ok_m:
            ok_l = False
            for _ in range(8):                       # the Menu screen renders in ~1s
                time.sleep(0.6)
                ok_l, how_l = _idbd.tap(udid, ["logOutBtn"])
                if ok_l:
                    trail.append("idb logOutBtn tapped")
                    break
            if ok_l:
                for _ in range(12):
                    time.sleep(1.0)
                    if self._biz_role_state() == "login":
                        trail.append("reached the sign-in screen (idb)")
                        return
                trail.append("idb logout did not reach sign-in -- Appium fallback")
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

        def _note(m: str) -> None:
            if notes is not None:
                notes.append(m)

        if not user or not pw:
            from automation.scenarios.cross_app_config import _ENV_SEED
            missing = " and ".join(w for w, v in (("email", user), ("password", pw)) if not v)
            _note(f"[FAIL] no {account} {missing} configured — enter it under Automation › "
                  f"Run Cross-App Suite › {account.title()}, or set "
                  f"{_ENV_SEED[account][0 if not user else 1]} in .env and restart")
            return False

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
                # idb first: type + read back EXACTLY (secure field: bullet count), and
                # Return closes the keyboard -- InputField has no submit handler, so it
                # only blurs (App/Components/InputField). ~5s a field against ~20s.
                udid = self._business_udid()
                _t_type = time.time()
                typed = []
                for field, value, label in (("emailValue", user, "email"),
                                            ("passwordValue", pw, "password")):
                    info: Dict[str, Any] = {}
                    ok, _ = _idbd.fill(udid, field, value, info=info)
                    if not ok:
                        # Only THIS field through Appium (slow, ~20s) -- the other
                        # one is already right and must not be retyped.
                        _fill_field(r.d, field, value)
                        try:
                            r.d.hide_keyboard()
                        except Exception:
                            pass
                        typed.append(f"{label}: Appium fallback")
                    elif info.get("skipped"):
                        typed.append(f"{label}: already filled")
                    else:
                        n = info.get("attempts", 1)
                        typed.append(f"{label}: {n} {'try' if n == 1 else 'tries'}")
                _note(f"    · typed the {account} credentials ({', '.join(typed)}; "
                      f"{time.time() - _t_type:.1f}s)")
                # The T&C box is REQUIRED — Sign In stays disabled without it. It is
                # also reset every time the app bounces back to the sign-in screen,
                # so re-tick each attempt.
                ticked = self._tick_tc_idb(udid, _note)
                if not ticked:
                    self._tick_tc_checkbox(r, _note)
                ok_s, _ = _idbd.tap(udid, ["signInBtn"])
                if not ok_s:
                    btns = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "signInBtn")
                    if btns:
                        btns[0].click()

                # 3) VERIFY the CORRECT role's home appears (the shared login resolves waiter vs
                #    kitchen server-side from the account). Poll for it — never assume success.
                for _ in range(24):                  # ~24s
                    time.sleep(1)
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
        # A 'Not Sure' (or 2/3 hr) booking skips the dialog and lands on the Wallet:
        # the pre-order prompt is for 1 hr bookings only (Reservation.js, measured
        # 2026-10-01: booking MEPHSR, 13:00, 'Any .Not Sure', straight to Wallet).
        WALLET = {"walletUpcomingSearchInput", "upcomingBlock"}

        def dialog_open() -> bool:
            # Must go through idb. These are GenericElements, which Appium's
            # collapsed snapshot never reports — checking via r._resolve() would
            # return False no matter how well the tap worked, so the step could
            # never confirm its own success.
            labels = {e.get("label") for e in self._idb_els()}
            if labels & WALLET:
                self._booked_to_wallet = True
                return True
            return bool(labels & MARKERS)

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
                if not getattr(self, "_touched_since", None):
                    self._touched_since = time.time()      # see _own_panel_card
                self._idb_tap(btn["cx"], btn["cy"])
            elif attempt == 1:
                notes.append("[FAIL] @book_appointment — BOOK NOW is not on screen "
                             "(polled ~24s). On screen: "
                             f"{[e['label'] or e['id'] for e in self._idb_els() if e['label'] or e['id']][:16]}")
                return False
            # Give the dialog a moment; the app re-renders after committing the slot.
            # By the CLOCK, not a count: a read while the app submits the booking can
            # take many seconds, so "5 polls" had no fixed length.
            #
            # And never re-tap while the booking may be in flight. MEASURED
            # 2026-10-17 (flow3): tap 1 "did not open the dialog", tap 2 went on,
            # and the step took 220s. If BOOK NOW has left the screen, the form is
            # submitting: keep waiting rather than risk a second booking.
            # MEASURED 2026-10-17 (flow3 220s, book demo 126s): BOOK NOW left the
            # form and the Wallet came up 1.5-2 min later -- staging was slow to
            # create the booking. That is a wait, not a lost tap.
            t_tap = time.time()
            confirmed, submitting, form_back = False, False, False
            deadline = time.time() + 12.0
            while time.time() < deadline:
                time.sleep(1.5)
                if dialog_open():
                    confirmed = True
                    break
                if submitting:
                    # A failed request puts the form back (Reservation.js
                    # handleReservation .catch -> loader off): stop waiting then, do
                    # not idle out the 150s (review 2026-10-19).
                    if any(e.get("label") == "bookAppoitment" for e in self._idb_els()):
                        form_back = True
                        break
                    continue
                now_els = self._idb_els() if time.time() >= deadline else []
                if now_els and not any(e.get("label") == "bookAppoitment" for e in now_els):
                    submitting, deadline = True, time.time() + 150.0
                    notes.append("    · @book_appointment — BOOK NOW accepted; the app is "
                                 "creating the booking (waiting for the server)")
            if form_back:
                notes.append(f"[FAIL] @book_appointment — the booking request failed: the "
                             f"form came back {time.time() - t_tap:.0f}s after BOOK NOW "
                             f"(staging error?). Not tapping again: the server may still "
                             f"have made the booking")
                return False
            if confirmed and submitting:
                notes.append(f"    · @book_appointment — the server took "
                             f"{time.time() - t_tap:.0f}s to confirm the booking")
            if confirmed:
                notes.append(f"[ok] @book_appointment — booking confirmed"
                             + (" — the app went straight to the Wallet (no pre-order "
                                "prompt for this duration)"
                                if getattr(self, "_booked_to_wallet", False) else "")
                             + f"{' (retry %d)' % attempt if attempt > 1 else ''}")
                return True
            if submitting:
                notes.append(f"[FAIL] @book_appointment — BOOK NOW was accepted but the "
                             f"booking was not confirmed within {time.time() - t_tap:.0f}s "
                             f"(staging slow or down?); not tapping again, to avoid a "
                             f"second booking")
                return False
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

    def _wait_form_closed(self, form_open, window: float = 30.0) -> bool:
        """Wait for the New Appointment form to close after Save. True once closed.

        Polling with form_open() re-read the WHOLE iPad screen each time: 2.6-5.7s a
        read with the form up, plus a 1s sleep, 16 times -- @save_appointment took
        127s on a save that had already gone through. Instead, hit-test the single
        point Save sits at (describe-point, 0.04-0.2s): while that point still
        answers 'saveBtn' the form is open. The moment it does not, ONE full read
        confirms -- a toast passing over the button must not count as closed.
        Falls back to the full read every round if the button cannot be located."""
        udid = getattr(self, "_cur_udid", "") or self.devices.get("business") or ""
        els = self._idb_els(udid)
        btn = next((e for e in els if (e["label"] or "").strip() == "saveBtn"), None)
        deadline = time.time() + window
        while time.time() < deadline:
            time.sleep(0.5)
            if btn is not None and udid:
                try:
                    px, py = _idbd.to_device(udid, btn["cx"], btn["y"] + 9)
                    if _idbd.name(_idbd.describe_point(udid, px, py)) == "saveBtn":
                        continue                      # still exactly where it was: open
                except Exception:
                    pass
            if not form_open():
                return True
        return False

    def _save_appointment(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Tap Save on the New Appointment form and CONFIRM the form closed.

        `click saveBtn` on its own reported [ok] while the form stayed open and no booking was
        created — Appium found the element and dispatched a click, which says nothing about
        whether the app accepted it. The form staying up IS the failure signal, so poll for it
        to disappear and retry. A LogBox toast sits along the bottom edge, right where Save is,
        and can swallow the tap, so clear it before each try."""
        FORM_MARKERS = ("saveBtn", "New Appointment", "firstName", "closeEventModal")

        def form_open() -> bool:
            els = self._idb_els()            # ONE screen read (was two per check)
            labels = {(e["label"] or "").strip() for e in els}
            labels |= {(e["id"] or "").strip() for e in els}
            return any(m in labels for m in FORM_MARKERS)

        for attempt in range(1, 4):
            # r.dismiss_logbox() was here. It cannot see this toast at all — a
            # GenericElement, which Appium's collapsed tree never reports. _clear_logbox()
            # (idb) does see it, so use that; but do not RELY on it, because tapping the
            # strip's right edge does not close it on the iPad (see _tap_save_btn).
            self._clear_logbox()
            if not getattr(self, "_touched_since", None):
                self._touched_since = time.time()          # see _own_panel_card
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
            if self._wait_form_closed(form_open):
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

    def _tick_tc_idb(self, udid: str, _note) -> bool:
        """_tick_tc_checkbox through idb: same geometry, same pixel proof, no Appium.

        The row 'clickCheckBox' spans the whole sentence; the square is its
        leftmost non-white pixel (10pt in on the iPhone, ~32pt on the iPad), and a
        tick shifts the square's colour from near-white to purple (measured
        (240,242,245) -> (199,189,206)). One simulator screenshot per reading
        instead of Appium's, and no 7-9s is_keyboard_shown: the fill closed it."""
        els = _idbd.describe_all(udid)
        row = next((e for e in els if _idbd.name(e) == "clickCheckBox"), None)
        if row is None:
            _note("    · no T&C checkbox on screen (already agreed?)")
            return False
        if not _idbd.hittable(udid, row):
            return False
        x, y, w, h = _idbd.frame(row)
        cy = y + h / 2
        scan = [(x + i, cy) for i in range(0, int(min(w, 80)), 2)]
        cols = _idbd.sample_colors(udid, scan, els, radius=0.5)
        dx0 = next((i * 2 + int(h * 0.35) for i, c in enumerate(cols)
                    if c and sum(c) < 720), None)
        for dx in ([max(2, dx0)] if dx0 is not None else []) + [10, 32, 6, 14, 22, 3]:
            pt = (x + dx, cy)
            before = _idbd.sample_colors(udid, [pt], els, radius=h * 0.35)[0]
            px, py = _idbd.to_device(udid, *pt)
            _idbd._idb(["ui", "tap", "--udid", udid, str(px), str(py)])
            time.sleep(0.6)
            after = _idbd.sample_colors(udid, [pt], els, radius=h * 0.35)[0]
            if before and after and sum(abs(a - b) for a, b in zip(after, before)) > 30:
                _note(f"    · ticked the Terms & Conditions checkbox "
                      f"(idb tap at x+{dx}, verified {before}->{after})")
                return True
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
        creds = self.credentials.get(account, {})
        if creds.get("email") and creds.get("password"):
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

    @staticmethod
    def _raw_el(e: dict) -> dict:
        """An _idb_els() dict back in idb's raw shape (for idb_driver checks)."""
        return {"AXLabel": e.get("label") or "", "AXIdentifier": e.get("id") or "",
                "type": e.get("type") or "",
                "frame": {"x": e.get("x", 0), "y": e.get("y", 0),
                          "width": e.get("w", 0), "height": e.get("h", 0)}}

    def _idb_els(self, udid: str = "") -> List[dict]:
        """Fast full-screen snapshot via idb (~1-2s) as a flat list of element dicts —
        replaces the expensive Appium IOS_PREDICATE traversals on this app's huge tree.
        'map the screen once, resolve everything locally' instead of one traversal per
        element. Each dict: id, label, type, x/y/w/h and centre cx/cy (points)."""
        import json as _json
        import subprocess as _sp
        udid = udid or getattr(self, "_cur_udid", "") or self._consumer_udid()
        try:
            # 30s, not 15: on the busy iPad a read measured 14.6s, the next one ran
            # past 15s, came back empty, and @save_appointment fell through to
            # Appium -- 29s to find saveBtn + 79s to click it (2026-10-06). A slow
            # idb read is still far cheaper than any Appium fallback on this tree.
            raw = _idb_fast.subprocess_run([_IDB, "ui", "describe-all", "--udid", udid],
                          capture_output=True, text=True, timeout=30).stdout
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

    def _click_sidebar_row(self, slot: str, statuses: tuple, notes: List[str],
                           max_pages: int = 8, where: str = "events list") -> bool:
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

        udid = self._business_udid()

        def matching(els):
            out = []
            for e in els:
                lbl = _idbd.name(e)
                m = _SIDEBAR_ROW_RE.search(lbl)
                if not m:
                    continue
                start = m.group(0).split("-")[0].strip()
                if f"{int(start.split(':')[0]):02d}:{start.split(':')[1]}" != want_hhmm:
                    continue
                if statuses and not _status_ok(lbl, statuses):
                    continue
                out.append(e)
            return out

        # The list is not sorted by start time (measured: 4770 16:05, 4772 16:15,
        # 4771 16:15, 4769, 4768 ... and the 16:35 booking further DOWN), so a late
        # booking sits below the visible part of the sheet. Two cases:
        #  * rendered but clipped -- idb still reports a frame for it, and a tap
        #    at that frame lands on nothing (measured: "tapped the 16:35 row ...
        #    the reservation did not open"). tap_el scrolls it into view slowly,
        #    confirms it under the point, then taps.
        #  * not rendered yet (a virtualised list only renders near the viewport):
        #    scroll the list down a page and look again, until it stops moving.
        seen_before = None
        for page in range(max_pages):
            els = _idbd.describe_all(udid)
            rows = matching(els)
            if rows:
                if len(rows) > 1:
                    # Staging is shared: another diner's booking can start at this exact
                    # time (review 2026-10-19). A row reads '4657 <icon> RESERVED 13:45 -
                    # 14:45 <icon> 13:38' -- its ticket first, its updatedAt last.
                    mine = getattr(self, "_booked_ticket", "")
                    own = [e for e in rows if mine and _idbd.name(e).split()[:1] == [mine]]
                    if own:
                        notes.append(f"    · {len(rows)} {where} rows start at {want_hhmm}; "
                                     f"opening this run's ticket {mine}")
                        rows = own
                    else:
                        def changed_at(lbl: str) -> str:
                            times = re.findall(r"\b\d{1,2}:\d{2}\b", lbl)
                            return times[-1] if len(times) >= 3 else ""
                        cands = [((_idbd.name(e).split() or [""])[0], "", e,
                                  [(changed_at(_idbd.name(e)),)]) for e in rows]
                        rows = [self._own_panel_card(cands, want_hhmm, notes, where)[2]]
                e = rows[0]
                notes.append(f"    · opening the {want_hhmm} booking from the {where}: "
                             f"{_idbd.name(e)[:60]!r}"
                             + (f" (found after scrolling the list {page}x)" if page else ""))
                ok, how = _idbd.tap_el(udid, e, els)
                if ok:
                    if "scrolled" in how:
                        notes.append(f"    · scrolled the {where} to bring the "
                                     f"{want_hhmm} row on screen")
                    self._remember_ticket(_idbd.name(e).split()[0] if _idbd.name(e) else "", notes)
                    self._remember_status(_idbd.name(e))
                    return True
                # Too far down to reach in a few slow drags: page the list down and
                # try again (bounded below by the page count and "list stopped").
                notes.append(f"    · the {want_hhmm} row is in the list but not reachable "
                             f"yet ({how}) — scrolling the {where} down")
            all_rows = [e for e in els if _SIDEBAR_ROW_RE.search(_idbd.name(e))]
            if not all_rows:
                return False
            labels = tuple(_idbd.name(e) for e in all_rows)
            if labels == seen_before:
                return False                       # the list did not move: its end
            seen_before = labels
            w, h = _idbd.app_size(els)
            col_x = _idbd.centre(all_rows[0])[0]
            # Finger travels UP (content moves up, later rows come into view), slowly
            # enough to move the list 1:1 rather than fling past the row.
            _idbd.swipe(udid, col_x, h * 0.78, col_x, h * 0.42, 1.6)
            time.sleep(0.5)
        return False

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

    def _window_size(self, r) -> Tuple[float, float]:
        """The business session's window (width, height) in points, (0, 0) if unknown.

        Cached per Appium SESSION (its id is never reused, unlike id() of a freed
        driver): the size does not change within a session, and an unbounded
        get_window_size() is one of the Appium calls that froze @open_order (see
        _bounded_call). Falls back to idb's app size when WDA does not answer."""
        key = getattr(getattr(r, "d", None), "session_id", None) or id(getattr(r, "d", None))
        cache = self.__dict__.setdefault("_win_size_cache", {})
        if key not in cache:
            size = _bounded_call(lambda: r.d.get_window_size(), 15.0, None) or {}
            w, h = float(size.get("width") or 0.0), float(size.get("height") or 0.0)
            if not (w and h):
                try:
                    w, h = (float(v or 0.0) for v in
                            _idbd.app_size(_idbd.describe_all(self._business_udid())))
                except Exception:
                    w, h = 0.0, 0.0
            if not (w and h):
                return 0.0, 0.0                  # don't cache a miss
            cache[key] = (w, h)
        return cache[key]

    def _window_h(self, r) -> float:
        return self._window_size(r)[1]

    def _window_w(self, r) -> float:
        return self._window_size(r)[0]

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
        _late = object()                          # an Appium call that did not answer
        # FASTEST: a slow idb drag moves the list 1:1 (measured: 200pt drag in >=1s
        # -> 190pt of travel; faster drags fling unpredictably), so drag by exactly
        # the distance to the target hour. ~1-2s a drag, no Appium round-trips.
        target = getattr(self, "_scroll_target_hour", "")
        if target and before.get(target) is not None:
            udid = self._business_udid()
            h_app = _idbd.app_size(_idbd.describe_all(udid))[1] or 834.0
            want = before[target] - 0.4 * h_app
            if abs(want) > 60:
                travel = max(-0.6 * h_app, min(0.6 * h_app, want))
                y_from = 0.8 * h_app if travel > 0 else 0.2 * h_app
                _idbd.swipe(udid, 600, y_from, 600, y_from - travel,
                            max(1.0, abs(travel) / 200.0))
                time.sleep(0.5)
                after = hour_positions()
                if any(after.get(k) != v for k, v in before.items() if k in after):
                    return True
        # Prefer a DIRECT DRAG to a stepwise swipe. 'mobile: swipe' costs a measured
        # 9.3s per call here (13.7s including the scans around it) and moves a fixed
        # ~590pt, so walking from 00:00 to 15:00 is six swipes and ~82s -- a third of
        # the step's whole 240s budget spent scrolling. The rows are 100pt apart and
        # idb reports live positions, so the exact distance is known: one drag covers
        # it. Falls through to the swipe loop if the drag does not move the list.
        h0 = self._window_h(r) or 834.0
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
            # A timed-out gesture still runs later (Appium queues it): send nothing
            # more, or the late drag and the next swipe both land and overshoot the
            # booked hour (review 2026-10-19).
            if _bounded_call(lambda: r.d.execute_script("mobile: dragFromToForDuration", {
                    "fromX": x, "fromY": y_from, "toX": x, "toY": y_to,
                    "duration": 0.6}), 20.0, None, late=_late) is _late:
                return False
            time.sleep(1.0)
            after0 = hour_positions()
            if any(after0.get(k) != v for k, v in before.items() if k in after0):
                return True
        h = self._window_h(r)
        on_screen = [lbl for lbl, y in before.items()
                     if not h or 0.1 * h <= y <= 0.9 * h]
        if not on_screen:
            return False
        for lbl in on_screen[:3]:
            try:
                els = _bounded_call(lambda: r.d.find_elements(
                    AppiumBy.IOS_PREDICATE, f'label == "{lbl}"'), 20.0, [], late=_late)
                if els is _late:
                    return False                  # WDA busy: queue nothing behind it
                if not els:
                    continue
                # An hour label resolves to TWO elements (the text and its row
                # container), and the first is not necessarily the one on screen.
                # Swiping an off-screen anchor is a silent no-op -- the same trap
                # _pick_swipe_surface documents for the horizontal rows -- so choose
                # the match whose own rect is really inside the viewport.
                anchor = None
                for e in els:
                    rect = _bounded_call(lambda: e.rect, 10.0, None, late=_late)
                    if rect is _late:
                        return False
                    if not rect:
                        continue
                    ey = (rect.get("y") or 0) + (rect.get("height") or 0) / 2
                    if not h or 0.1 * h <= ey <= 0.9 * h:
                        anchor = e
                        break
                if anchor is None:
                    continue
                if _bounded_call(lambda: r.d.execute_script(
                        "mobile: swipe", {"direction": direction, "element": anchor.id}),
                        20.0, None, late=_late) is _late:
                    return False
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
        udid = udid or getattr(self, "_cur_udid", "") or self._consumer_udid()
        x, y = self._rotate_for_device(x, y, udid)
        try:
            _idb_fast.subprocess_run([_IDB, "ui", "tap", "--udid", udid, str(int(x)), str(int(y))], timeout=10)
            return True
        except Exception:
            return False

    def _rotate_for_device(self, x, y, udid: str):
        """Map an app-space point (describe-all) to the DEVICE space `idb ui tap`
        expects. On the landscape iPad they differ, and in a direction that
        depends on how the simulator was rotated -- idb_coords measures it."""
        from automation.scenarios.idb_coords import to_device
        return to_device(udid, x, y)

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

    def _add_items_sheet_products(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Add products from the waiter's ADD NEW ITEM sheet. True if any were added.

        This sheet has no quantity steppers. Each product is a TouchableOpacity
        labelled `${product.name}Item`, spaces stripped, whose onPress calls
        addNewItem(product) directly (Screens/Event/AddNewItem.js:89) -- one tap per
        product, no '+' to find.

        The CATEGORY headers on the same sheet are labelled the same way
        (AddNewItem.js:178 -- 'PASTA Item', 'PIZZA Item'), so they have to be
        excluded or the step 'adds' a heading and nothing lands in the order. They
        are distinguishable: a category keeps its space before 'Item' because it is
        stripped of non-ASCII rather than whitespace.
        """
        MARKERS = ("addNewItemClose", "addNewItemInput", "addNewItemAll")
        seen = {e["label"].strip() for e in self._idb_els()} | \
               {e["id"].strip() for e in self._idb_els()}
        if not any(m in seen for m in MARKERS):
            return False

        # CATEGORY CHIPS LOOK EXACTLY LIKE PRODUCTS.
        #
        # AddNewItem.js labels both `${...}Item`, so the suffix cannot separate them.
        # Most categories keep a space ('PASTA Item') but not all -- MEASURED on this
        # restaurant, 'biriyaniItem' is a CATEGORY, and matching it tapped a filter
        # chip while the step reported adding a product and the order stayed at 0.
        #
        # The layout does separate them: the chips are one horizontal row (all at
        # y=200, x=190..702) and the products a column beneath (x=97, y=321+). So
        # take the products' own column -- the x shared by the most 'Item' elements
        # -- and keep only the rows in it.
        cand = [(( e.get("label") or "").strip() or (e.get("id") or "").strip(),
                 e.get("x") or 0, e.get("y") or 0)
                for e in self._idb_els()]
        cand = [c for c in cand if c[0].endswith("Item")]
        if not cand:
            products = []
        else:
            xs = {}
            for _nm, x, _y in cand:
                xs[x] = xs.get(x, 0) + 1
            col = max(xs, key=lambda k: (xs[k], -k))     # the busiest column
            products = []
            for nm, x, _y in sorted(cand, key=lambda c: c[2]):
                if x == col and nm not in products:
                    products.append(nm)
        if not products:
            notes.append("    · @add_all_products — ADD NEW ITEM sheet is open but holds "
                         "no products")
            return False

        added = 0
        for nm in products[:2]:        # a couple is enough to send to the kitchen
            if not self._appium_click_id(r, nm):
                continue
            time.sleep(2.0)
            # TAPPING A PRODUCT DOES NOT ADD IT. handleProductFeature routes a
            # product carrying features/modifiers to the 'Add Options' sheet
            # (Screens/Event/index.js:843) and only a plain one straight to the
            # order. MEASURED: PennePolloItem opened a sheet with 'Add Options',
            # 'Special Instructions' and applyOptionBtn -- so without Apply the item
            # never lands and the order stays at 0 EUR while the step reports
            # success. Apply when the sheet appears; a plain product shows none.
            for _ in range(8):
                if self._appium_click_id(r, "applyOptionBtn", secs=10.0):
                    time.sleep(2.0)
                    break
                if self._add_items_sheet_up():
                    break                     # plain product: already added
                time.sleep(1.0)
            added += 1
        if not added:
            return False
        notes.append(f"    · @add_all_products — added {added} product(s) from the ADD "
                     f"NEW ITEM sheet: " + ", ".join(n[:-4] for n in products[:added]))
        # COMMIT, then close.
        #
        # Apply only stages a product: handleAddingProducts pushes it onto
        # addedProductsList (Screens/Event/index.js:912), which is NOT the order.
        # 'assignToBtn' on the sheet itself (AddNewItem.js:212) is what assigns the
        # staged products -- it is disabled while addedProductsList is empty, which
        # is exactly why it has to be tapped BEFORE the sheet is closed. Closing
        # first threw the staging list away, and the order stayed at 0 EUR while the
        # step reported adding items.
        #
        # 'addNewItemAll' is NOT a commit button either -- it is the 'All' CATEGORY
        # FILTER chip (AddNewItem.js:155), and tapping it just re-filters the list.
        if self._appium_click_id(r, "assignToBtn"):
            time.sleep(2.5)
            # The assign sheet ("Assign to or split among…") asks WHO the products
            # are for. Each diner row is `${username}select` with spaces stripped
            # (Components/Modal/index.js:1647) -- 'RoopaDselect' -- so there is no
            # fixed id to click; match the suffix. MEASURED: the sheet held
            # RoopaDselect, RoopaDclose, unSelect, addNewGuest and assignProductsBtn,
            # and a guessed 'selectAll' matched nothing, leaving the products
            # assigned to no one.
            for _ in range(10):
                if any(m in {(e.get("label") or "").strip() for e in self._idb_els()}
                       for m in ("assignProductsBtn",)):
                    break
                time.sleep(1.0)
            picked = False
            for e in self._idb_els():
                nm = (e.get("label") or "").strip()
                if nm.endswith("select") and nm not in ("unSelect",):
                    if self._appium_click_id(r, nm, secs=15.0):
                        picked = True
                        time.sleep(1.5)
                    break
            if not picked:
                notes.append("    · @add_all_products — no diner row on the assign sheet")
            # TWO buttons share this id, side by side (Components/Modal:1731 and
            # :1756): the FIRST is 'Assign', the SECOND is 'Split'. Split is
            # disabled unless 2+ diners are selected, so clicking the last match --
            # which _appium_click_id does by default -- pressed a dead button and
            # the sheet never closed. Take the leftmost, which is Assign.
            if not self._appium_click_leftmost(r, "assignProductsBtn"):
                notes.append("    · @add_all_products — could not press Assign")
            else:
                time.sleep(3.0)
        else:
            notes.append("    · @add_all_products — no assignToBtn; products may stay "
                         "staged and never reach the order")
        # Leave the sheet if it is still up.
        if self._add_items_sheet_up():
            self._appium_click_id(r, "addNewItemClose", secs=15.0)
            time.sleep(2.0)
        return True

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
            # THE 'ADD NEW ITEM' SHEET IS A DIFFERENT SCREEN.
            #
            # _steppers looks for a '- 0 +' control whose id ends 'Inc'. The waiter's
            # Add-New-Item sheet (Screens/Event/AddNewItem.js:89) has no steppers at
            # all: each product is a TouchableOpacity labelled `${name}Item` whose
            # onPress calls addNewItem(product) directly. MEASURED on this build: 0
            # ids ending 'Inc', 39 ending 'Item' -- so the wait timed out and the
            # step reported "no menu products" on a sheet full of them, which is the
            # 'Nylai Kitchen2 has no products' conclusion this step kept producing.
            if self._add_items_sheet_products(r, notes):
                return True
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

    def _c_confirm_order(self, notes: List[str]) -> bool:
        """`@c_confirm_order` — a diner's order DURING the event (the table assigned):
        the cart's CONFIRM ORDER sends it to the restaurant, no payment now.

        Cart/index.js:892-1012: with `visited_restaurant` the cart shows CONFIRM ORDER
        (text only, no id) and `cartAddItems`, not `cartCheckout`; it posts
        makeReservation?query=checkout and shows 'Order Confirmed' with
        `orderConfirmedMenu` (Modal/index.js:2887) -- unless the diner once ticked
        "don't show", then nothing. Done when the cart left the screen."""
        tag = "@c_confirm_order"
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        name = _idbd.name
        is_btn = lambda e: _norm(name(e)) == "confirmorder"      # noqa: E731
        els = _idbd.describe_all(udid)
        if not any(is_btn(e) for e in els):
            # The menu search's keyboard can still cover the cart bar (MEASURED
            # 2026-10-29: 'cartImage: not on screen' right after an add).
            if not self._has(els, "cartImage"):
                _idbd.press_return(udid)
                els, _ = self._wait_els(udid, lambda es: self._has(es, "cartImage"), 6.0)
            ok, how = _idbd.tap(udid, ["cartImage"], els)
            if not ok:
                notes.append(f"[FAIL] {tag} — no cart to open (cartImage: {how})")
                return False
            els, _ = self._wait_els(udid, lambda es: any(is_btn(e) for e in es)
                                    or self._has(es, "cartCheckout"), 8.0)
        if self._has(els, "cartCheckout") and not any(is_btn(e) for e in els):
            notes.append(f"[FAIL] {tag} — the cart offers Checkout, not CONFIRM ORDER: the "
                         f"table is not assigned yet (this is a pre-order cart)")
            return False
        btn = next((e for e in els if is_btn(e)), None)
        if btn is not None:
            ok, how = _idbd.tap_el(udid, btn, els)
        else:
            w, h = _idbd.app_size(els)
            ok, how = self._ocr_tap(udid, (0, h * 0.5, w, h), "CONFIRM ORDER")
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap CONFIRM ORDER ({how})")
            return False
        els, done = self._wait_els(udid, lambda es: self._has(es, "orderConfirmedMenu")
                                   or not any(is_btn(e) for e in es), 12.0)
        if self._has(els, "orderConfirmedMenu"):
            _idbd.tap(udid, ["orderConfirmedMenu"], els)
            self._wait_els(udid, lambda es: not self._has(es, "orderConfirmedMenu"), 6.0)
            notes.append(f"[ok] {tag} — CONFIRM ORDER sent the order: 'Order Confirmed'")
            return True
        if not done:
            toast = self._toast(els) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — tapped CONFIRM ORDER but the cart stayed"
                         + (f" ({toast!r})" if toast else ""))
            return False
        notes.append(f"[ok] {tag} — CONFIRM ORDER sent the order (the cart closed)")
        return True

    # `@c_add:<Product>*<qty>[option, option]`, e.g. `@c_add:Pizza Margheritas*2[large, veg]`.
    _C_ADD_RE = re.compile(r"^\s*(?P<name>[^*\[\]]+?)\s*(?:\*\s*(?P<qty>\d+))?\s*"
                           r"(?:\[(?P<opts>[^\]]*)\])?\s*$")

    @staticmethod
    def _squash(text: str) -> str:
        """An id the apps build from a name: `.replace(/\\s+/g, '')`."""
        return re.sub(r"\s+", "", text or "")

    @staticmethod
    def _search_text(product: str) -> str:
        """What to TYPE into a product search: the name up to its first non-ASCII
        character. `idb ui text` mangles one -- MEASURED 2026-10-29: 'Coca-Cola®
        Light 1.0l' landed as 'Coca-Colaight 1.0l' and matched nothing. Both apps'
        searches match a part of the name, and the exact product is then picked by
        its id among the results."""
        head = re.match(r"[\x20-\x7e]*", product or "").group(0).strip()
        return head if len(head) >= 3 else (product or "")

    @staticmethod
    def _tile_count(els: List[dict], pid: str) -> Optional[int]:
        """The quantity a menu tile shows: the number between its `<pid>Dec` and
        `<pid>Inc` (three separate elements, MEASURED 2026-10-29: 'PennePolloDec'
        x13, '1' x92, 'PennePolloInc' x132 on one row). None when not on screen."""
        name, frame = _idbd.name, _idbd.frame
        dec = next((e for e in els if name(e) == pid + "Dec"), None)
        inc = next((e for e in els if name(e) == pid + "Inc"), None)
        if dec is None or inc is None:
            return None
        dx, dy, dw, dh = frame(dec)
        for e in els:
            if (name(e) or "").isdigit():
                x, y, w, h = frame(e)
                if dx + dw <= x + w / 2 <= frame(inc)[0] and dy <= y + h / 2 <= dy + dh:
                    return int(name(e))
        return None

    _SEARCH_ICON = ""      # the menu bar's search glyph (AntDesign 'search1'), no id
    _SEARCH_CLEAR = ""     # the search field's clear X, once something is typed

    def _menu_search(self, udid: str, product: str, els: List[dict]) -> List[dict]:
        """Type *product*'s exact name into the menu's own search and return the
        screen after it. The user's procedure (2026-10-29): search, don't scroll.

        The search icon has no id: it is the TabBar's 'search1' glyph
        (Components/TabSectionList/TabBar.js:137), on the bar that sticks to the top
        once the list scrolls past the restaurant header -- MEASURED: '\\ue670' at
        x347 y45 after a 450pt drag, absent at the top. It opens SearchProducts
        (searchList.js:415, a TextInput 'Search for an item') listing
        `name.includes(text)` with the same tiles (`<pid>Inc`)."""
        name, frame = _idbd.name, _idbd.frame
        w, h = _idbd.app_size(els)

        def field(es):
            return next((e for e in es if e.get("type") == "TextField"
                         and frame(e)[1] < h * 0.2), None)

        if field(els) is None:
            icon = next((e for e in els if name(e) == self._SEARCH_ICON
                         and frame(e)[1] < h * 0.2), None)
            if icon is None:
                _idbd.swipe(udid, w / 2, h * 0.75, w / 2, h * 0.75 - 450, 2.2)
                time.sleep(0.8)
                els = _idbd.describe_all(udid)
                icon = next((e for e in els if name(e) == self._SEARCH_ICON
                             and frame(e)[1] < h * 0.2), None)
            if icon is None:
                return els
            _idbd.tap_el(udid, icon, els, scroll=False)
            els, _ = self._wait_els(udid, field, 5.0)
        if field(els) is None:
            return els
        clear = next((e for e in els if name(e) == self._SEARCH_CLEAR), None)
        if clear is not None:                     # the last product's name is still in
            _idbd.tap_el(udid, clear, els, scroll=False)
            time.sleep(0.6)
            els = _idbd.describe_all(udid)
        f = field(els)
        if f is None:
            return els
        _idbd.tap_el(udid, f, els, scroll=False)
        time.sleep(0.6)
        _idbd._idb(["ui", "text", "--udid", udid, self._search_text(product)])
        els, _ = self._wait_els(udid, lambda es: any(name(e) == self._squash(product) + "Inc"
                                                     for e in es), 6.0)
        return els

    def _find_menu_tile(self, udid: str, pid: str,
                        product: str = "") -> Tuple[List[dict], Optional[dict]]:
        """(els, the tile's `<pid>Inc`) on the C-App menu, or (els, None).

        The menu's own search first (_menu_search). Failing that, scroll: the menu
        renders only the tiles near the viewport -- MEASURED 2026-10-29: 4 of 41
        tiles in the tree, while every section header ('DRINKS 🥤' y≈2886) is. So:
        each category chip (`pasta-category`, `biriyanicategory`: Products.js
        category tabs) with a few slow drags inside its section; then a slow drag
        through the whole list. Drags are slow (≤200 pt/s): a fast swipe flings past
        a whole section (idb_driver _bring_into_view)."""
        name, frame = _idbd.name, _idbd.frame
        want = pid + "Inc"
        find = lambda es: next((e for e in es if name(e) == want), None)  # noqa: E731
        els = _idbd.describe_all(udid)
        if find(els) is not None:
            return els, find(els)
        els = self._menu_search(udid, product, els) if product else els
        if find(els) is not None:
            return els, find(els)
        w, h = _idbd.app_size(els)

        def drag() -> List[dict]:
            # Between the two tile columns, so a drag never lands on a '+'.
            _idbd.swipe(udid, w / 2, h * 0.75, w / 2, h * 0.75 - 350, 1.8)
            time.sleep(0.6)
            return _idbd.describe_all(udid)

        def chips_on(es) -> List[dict]:
            return sorted((e for e in es if name(e).endswith("category")
                           and 0 <= frame(e)[0] + frame(e)[2] / 2 <= w
                           and 0 < frame(e)[1] < h * 0.6), key=lambda e: frame(e)[0])

        def to_top() -> List[dict]:
            # The chips scroll WITH the list (MEASURED: 'biriyanicategory' at y -5156
            # at the end), so they can be tapped only from the top: fling back up.
            es = _idbd.describe_all(udid)
            for _ in range(10):
                if chips_on(es):
                    return es
                _idbd.swipe(udid, w / 2, h * 0.3, w / 2, h * 0.85, 0.2)
                time.sleep(0.5)
                es = _idbd.describe_all(udid)
            return es

        # The chip this product was found under before (same menu on both phones).
        known = getattr(self, "_menu_chip", {}).get(pid)
        order = sorted((name(c) for c in chips_on(to_top())), key=lambda n: n != known)
        for chip_name in order:
            els = to_top()
            chip = next((e for e in chips_on(els) if name(e) == chip_name), None)
            if chip is None:
                continue
            _idbd.tap_el(udid, chip, els, scroll=False)
            time.sleep(1.2)
            els = _idbd.describe_all(udid)
            for _ in range(4):
                if find(els) is not None:
                    self._menu_chip = {**getattr(self, "_menu_chip", {}), pid: chip_name}
                    return els, find(els)
                els = drag()
        els = to_top()
        for _ in range(24):
            if find(els) is not None:
                return els, find(els)
            els = drag()
        return els, find(els)

    def _clear_of_bars(self, udid: str, els: List[dict], e: dict) -> Tuple[List[dict], dict]:
        """Drag the menu until *e* sits in the clear middle of the screen.

        The floating 'Checkout (n)' bar (`cartImage`, y≈778-826 on the phone) covers
        the bottom tiles: MEASURED 2026-10-29 (run 74b912e8) a '+' tapped under it
        opened the Cart instead of the product."""
        w, h = _idbd.app_size(els)
        bar = next((x for x in els if _idbd.name(x) == "cartImage"), None)
        low = (_idbd.frame(bar)[1] if bar else h * 0.88) - 30
        high = h * 0.3
        for _ in range(3):
            cy = _idbd.frame(e)[1] + _idbd.frame(e)[3] / 2
            if high <= cy <= low:
                return els, e
            shift = cy - h / 2
            y0 = h * 0.7 if shift > 0 else h * 0.35
            _idbd.swipe(udid, w / 2, y0, w / 2, y0 - shift, max(1.0, abs(shift) / 200.0))
            time.sleep(0.6)
            els = _idbd.describe_all(udid)
            e = next((x for x in els if _idbd.name(x) == _idbd.name(e)), e)
        return els, e

    def _c_add_product(self, spec: str, notes: List[str]) -> bool:
        """`@c_add:<Product>*<qty>[option, …]` — put ONE line in the C-App cart: a
        NAMED product, *qty* units, with these variants / modifiers.

        RestaurantProduct.onChangeQty (Components/Cards/Block/RestaurantProduct.js:235)
        opens the options sheet on the FIRST '+' of every product, while a later '+'
        opens a features confirmation instead of counting. So the tile is tapped
        once -- its name button `${name}` or `${name}Inc` (spaces stripped,
        RestaurantProduct.js:726, CustomCounter.js:123) -- and the quantity is set
        INSIDE the sheet with `counterPlus` (BottomUpdate.js:781, 1..stock), next to
        the options: rows `${option}Btn ${price}` (spaces stripped, a space, the
        price; BottomUpdate.js:427 variants, :507/:633 modifiers). `confirmProduct`
        (:794) adds them as one line, and does nothing until every required /
        compulsory group is satisfied -- a sheet that stays open says which. Done when
        the sheet is gone and the tile counts *qty* more."""
        tag = f"@c_add:{spec}"
        m = self._C_ADD_RE.match(spec or "")
        if not m:
            notes.append(f"[FAIL] {tag} — write it as <Product>*<qty>[option, option]")
            return False
        product, qty = m.group("name").strip(), int(m.group("qty") or 1)
        opts = [o.strip() for o in (m.group("opts") or "").split(",") if o.strip()]
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        name, pid = _idbd.name, self._squash(product)

        # The menu (or its search, left open by the last add) has to be up: the step
        # after @pre_order can catch it loading.
        self._wait_els(udid, lambda es: any(name(e).endswith("category")
                                            or name(e) == "cartImage" for e in es), 10.0)
        els, inc = self._find_menu_tile(udid, pid, product)
        if inc is None:
            near = sorted({name(e)[:-3] for e in els if name(e).endswith("Inc")})[:12]
            menu_says = any("not on the menu" in (name(e) or "").lower() for e in els)
            notes.append(f"[FAIL] {tag} — {product!r} is not on the menu"
                         + (" at this booking's time (the app's search says \"That's not on "
                            "the menu\": the restaurant's menus change by the time of day)"
                            if menu_says else
                            f" (no {pid + 'Inc'!r} in any category; tiles last seen: "
                            f"{', '.join(near) or 'none'})"))
            return False
        els, inc = self._clear_of_bars(udid, els, inc)
        before = self._tile_count(els, pid) or 0
        ok, how = _idbd.tap_el(udid, inc, els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap {product!r}'s + ({how})")
            return False
        # Already in the cart: '+' asks first ('Custom Selection', NewModal/index.js:845);
        # its ADD NEW CUSTOM SELECTION (`addNewCustomSelection`) opens the options
        # sheet for a NEW line (hadleFeaturesConformation 'choose').
        # A cart still holding another booking's items asks to empty it first
        # (AlertPrevCart, Modal/index.js:3719): `deletePrevYes` empties it and goes on
        # with this '+' (RestaurantProduct.handlePrevStore). On this shared test
        # account that cart is an earlier run's leftover.
        els, up = self._wait_els(udid, lambda es: self._has(es, "confirmProduct")
                                 or self._has(es, "addNewCustomSelection")
                                 or self._has(es, "deletePrevYes"), 8.0)
        if up and self._has(els, "deletePrevYes"):
            _idbd.tap(udid, ["deletePrevYes"], els)
            notes.append(f"    · {tag}: the cart held another booking's items — emptied it")
            els, up = self._wait_els(udid, lambda es: self._has(es, "confirmProduct")
                                     or self._has(es, "addNewCustomSelection"), 8.0)
        if up and not self._has(els, "confirmProduct"):
            _idbd.tap(udid, ["addNewCustomSelection"], els)
        els, sheet = self._wait_els(udid, lambda es: self._has(es, "confirmProduct"), 8.0)
        if not sheet:
            toast = self._toast(els)
            notes.append(f"[FAIL] {tag} — tapped {product!r} but its options sheet did not "
                         f"open" + (f" ({toast!r})" if toast else ""))
            return False

        def option_rows(es):
            return [e for e in es if re.search(r"Btn -?\d", name(e) or "")]

        for opt in opts:
            want = self._squash(opt).lower()
            row = next((e for e in option_rows(els)
                        if name(e).split("Btn ", 1)[0].lower() == want), None)
            if row is None:
                offered = ", ".join(sorted({name(e).split("Btn ", 1)[0]
                                            for e in option_rows(els)})) or "none"
                notes.append(f"[FAIL] {tag} — the sheet has no option {opt!r} "
                             f"(it offers: {offered})")
                return False
            ok, how = _idbd.tap_el(udid, row, els)
            if not ok:
                notes.append(f"[FAIL] {tag} — could not tap option {opt!r} ({how})")
                return False
            time.sleep(0.5)
            els = _idbd.describe_all(udid)
        # The sheet slides in: a '+' tapped while it moves is lost (MEASURED 2026-10-29:
        # 'Penne Pollo*2' landed as 1). Settle, then confirm each '+' on the sheet's own
        # count -- the number between counterMinus and counterPlus.
        def sheet_count(es) -> Optional[int]:
            lo = next((e for e in es if name(e) == "counterMinus"), None)
            hi = next((e for e in es if name(e) == "counterPlus"), None)
            if lo is None or hi is None:
                return None
            cy = _idbd.frame(hi)[1] + _idbd.frame(hi)[3] / 2
            for e in es:
                x, y, w, h = _idbd.frame(e)
                if (name(e) or "").isdigit() and _idbd.frame(lo)[0] < x + w / 2 < _idbd.frame(hi)[0] \
                        and abs(y + h / 2 - cy) < 20:
                    return int(name(e))
            return None

        if qty > 1:
            time.sleep(0.8)
            els = _idbd.describe_all(udid)
        for _ in range(qty - 1):
            n = sheet_count(els) or 1
            for _try in range(2):
                ok, how = _idbd.tap(udid, ["counterPlus"], els)
                if not ok:
                    notes.append(f"[FAIL] {tag} — could not raise the quantity ({how})")
                    return False
                els, up = self._wait_els(udid, lambda es: (sheet_count(es) or 0) > n, 3.0)
                if up or sheet_count(els) is None:
                    break
        els = _idbd.describe_all(udid)
        price = next((name(e) for e in els if "€" in (name(e) or "")
                      and "cart" in (name(e) or "").lower()), "")
        ok, how = _idbd.tap(udid, ["confirmProduct"], els)
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap Add to cart ({how})")
            return False
        els, closed = self._wait_els(udid, lambda es: not self._has(es, "confirmProduct"), 6.0)
        if not closed:
            groups = ", ".join(sorted({name(e).split("Btn ", 1)[0]
                                       for e in option_rows(els)})) or "none"
            notes.append(f"[FAIL] {tag} — Add to cart did nothing: a required option is not "
                         f"chosen (picked: {', '.join(opts) or 'none'}; the sheet offers "
                         f"{groups})")
            return False
        after = self._tile_count(els, pid)
        if after is not None and after < before + qty:
            notes.append(f"[FAIL] {tag} — the sheet closed but the tile reads {after}, "
                         f"expected {before + qty}")
            return False
        self._c_cart_lines = getattr(self, "_c_cart_lines", []) + [
            {"product": product, "qty": qty, "options": opts, "sheet": price}]
        notes.append(f"[ok] {tag} — added {qty} × {product}"
                     + (f" ({', '.join(opts)})" if opts else "")
                     + (f"; the sheet read {price!r}" if price else "")
                     + (f"; tile {before} → {after}" if after is not None else ""))
        return True

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

    def _assign_table_idb(self, notes: List[str], table: str = "") -> bool:
        """Assign a table through idb when the table sheet is already up.

        The sheet reads: heading 'Select a table' (or 'Modify Table'), a row of
        chips named after the free tables (I2, I3 ... -- the booked ones are not
        offered), and Confirm (applyTableBtn / AssignTableBtn), which stays pale
        until a chip is picked. So: tap the chip, SEE Confirm turn dark (its
        enabled state is visible only in its colour), tap Confirm, and wait for
        the sheet to close. False means "not decided here" -- the full Appium
        routine below then runs as before. ~8s against the 53-75s it measured."""
        udid = self._business_udid()
        els = _idbd.describe_all(udid)
        name, frame = _idbd.name, _idbd.frame
        head = next((e for e in els if _norm(name(e)) in ("selectatable", "modifytable")
                     and e.get("type") == "StaticText"), None)
        commit = next((e for e in els if name(e) in ("applyTableBtn", "AssignTableBtn")), None)
        if head is None or commit is None:
            return False
        top, bottom = frame(head)[1], frame(commit)[1]
        chip_re = re.compile(r"(?:tableChip\w+|T\d+AssignAnyBtn|[A-Za-z]{1,2}\d{1,3})\Z")
        chips = [e for e in els if chip_re.fullmatch(name(e))
                 and top < frame(e)[1] < bottom and frame(e)[2] > 0]
        if not chips:
            return False
        chips.sort(key=lambda e: frame(e)[0])
        pick = next((c for c in chips if table and name(c).endswith(table)), chips[0])
        ok, _how = _idbd.tap_el(udid, pick, els, scroll=False)
        if not ok:
            return False
        time.sleep(0.6)
        cx, cy, cw, ch = frame(commit)
        if _idbd.is_dark(_idbd.sample_colors(udid, [(cx + 14, cy + ch / 2)])[0]) is not True:
            return False                            # Confirm still pale: not selected
        ok, _how = _idbd.tap_el(udid, commit, scroll=False)
        if not ok:
            return False
        for _ in range(12):
            time.sleep(1.0)
            if not self._table_sheet_open():
                notes.append(f"[ok] @assign_table — assigned '{name(pick)}' and the sheet "
                             f"closed (idb)")
                return True
        return False

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

        # One ceiling for the whole id sweep: five whole-tree finds on this app can
        # each hang, and this runs inside @open_reservation's bounded search
        # (review 2026-10-19). None of the first three ids exists in the business
        # source; 'Close'/'Cancel' are kept for a build that labels its text.
        def _close_by_id() -> bool:
            for ident in ("closeBtn", "modalCloseBtn", "cancelBtn", "Close", "Cancel"):
                try:
                    els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
                    if els:
                        els[0].click(); time.sleep(1.2)
                        if not modal_up():
                            return True
                except Exception:
                    pass
            return False

        if _bounded_call(_close_by_id, 15.0, False):
            return True
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
        if self._relaunch_business(notes, "table sheet"):
            time.sleep(6)
        return not modal_up()

    def _relaunch_business(self, notes: List[str], tag: str) -> bool:
        """Terminate and launch the business app with xcrun simctl, not Appium.

        A timed-out Appium terminate_app/activate_app is not cancelled: Appium
        queues each session's commands, so a call abandoned while WDA was wedged
        ran later and killed the app in the middle of a later step (review
        2026-10-19). simctl runs outside the session, and a subprocess timeout
        kills the child -- nothing can fire late. ensure_business_metro uses the
        same simctl terminate."""
        import subprocess as _sp
        udid = self._business_udid()
        try:
            _sp.run(["xcrun", "simctl", "terminate", udid, self.business_bundle],
                    capture_output=True, timeout=20)       # non-zero when not running: fine
            time.sleep(1.5)
            res = _sp.run(["xcrun", "simctl", "launch", udid, self.business_bundle],
                          capture_output=True, text=True, timeout=30)
        except Exception as e:
            notes.append(f"    · {tag}: could not relaunch the business app "
                         f"({type(e).__name__})")
            return False
        if res.returncode != 0:
            notes.append(f"    · {tag}: could not relaunch the business app "
                         f"({(res.stderr or res.stdout or '').strip()[:90]})")
            return False
        return True

    def _appium_click_id(self, r: ScenarioRunner, ident: str, secs: float = 30.0) -> bool:
        """Click by accessibility id through Appium, which SCROLLS the element into
        view first. Use this wherever a control can sit under the LogBox toasts that
        a debug build stacks along the bottom edge -- a coordinate tap there lands on
        the toast and silently does nothing. Bounded so a slow WDA cannot wedge the
        step."""
        import concurrent.futures as _fut

        def _do():
            els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
            if not els:
                return False
            els[-1].click()
            return True

        ex = _fut.ThreadPoolExecutor(max_workers=1)
        try:
            return bool(ex.submit(_do).result(timeout=secs))
        except Exception:
            return False
        finally:
            ex.shutdown(wait=False)

    def _appium_click_leftmost(self, r: ScenarioRunner, ident: str,
                               secs: float = 20.0) -> bool:
        """Click the LEFTMOST element with this id.

        Some sheets render two controls under one accessibility id -- the assign
        sheet's 'Assign' and 'Split' both answer to assignProductsBtn
        (Components/Modal/index.js:1731 and :1756). Split is disabled unless two or
        more diners are selected, so a default last-match click presses a dead
        button and nothing happens. Position is what tells them apart.
        """
        import concurrent.futures as _fut

        def _do():
            els = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, ident)
            if not els:
                return False
            best, best_x = None, None
            for e in els:
                try:
                    if not e.is_enabled():
                        continue
                    x = (e.rect or {}).get("x")
                except Exception:
                    continue
                if x is not None and (best_x is None or x < best_x):
                    best, best_x = e, x
            if best is None:
                return False
            best.click()
            return True

        ex = _fut.ThreadPoolExecutor(max_workers=1)
        try:
            return bool(ex.submit(_do).result(timeout=secs))
        except Exception:
            return False
        finally:
            ex.shutdown(wait=False)

    def _add_items_sheet_up(self) -> bool:
        """Is the ADD NEW ITEM sheet showing? Its own controls, not its products —
        the list arrives from the backend a beat later."""
        els = self._idb_els()
        seen = {(e.get("label") or "").strip() for e in els} | \
               {(e.get("id") or "").strip() for e in els}
        return bool(seen & {"addNewItemClose", "addNewItemInput", "addNewItemAll"})

    def _order_has_items(self, r: ScenarioRunner, wait: float = 6.0) -> bool:
        """Does the open Order Summary list any items? (Select All or Unselect is
        in its header only then.) Read with idb; Appium only if idb is down."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        names = set(self._SELECT_ALL + self._UNSELECT)
        deadline, empty_since = time.time() + wait, None
        while True:
            els = _idbd.describe_all(udid)
            if not els:
                break
            if any(_idbd.name(e) in names or (e.get("AXLabel") or "").strip() in names
                   for e in els):
                return True
            # The screen is up (Add is shown) with no selector in the header. Add can
            # render before the items load, so only an empty state that HOLDS counts.
            if any(_idbd.name(e) == "addItemsBtn" for e in els):
                empty_since = empty_since or time.time()
                if time.time() - empty_since >= 3.0:
                    return False
            if time.time() >= deadline:
                return False
            time.sleep(0.5)
        return any(r._resolve([i]) for i in self._id_candidates("selectAllItemsBtn"))

    def _add_items_fast(self, notes: List[str], count: int = 2) -> str:
        """ADD -> the first *count* dishes -> ASSIGN / SPLIT -> the first profile ->
        Assign, all through verified idb taps. Returns 'ok' (the dishes reached the
        order), 'untouched' (nothing was tapped), 'retry' (stopped BEFORE Assign;
        the sheet is closed again, which drops anything staged) or 'fail' (Assign
        was pressed but the order does not show the dishes -- retrying would add
        them twice)."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, frame = _idbd.name, _idbd.frame
        els = _idbd.describe_all(udid)
        before = len(self._order_rows(els))
        ok, _how = _idbd.tap(udid, ["addItemsBtn"], els)
        if not ok:
            return "untouched"

        def bail(why: str) -> str:
            notes.append(f"    · @ensure_order_items: fast path stopped ({why}) — "
                         f"using the Appium path")
            if any(name(e) in self._ADD_ITEM_SHEET for e in _idbd.describe_all(udid)):
                _idbd.tap(udid, ["addNewItemClose"])
                self._wait_els(udid, lambda es: not any(
                    name(e) in self._ADD_ITEM_SHEET for e in es), 6.0)
            return "retry"

        els, products = self._wait_els(udid, self._sheet_products, 15.0)
        if not products:
            return bail("the ADD NEW ITEM sheet showed no products")
        added = []
        for p in products[:count]:
            dish = name(p)[:-4]
            cur = next((e for e in self._sheet_products(els) if name(e) == name(p)), p)
            ok, how = _idbd.tap_el(udid, cur, els)
            if not ok:
                return bail(f"could not tap {self._pretty(dish)}: {how}")

            def staged(es, dish=dish):
                return any(name(e) == dish + "card" for e in es)

            els, _ = self._wait_els(udid, lambda es: staged(es)
                                    or self._has(es, "applyOptionBtn"), 8.0)
            if not staged(els) and self._has(els, "applyOptionBtn"):
                _idbd.tap(udid, ["applyOptionBtn"], els)
                els, _ = self._wait_els(udid, staged, 8.0)
            if not staged(els):
                return bail(f"{self._pretty(dish)} did not land in the Summary")
            added.append(dish)
        ok, how = _idbd.tap(udid, ["assignToBtn"])
        if not ok:
            return bail(f"ASSIGN / SPLIT: {how}")
        els, up = self._wait_els(udid, lambda es: self._profiles(es)
                                 and self._has(es, "assignProductsBtn"), 10.0)
        if not up:
            return bail("the assign dialog did not open")
        scratch: List[str] = []
        els, picked = self._select_profiles(udid, els, "one", "@ensure_order_items", scratch)
        if picked is None:
            return bail(scratch[-1].split("— ", 1)[-1] if scratch else "no profile")
        btns = sorted((e for e in els if name(e) == "assignProductsBtn"),
                      key=lambda e: frame(e)[0])
        ok, how = _idbd.tap_el(udid, btns[0], els, scroll=False)   # Assign, not Split
        if not ok:
            return bail(f"Assign: {how}")
        els, done = self._wait_els(
            udid, lambda es: not any(name(e) in self._ADD_ITEM_SHEET for e in es)
            and len(self._order_rows(es)) >= before + len(added), 15.0)
        if not done:
            if any(name(e) in self._ADD_ITEM_SHEET for e in els):
                return bail("the sheet stayed open after Assign")
            toast = self._toast(els)
            notes.append(f"[FAIL] @ensure_order_items — assigned "
                         f"{', '.join(self._pretty(d) for d in added)} but the order shows "
                         f"{len(self._order_rows(els))} row(s)"
                         + (f" ({toast!r})" if toast else ""))
            return "fail"
        notes.append(f"    · @ensure_order_items: added "
                     f"{', '.join(self._pretty(d) for d in added)} for {picked[0]} (idb)")
        return "ok"

    def _ensure_order_items(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """On the opened Order Summary: if the booking was PRE-ORDERED, items are already
        there — do nothing. If it's an order-later booking with an EMPTY order, ADD items
        first so there's something to send to the kitchen. (Per the flow: 'add items if there
        is no pre-order one'.) Real ids: selectAllItemsBtn only renders once the order has
        products; addItemsBtn opens the menu; then assign the added products."""
        # Pre-ordered items present? The header's Select All / Unselect renders only
        # when the order has items. It used to look for 'selectAllItemsBtn' alone --
        # the PHONE id -- so on the iPad a pre-ordered order read as empty and got
        # items added on top, after a slow Appium miss on the whole tree.
        if self._order_has_items(r):
            notes.append("[ok] @ensure_order_items — pre-ordered items present; no add needed")
            return True
        # FAST PATH: verified idb taps end to end. MEASURED: the Appium path below
        # averaged 97s over 16 runs (an Appium find + click per product, a 10s probe
        # for the options sheet, Appium for the diner and Assign); the same work
        # through idb took 27s in @add_item_for_all. 'untouched' = it never touched
        # the screen (e.g. addItemsBtn under a LogBox toast -- the verified tap
        # refuses it), 'retry' = it stopped before Assign and closed the sheet:
        # either way the Appium path below starts from a clean order screen.
        fast = self._add_items_fast(notes)
        if fast == "ok":
            notes.append("[ok] @ensure_order_items — no pre-order; added items to the order")
            return True
        if fast == "fail":
            return False
        # Empty order → add items.
        if r._resolve(["addItemsBtn"]):
            # APPIUM CLICK, not a coordinate tap. A LogBox toast sits along the
            # bottom edge, which is exactly where addItemsBtn renders -- MEASURED:
            # button centre (442, 735), toast spanning y=708..756, so the tap lands
            # on the toast and the ADD NEW ITEM sheet never opens. The step then
            # reported adding items while the order stayed empty at 0 EUR. Appium's
            # .click() scrolls the element clear of the toast first, the same fix the
            # time chips needed.
            if not self._appium_click_id(r, "addItemsBtn"):
                r.run_one("click addItemsBtn", 0)
            for _ in range(10):                          # the sheet fetches its menu
                time.sleep(1.5)
                if self._add_items_sheet_up():
                    break
            self._add_all_products(r, notes)            # add a couple of products (idb)
            udid = getattr(self, "_cur_udid", "") or self._business_udid()
            for bid in ("assignToBtn", "selectAll", "assignProductsBtn"):   # commit them to the order
                ok, why = _idbd.tap(udid, [bid])             # verified idb tap, ~1s
                if not ok and why != "not on screen" and r._resolve([bid]):
                    r.run_one(f"click {bid}", 0)             # present but not verifiable
                time.sleep(0.8)
            notes.append("[ok] @ensure_order_items — no pre-order; added items to the order")
            return True
        notes.append("[ok] @ensure_order_items — no addItems button (already has items?); continuing")
        return True

    # Controls on the kitchen screen that end in 'Btn' but are NOT products.
    # Kept as a safety net only: products are found by POSITION inside their order
    # card (see _kitchen_cards), which is what actually keeps the nav rail out --
    # this build added `inventoryBtn` to the rail, a name-only blacklist missed it,
    # and the step tapped Inventory as a "product" and left the board.
    _KITCHEN_STATIC = {
        "kitchenAllBtn", "kitchenTableBtn", "kitchenPickupBtn",
        "orderReadyBtn", "orderCloseBtn", "orderPrintBtn",
        "preOrderBtn", "homeBtn", "historyBtn", "menuBtn", "inventoryBtn",
        "allBtn", "tableBtn", "pickupBtn", "orderFilterBtn",
        "addNewEvent", "qrScaner", "screenBackBtn", "walletBackBtn",
    }

    def _kitchen_cards(self, els: List[dict]) -> List[dict]:
        """The order cards on the kitchen board, top-left first.

        Source (Components/KitchenCards): each card shows its ticket number, one
        Radio per product labelled `${name}Btn` with spaces removed (only for
        products with modifiers), then a button row -- orderPrintBtn on the left
        and orderReadyBtn, or orderCloseBtn once every product is completed.

        A card's products are the 'Btn' elements INSIDE it: between its ticket
        number and its button row, within the button row's width. Measured on
        ticket 4772: TagliatellealSalmoneBtn / SpaghettiallaPuttanescaBtn at
        x=155 inside the card (x 153-434); inventoryBtn at x=20 in the rail."""
        name, frame, centre = _idbd.name, _idbd.frame, _idbd.centre
        cards = []
        for b in els:
            kind = {"orderReadyBtn": "ready", "orderCloseBtn": "close"}.get(name(b))
            if not kind:
                continue
            bx, by, bw, bh = frame(b)
            prints = [p for p in els if name(p) == "orderPrintBtn"
                      and abs(frame(p)[1] - by) < 12 and frame(p)[0] < bx]
            x0 = (max(frame(p)[0] for p in prints) if prints else bx - 150) - 16
            x1 = bx + bw + 16
            tickets = [t for t in els if t.get("type") == "StaticText"
                       and re.fullmatch(r"\d{3,6}", name(t))
                       and x0 <= centre(t)[0] <= x1 and frame(t)[1] < by]
            ticket = max(tickets, key=lambda t: frame(t)[1]) if tickets else None
            y0 = frame(ticket)[1] if ticket else 0.0
            radios = [e for e in els
                      if name(e).endswith("Btn") and name(e) not in self._KITCHEN_STATIC
                      and e.get("type") != "StaticText"
                      and x0 <= centre(e)[0] <= x1 and y0 < centre(e)[1] < by]
            radios.sort(key=lambda e: frame(e)[1])
            cards.append({"ticket": name(ticket) if ticket else "", "kind": kind,
                          "button": b, "radios": radios})
        cards.sort(key=lambda c: (frame(c["button"])[1], frame(c["button"])[0]))
        return cards

    def _waiter_board(self, notes: List[str]) -> bool:
        """Bring the waiter to the bookings board (My Bookings + My Orders).

        MEASURED 2026-10-21 (waiter demo, run 91a23db5): the iPad was still on the
        previous flow's order screen (booking 5283) when 'click addNewEvent' ran; the
        button exists on the board only, and the step hung its full 240s."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        on_board = lambda es: any(_idbd.name(e) == "addNewEvent" for e in es)  # noqa: E731
        els = _idbd.describe_all(udid)
        if on_board(els):
            notes.append("[ok] @waiter_board — already on the bookings board")
            return True
        how = "homeBtn not on screen"
        for _ in range(2):
            ok, how = _idbd.tap(udid, ["homeBtn"], els)
            els, up = self._wait_els(udid, on_board, 10.0)
            if up:
                notes.append(f"[ok] @waiter_board — went Home to the bookings board ({how})")
                return True
        notes.append(f"[FAIL] @waiter_board — could not reach the bookings board ({how})")
        return False

    def _kitchen_board(self, udid: str, notes: List[str], wait: float = 15.0) -> List[dict]:
        """The kitchen board's elements, once an order card is on it (or the wait
        runs out). Goes back Home if a previous step left another screen up."""
        deadline, went_home = time.time() + wait, 0
        els: List[dict] = []
        while True:
            els = _idbd.describe_all(udid)
            names = {_idbd.name(e) for e in els}
            if names & {"orderReadyBtn", "orderCloseBtn"} or time.time() >= deadline:
                return els
            if "My Orders" not in names and went_home < 2:
                ok, _ = _idbd.tap(udid, ["homeBtn"], els, scroll=False)
                went_home += 1
                if ok:
                    notes.append("    · @kitchen_ready — not on the kitchen board; went Home")
                time.sleep(1.5)
                continue
            time.sleep(1.0)

    def _kitchen_select_items(self, r: ScenarioRunner, udid: str, card: dict,
                              els: List[dict], notes: List[str]) -> List[str]:
        """Tap every product dot on *card*, then CONFIRM each one filled in.

        Accessibility does not expose the selection (measured: a radio reports the
        same attributes selected or not), but the pixels do -- the dot fills with
        the app's purple. So one screenshot after the taps checks every dot, and a
        dot still empty gets one more tap. Returns the names confirmed selected."""
        name, frame = _idbd.name, _idbd.frame

        def dot(e):
            # The dot is a ~14pt circle at the row's left edge. A name that wraps
            # makes the row taller (measured: 29pt), and "x + h/2" then sampled the
            # dot's right edge -- white -- so a selected dot read as empty.
            x, y, w, h = frame(e)
            return (x + min(h, 14) / 2, y + h / 2)

        # A dot TOGGLES: tapping one that is already selected unselects it. And dots
        # can be LINKED: MEASURED 2026-10-29 (ticket 5568) one tap filled both
        # 'mushroombiryaniBtn' dots, while two 'PizzaMargheritasBtn' lines (other
        # options) filled one each. So the dots are taken one at a time, each read
        # just before: an empty one is tapped, a filled one left alone.
        before = _idbd.sample_colors(udid, [dot(e) for e in card["radios"]], els)
        taps: Dict[str, str] = {}
        rows = list(card["radios"])
        for i in range(len(card["radios"])):
            els[:] = _idbd.describe_all(udid) or els
            cur = self._kitchen_card_by_ticket(els, card) or card
            rows = list(cur["radios"])
            if i >= len(rows):
                break
            e = rows[i]
            if _idbd.is_dark(_idbd.sample_colors(udid, [dot(e)], els)[0]):
                taps.setdefault(name(e), "already selected")
                continue
            ok, _how = _idbd.tap_el(udid, e, els, scroll=False)
            taps[name(e)] = _how if ok else f"idb: {_how}; Appium click"
            if not ok:                              # idb could not confirm it: Appium
                try:
                    found = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, name(e))
                    if found:
                        found[0].click()
                except Exception:
                    pass
            time.sleep(0.6)
        els[:] = _idbd.describe_all(udid) or els
        cur = self._kitchen_card_by_ticket(els, card)
        rows = list(cur["radios"]) if cur else rows
        colours = _idbd.sample_colors(udid, [dot(e) for e in rows], els)
        for k, (e, c) in enumerate(zip(rows, colours)):       # one more tap per empty dot
            if _idbd.is_dark(c) is False and \
                    _idbd.is_dark(_idbd.sample_colors(udid, [dot(e)], els)[0]) is False:
                _idbd.tap_el(udid, e, els, scroll=False)
                time.sleep(0.6)
        colours = _idbd.sample_colors(udid, [dot(e) for e in rows], els)
        # What was seen, for the failure note: a bare "none selected" could not be
        # told apart from a missed tap, a misread dot or a row that is not a dot.
        self._kitchen_diag = "; ".join(
            f"{name(e)} at {tuple(int(v) for v in frame(e))}: {b} → {c}, tap {taps.get(name(e), '?')}"
            for e, b, c in zip(rows, before, colours))
        return [name(e) for e, c in zip(rows, colours) if _idbd.is_dark(c) is not False]

    def _kitchen_card_by_ticket(self, els: List[dict], card: dict) -> Optional[dict]:
        cards = self._kitchen_cards(els)
        if card.get("ticket"):
            return next((c for c in cards if c["ticket"] == card["ticket"]), None)
        return cards[0] if cards else None

    def _kitchen_ready(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """The chef's job on one ticket: select every item -> Ready -> Close Order.

        Source (Components/KitchenCards + Screens/Home/kitchen.js):
          * each item with modifiers has a dot (a Radio labelled `${name}Btn`);
          * Ready is disabled until an item is selected --
                disabled={!readyActive(orders?._id)}
            and it marks ONLY the selected items completed (updatePrepare), so
            every dot must be selected first;
          * once all items are completed the button becomes Close Order
            (orderCloseBtn), which closes the ticket and removes it from the board.

        Every tap is an idb tap verified under the point (the old note that these
        buttons "ignore idb taps" dated from the iPad rotation bug -- measured now:
        one idb tap fills the dot and enables Ready). The dots and Ready are
        verified by pixel colour, which is where their state is visible.

        Marking Ready is the assertion; closing is the chef finishing the ticket.
        A run that only closes a leftover ticket has readied nothing and FAILS."""
        udid = self._business_udid()
        name, frame = _idbd.name, _idbd.frame
        els = self._kitchen_board(udid, notes)
        cards = self._kitchen_cards(els)
        queued = [c for c in cards if c["kind"] == "ready"]
        mine = getattr(self, "_booked_ticket", "")
        if mine:
            # This run's order, not whichever ticket is first on the board. It can
            # take a few seconds to arrive after the waiter's SEND.
            for _ in range(10):
                if any(c["ticket"] == mine for c in cards):
                    break
                time.sleep(1.5)
                els = _idbd.describe_all(udid) or els
                cards = self._kitchen_cards(els)
            own = [c for c in cards if c["ticket"] == mine]
            if not own:
                others = ", ".join(c["ticket"] for c in cards if c["ticket"]) or "none"
                notes.append(f"[FAIL] @kitchen_ready — ticket {mine} (this run's order) is "
                             f"not on the kitchen board; tickets there: {others}")
                return False
            queued = [c for c in own if c["kind"] == "ready"]
            if not queued:                       # already prepared: only Close is left
                notes.append(f"    · @kitchen_ready — ticket {mine} is already prepared "
                             f"(all items ready); closing it")
                ok, _how = _idbd.tap_el(udid, own[0]["button"], els)
                notes.append(f"[{'ok' if ok else 'FAIL'}] @kitchen_ready — ticket {mine}: "
                             f"already prepared → Close Order" + ("" if ok else " could not be tapped"))
                return ok
        if not mine and queued:
            # Measured 2026-09-30: bookings with a table but no items sit on the
            # board as empty tickets with a Ready that can never enable (4847, 4846,
            # 4845, 4843), ahead of the one real order. Take a ticket with items.
            with_items = [c for c in queued if c["radios"]]
            if with_items and with_items[0] is not queued[0]:
                notes.append(f"    · @kitchen_ready — skipped {queued.index(with_items[0])} "
                             f"empty ticket(s) (no items to prepare)")
            queued = with_items or queued
        if not queued:
            leftover = next((c for c in cards if c["kind"] == "close"), None)
            if leftover:
                _idbd.tap_el(udid, leftover["button"], els)
                notes.append("[FAIL] @kitchen_ready — nothing to mark Ready; only closed a "
                             f"leftover prepared ticket {leftover['ticket']} (no queued order "
                             "arrived from a waiter)")
                return False
            notes.append("[FAIL] @kitchen_ready — no order on the kitchen board (queue "
                         "empty). This step needs a waiter to have sent an order first — "
                         "run a full cross-app flow, not the standalone kitchen demo.")
            return False

        card = queued[0]
        tag = f"ticket {card['ticket']}" if card["ticket"] else "the first ticket"
        selected: List[str] = []
        for round_ in range(1, 4):
            if card["radios"]:
                got = self._kitchen_select_items(r, udid, card, els, notes)
                if not got:
                    notes.append(f"    · dots: {getattr(self, '_kitchen_diag', '')}")
                    notes.append(f"[FAIL] @kitchen_ready — {tag}: tapped the item dots but "
                                 f"none shows as selected, so Ready stays disabled")
                    return False
                selected += [g for g in got if g not in selected]
            # Ready must be ENABLED now: its background turns from pale to dark
            # purple (accessibility reports enabled=True either way).
            b = card["button"]
            bx, by, bw, bh = frame(b)
            # A long ticket pushes Ready below the screen -- MEASURED 2026-10-29
            # (ticket 5568, ten lines): Ready at y 1008 on an 834-high screen, and its
            # colour was read off-screen ("still disabled"). Drag the board up first.
            _w, H = _idbd.app_size(els)
            if by + bh > H - 20:
                shift = by + bh - H * 0.7
                _idbd.swipe(udid, bx + bw / 2, H * 0.8, bx + bw / 2, H * 0.8 - shift,
                            max(1.0, shift / 200.0))
                time.sleep(0.8)
                els = _idbd.describe_all(udid) or els
                card = self._kitchen_card_by_ticket(els, card) or card
                b = card["button"]
                bx, by, bw, bh = frame(b)
            if _idbd.is_dark(_idbd.sample_colors(udid, [(bx + 14, by + bh / 2)], els)[0]) is False:
                if not card["radios"] and not mine:
                    # Measured 2026-10-17: ticket 5205, the only one queued, showed no
                    # item dot; Ready enables only once a dot is selected
                    # (KitchenCards: disabled={!readyActive(id)}), so nothing can be
                    # readied. "selecting 0 item(s)" read like a broken tap.
                    tickets = ", ".join(c["ticket"] for c in queued if c["ticket"]) or "unnumbered"
                    notes.append(f"[FAIL] @kitchen_ready — no queued ticket has an item to "
                                 f"select ({tickets}), so Ready cannot enable. The kitchen "
                                 "demo needs an order a waiter has sent with items — run a "
                                 "full cross-app flow first.")
                    return False
                notes.append(f"[FAIL] @kitchen_ready — {tag}: Ready is still disabled after "
                             f"selecting {len(selected)} item(s)")
                return False
            ok, _how = _idbd.tap_el(udid, b, els)
            if not ok:
                try:
                    found = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "orderReadyBtn")
                    if found:
                        found[0].click()
                        ok = True
                except Exception:
                    pass
            if not ok:
                notes.append(f"[FAIL] @kitchen_ready — {tag}: could not tap Ready")
                return False
            # The server marks the selected items completed; wait for the card to show it.
            before = len(card["radios"])
            now = None
            for _ in range(15):
                time.sleep(1.0)
                els = _idbd.describe_all(udid) or els
                now = self._kitchen_card_by_ticket(els, card)
                if now is None or now["kind"] == "close" or len(now["radios"]) < before:
                    break
            if now is None:
                notes.append(f"[ok] @kitchen_ready — {tag}: selected {len(selected)} item(s) "
                             f"({', '.join(n[:-3] for n in selected) or 'no modifier rows'}), "
                             f"marked Ready; the ticket left the board")
                return True
            card = now
            if card["kind"] == "close":
                break
            if card["kind"] == "ready" and len(card["radios"]) >= before:
                notes.append(f"[FAIL] @kitchen_ready — {tag}: tapped Ready but no item was "
                             f"marked completed (the card did not change in 15s)")
                return False
        if card["kind"] != "close":
            notes.append(f"[FAIL] @kitchen_ready — {tag}: items still waiting after 3 rounds "
                         f"of select + Ready")
            return False
        items = ", ".join(n[:-3] for n in selected) or "no modifier rows"
        # Close Order: the chef finishes the ticket.
        ok, _how = _idbd.tap_el(udid, card["button"], els)
        closed = False
        if ok:
            for _ in range(12):
                time.sleep(1.0)
                els = _idbd.describe_all(udid) or els
                if self._kitchen_card_by_ticket(els, card) is None or (
                        card["ticket"] and card["ticket"] not in {name(e) for e in els}):
                    closed = True
                    break
        if closed:
            notes.append(f"[ok] @kitchen_ready — {tag}: selected {len(selected)} item(s) "
                         f"({items}) → Ready → Close Order; the ticket left the board")
        else:
            notes.append(f"[ok] @kitchen_ready — {tag}: selected {len(selected)} item(s) "
                         f"({items}) → Ready (all items completed)")
            notes.append(f"    · @kitchen_ready — Close Order did not remove {tag} from "
                         f"the board within 12s")
        return True

    def _hide_keyboard(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Close the on-screen keyboard before tapping a control it may cover.

        On the iPad create-appointment form the keyboard covers 'Any' and Save; a
        tap there lands on a key (a stray 'k' was typed). idb cannot see the
        keyboard at all on iOS 26 (it is a separate process), so nothing here can
        ask "is it up?" -- and Appium's is_keyboard_shown costs 7-9s a call.

        The Return key closes it: measured on the iPad, keyboard gone 0.4s after
        one press, confirmed by screenshot. It is also exactly what Appium's
        `mobile: hideKeyboard` presses ("Done"/"return"), in 2-11s. With no field
        focused the key goes nowhere, so pressing it blind is safe."""
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        _idbd.press_return(udid)
        time.sleep(0.4)
        notes.append("[ok] @hide_keyboard — keyboard closed (return key)")
        return True

    def _slot_into_view_and_tap(self, udid: str, chip: str,
                                tries: int = 12) -> Tuple[bool, str]:
        """Drag the waiter form's horizontal slot row until *chip* is on screen, then
        tap it (verified). idb reports the chip's CONTENT-space x, so the distance
        to drag is known; a slow drag moves the row 1:1 (the calendar's measured
        rule, see _swipe_calendar)."""
        name, frame, centre = _idbd.name, _idbd.frame, _idbd.centre
        moved = 0
        for _ in range(tries):
            els = _idbd.describe_all(udid)
            hits = [e for e in els if name(e) == chip]
            if len(hits) != 1:
                return False, f"{len(hits)} chips named {chip!r}"
            w, _h = _idbd.app_size(els)
            cx, cy = centre(hits[0])
            if not w:
                return False, "no screen size"
            # The row is only visible INSIDE the form. On the iPad the New
            # Appointment form is the right-hand panel (saveBtn measured at x
            # 832..1179, chips at 850/972/1096), so aiming at the middle of the whole
            # screen parked the chip over the bookings board, where the verified
            # tap refuses it (review 2026-10-19). Its Save button spans the form.
            save = next((e for e in els if name(e) == "saveBtn" and frame(e)[2] > 0), None)
            x0, x1 = (frame(save)[0], frame(save)[0] + frame(save)[2]) if save else (0.0, w)
            span, half = x1 - x0, frame(hits[0])[2] / 2
            if x0 + half + 4 <= cx <= x1 - half - 4:
                # scroll=True: the row can also sit below the fold or under the
                # keyboard -- the vertical bring-into-view handles that part.
                ok, how = _idbd.tap(udid, [chip], els)
                if ok:
                    return True, f"idb, dragged the slot row {moved}x"
                return False, f"in view after {moved} drag(s) but {how}"
            # Drag inside the form only, slowly (1:1), toward its middle.
            travel = max(-0.7 * span, min(0.7 * span, cx - (x0 + x1) / 2))
            x_from = x0 + (0.85 if travel > 0 else 0.15) * span
            _idbd.swipe(udid, x_from, cy, x_from - travel, cy, max(1.0, abs(travel) / 200.0))
            moved += 1
            time.sleep(0.5)
            after = [e for e in _idbd.describe_all(udid) if name(e) == chip]
            if after and frame(after[0]) == frame(hits[0]):
                return False, "the slot row did not move"
        return False, f"not in view after {moved} drag(s)"

    def _first_time_slot(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Tap the first available time slot — via idb (fast + bounded), NOT an Appium
        `MATCHES` predicate. That regex predicate traverses AND regex-matches this app's
        huge accessibility tree and can HANG ~50s+ (it wedged a live run). idb dumps the
        tree as JSON in ~1-2s; we find a HH:MM label and tap its centre. If no slot is
        present that's a DATA condition, not an automation failure — never block/hang."""
        import json as _json
        import subprocess as _sp
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()

        def _read_slots():
            """idb-read the tree and collect any HH:MM time chips (label, centre)."""
            try:
                raw = _idb_fast.subprocess_run([_IDB, "ui", "describe-all", "--udid", udid],
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
        #
        # WAIT for them first: they render only once the dining area is chosen and the
        # day's slots have loaded. MEASURED 2026-10-22 (flow6, run 0b9bfc7b): on a slow
        # staging the first read found none, the step scrolled 3x through the Appium
        # resolver (~70s each), and by the end the New Appointment form was gone --
        # the bookings board was on screen.
        _wait_until = time.time() + 12.0
        while not slots and time.time() < _wait_until:
            time.sleep(1.0)
            slots = _read_slots()
        # Then drag the FORM's content up (finger up), slowly, inside the form: on the
        # iPad it is the right-hand panel (its Save spans it), so a swipe in the middle
        # of the screen would move the board instead.
        _scrolls = 0
        while not slots and _scrolls < 3:
            els_f = _idbd.describe_all(udid)
            w_f, h_f = _idbd.app_size(els_f)
            if not (w_f and h_f):
                break
            save = next((e for e in els_f if _idbd.name(e) == "saveBtn"), None)
            x = _idbd.centre(save)[0] if save is not None else w_f / 2
            _idbd.swipe(udid, x, h_f * 0.70, x, h_f * 0.40, 1.5)
            time.sleep(1.0)
            _scrolls += 1
            slots = _read_slots()
        if _scrolls:
            notes.append(f"    · @first_time_slot — scrolled {_scrolls}x to reveal the time slots")
        if not slots:
            names_now = {_idbd.name(e) for e in _idbd.describe_all(udid)}
            if "addNewEvent" in names_now and not names_now & {"saveBtn", "closeEventModal"}:
                notes.append("[FAIL] @first_time_slot — the New Appointment form is no longer "
                             "open (the bookings board is showing), so there is no time chip "
                             "to pick")
                return False
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
            # THE APP'S OPEN-WINDOW GATE RUNS ON THE UTC CLOCK.
            #
            # BookingCard.onPress and AddCountModal.isClickable both do:
            #     moment.utc().hours(getUTCHours()).minutes(getUTCMinutes())
            #         .isSameOrAfter(moment.utc(from_time).subtract(30, 'minutes'))
            #
            # The LEFT side is the UTC wall clock. The RIGHT side is from_time
            # converted to UTC -- AddNewEventModal builds it as
            # moment(`${date} ${selectedTime}`, 'DD MMMM YYYY HH:mm'), a LOCAL moment
            # that serialises with its offset, so moment.utc() shifts it correctly.
            # VERIFIED against the live app: a slot of 11:07 local on a UTC+5:30 host
            # serialises as 2026-09-25T11:07:00+05:30, reads back as 05:37 UTC, and
            # the gate evaluates 05:32 >= 05:07 -> True.
            #
            # Both sides therefore live on the UTC clock, and a window measured
            # against the LOCAL clock is offset by the machine's own timezone. Aim at
            # the UTC clock instead, expressed in the local-time labels the form
            # offers. On a UTC machine the two coincide and nothing changes.
            # timezone-aware: utcnow() is deprecated and slated for removal.
            from datetime import timezone as _tz
            _utc = _dt.now(_tz.utc)
            utc_min = _utc.hour * 60 + _utc.minute
            offset = now_min - utc_min                 # local - UTC, in minutes
            if offset > 720:
                offset -= 1440
            elif offset < -720:
                offset += 1440

            def _openable(slot_min):
                """Minutes until the app's own UTC gate lets this slot be opened."""
                return (slot_min - offset) - utc_min

            window = [s for s in slots if 5 <= _openable(s[0]) <= 28]
            future = [s for s in slots if _openable(s[0]) >= 3]
            chosen = window[0] if window else (future[0] if future else slots[min(1, len(slots) - 1)])
            in_window = chosen in window
            if offset and not window:
                notes.append(f"    · @first_time_slot — no slot inside the app's UTC "
                             f"open window (this Mac is UTC{offset // 60:+d}:"
                             f"{abs(offset) % 60:02d}); the booking may not be openable")
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
            # FIRST: idb, verified -- describe-point must confirm the chip is under
            # the point, and the form is swiped to bring it into view if not. This
            # is exactly the failure the Appium-first order was guarding against
            # (a content-space frame that is really the Save button), now checked
            # instead of avoided. ~2-5s against ~40-56s.
            _ok_slot, _how = _idbd.tap(udid, [f"{safe}Btn", safe])
            if not _ok_slot:
                # The waiter's chips are ONE horizontal row of the whole day, and
                # idb's scroll-into-view is vertical only. MEASURED 2026-10-17 (waiter
                # demo, iPad Pro M5): 12:55 sat far right of the strip, the verified
                # tap refused it, Appium's click failed too, and the step ran past
                # 240s. Drag the row itself, slowly (1:1), until the chip is in view.
                # Only the waiter's chips carry 'Btn'; on the consumer's form the
                # drag finds none, and the first reason is the one to keep.
                _ok2, _how2 = self._slot_into_view_and_tap(udid, f"{safe}Btn")
                if _ok2:
                    _ok_slot, _how = _ok2, _how2
                elif not _how2.startswith("0 chips"):
                    _how = f"{_how}; row drag: {_how2}"
            if _ok_slot:
                tapped = True                    # no fixed pause: the next step waits for its target
                notes.append(f"[ok] @first_time_slot — selected slot '{lbl}' ({_how}, {tag})")
            if not tapped:
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
                size = _bounded_call(lambda: r.d.get_window_size(), 15.0, None) or {}
                _vw = float(size.get("width") or 0.0)
                if _vw and not (0 <= cx <= _vw):
                    notes.append(f"[FAIL] @first_time_slot — slot '{lbl}' is outside the "
                                 f"visible strip (x={cx:.0f} of {_vw:.0f}) and its chip "
                                 f"id did not resolve, so it cannot be tapped reliably")
                    return False
                # The VERIFIED idb tap above already tried this chip (and swiped it
                # into view). A raw coordinate tap now could only land on whatever
                # is drawn over the chip's content-space frame -- measured: the Save
                # button -- so refuse rather than press something else.
                notes.append(f"[FAIL] @first_time_slot — slot '{lbl}' could not be reached: "
                             f"idb could not confirm it under the tap point and Appium "
                             f"could not click it ({_how})")
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
            # The idb path proved the same thing BEFORE tapping: describe-point
            # returned the chip itself (so it was on screen and not under Save).
            if not _ok_slot and not self._time_slot_committed(r, lbl):
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

    def _wait_on_home(self, timeout: float) -> bool:
        """_on_home(), polled until *timeout* -- instead of a fixed sleep then one look.
        Returns as soon as Home is up; at least one look is always taken."""
        deadline = time.time() + timeout
        while True:
            time.sleep(0.3)
            if self._on_home():
                return True
            if time.time() >= deadline:
                return False

    def _wait_gone(self, labels, timeout: float) -> bool:
        """Poll until none of *labels* is on screen (e.g. a dialog just dismissed)."""
        want = set(labels)
        deadline = time.time() + timeout
        while True:
            time.sleep(0.3)
            if not any((e.get("label") or "").strip() in want for e in self._idb_els()):
                return True
            if time.time() >= deadline:
                return False

    def _on_home(self) -> bool:
        """Are we on the Home tab? Checked via idb (sees the full tree; the Appium
        snapshot is depth-capped and misses these)."""
        import subprocess
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        try:
            raw = _idb_fast.subprocess_run([_IDB, "ui", "describe-all", "--udid", udid],
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
                         # Nor is an input: the email field is labelled "loginEmail"
                         # and was "opened" four times instead of being filled.
                         and e.get("type") not in ("StaticText", "TextField",
                                                   "SecureTextField")
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
        """Sign the diner of this segment in (consumer or participant) using the
        run's configured credentials for that role."""
        role = getattr(self, "_cur_role", "")
        role = role if role in _CONSUMER_ROLES else "consumer"
        creds = (self.credentials or {}).get(role) or {}
        email, password = creds.get("email"), creds.get("password")
        if not (email and password):
            notes.append(f"[warn] first run — signed out and no {role} credentials "
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
            # Through _fill_field (clear, type, read back, retype per character):
            # a raw send_keys on a busy simulator dropped the first characters of
            # the email, leaving an invalid address, so SIGN-IN stayed disabled.
            for field, value in ((fields[0], email), (fields[1], password)):
                name = field.get_attribute("name")
                if not (name and _fill_field(r.d, name, value)):
                    field.clear()
                    field.send_keys(value)
            self._hide_keyboard(r, notes)
            btn = next((e for e in self._idb_els()
                        if _norm(e.get("label")) in ("signin", "login", "submit")), None)
            if btn:
                self._idb_tap(btn["cx"], btn["cy"])
            # Report what happened, not what was attempted: still on the form
            # means the sign-in did not go through.
            for _ in range(10):
                time.sleep(1.5)
                if self._on_home():
                    notes.append(f"[ok] first run — signed in as {email}")
                    return True
                # An empty read (idb hiccup, loader mid-transition) proves
                # nothing either way -- only a readable screen without the
                # sign-in form counts as "past sign-in".
                if self._idb_els() and not self._looks_signed_out():
                    notes.append(f"[ok] first run — signed in as {email}")
                    return True
            notes.append(f"[warn] first run — still on the sign-in screen after "
                         f"submitting {email} (check the consumer credentials)")
            return False
        except Exception as e:
            notes.append(f"[warn] first run — sign-in failed: {str(e)[:120]}")
            return False

    _RESTAURANT = "NylaiKitchen2"
    _HOME_MARKERS = ("homeProductsBar", "homeCouponIcon")
    # vya-consumer Screens/Home/index.js: the list's footer button has no id, only
    # I18n 'homePage.loadMoreResturants' = "Load More Restaurants"; the Favourites
    # chip is HomeChips/ScrollableChips.js accessibilityLabel 'favoriteButton'
    # (it scopes Home's list to the diner's favourites; the Favorite tab is
    # commented out in BottomNavigator.js).
    _LOAD_MORE = ("Load More Restaurants",)
    _FAVOURITES = ("favoriteButton",)
    RESTAURANT_SCROLLS = 8           # pages scrolled per search (Home, then Favourites)
    RESTAURANT_BUDGET_S = 150.0      # under STEP_TIMEOUT, so a miss is a reported FAIL
    # vya-consumer Components/Header/index.js 'homeSearchBar' opens
    # Components/Search/SearchModal.js. MEASURED 2026-10-28 on the iPhone: that sheet
    # is ONE accessibility element, 'Search' (its SafeAreaView is accessible={true}),
    # so idb sees neither its input nor its results -- only Home behind it (it is
    # drawn inside Home, coverScreen={false}, and stays open after it opened a
    # restaurant). Its text is read off the screen instead (OCR, ~1.7s a read).
    # The restaurant's display name has a space: 'Nylai Kitchen2' found 1 result;
    # 'NylaiKitchen2' (the Home card's id) found "0 result(s)". Tapping the result
    # opened the booking page in 1.3s (SearchResultCard.js -> 'StoreReservation',
    # the same screen and params as Home's card).
    _SEARCH_BAR = ("homeSearchBar",)
    _SEARCH_SHEET = "Search"
    _SEARCH_TERM = "Nylai Kitchen2"

    def _restaurant_page(self, udid: str, secs: float) -> bool:
        """The booking page is up: its 'Any' area is shown, or Home is gone."""
        end = time.time() + secs
        while True:
            now = {_idbd.name(e) for e in _idbd.describe_all(udid)}
            if now & {"Any", "anyBtn"} or not now & set(self._HOME_MARKERS):
                return True
            if time.time() >= end:
                return False
            time.sleep(0.8)

    def _search_sheet(self, els: List[dict]) -> Optional[Tuple[float, float, float, float]]:
        """The open search sheet's frame, or None."""
        e = next((e for e in els if _idbd.name(e) == self._SEARCH_SHEET
                  and _idbd.frame(e)[3] > 300), None)
        return _idbd.frame(e) if e is not None else None

    def _close_search(self, udid: str) -> None:
        """Close a search sheet left open over Home before Home's list is used: its
        back arrow is an icon (no text to read) at the left of the input row."""
        sheet = self._search_sheet(_idbd.describe_all(udid))
        if sheet is not None:
            _idbd.tap_point(udid, sheet[0] + 22, sheet[1] + 30)
            self._wait_els(udid, lambda es: self._search_sheet(es) is None, 4.0)

    def _search_restaurant(self, udid: str) -> Optional[str]:
        """Home's search bar: type the restaurant's name, tap its result.

        MEASURED 2026-10-27, 12 runs: the card was always behind two scrolls and a
        'Load More Restaurants' (the server's second page), 36-113s to reach (~45s
        typical). By search it took ~8-10s on 2026-10-28."""
        from automation.scenarios import screen_text
        els = _idbd.describe_all(udid)
        sheet = self._search_sheet(els)
        if sheet is None:
            ok, _how = _idbd.tap(udid, self._SEARCH_BAR, els, scroll=False)
            if not ok:
                return None
            els, sheet = self._wait_els(udid, self._search_sheet, 5.0)
            if sheet is None:
                return None
            time.sleep(0.5)                                   # it fades in
        sx, sy, sw, _sh = sheet
        row = [p for p in screen_text.read_text(udid)        # the input: placeholder or old text
               if sy <= p[2] <= sy + 55 and p[1] < sx + sw * 0.8]
        x, y = ((row[0][1] + row[0][3] / 2, row[0][2] + row[0][4] / 2) if row
                else (sx + sw * 0.4, sy + 30))
        _idbd.tap_point(udid, x, y)
        time.sleep(0.4)
        _idbd._idb(["ui", "key-sequence", "--udid", udid,
                    *([_idbd.KEY_DELETE_FORWARD] * 30 + [_idbd.KEY_BACKSPACE] * 30)])
        _idbd._idb(["ui", "text", self._SEARCH_TERM, "--udid", udid])
        want = _norm(self._SEARCH_TERM)
        hit, end = None, time.time() + 10.0
        while hit is None and time.time() < end:
            time.sleep(1.0)
            seen = screen_text.read_text(udid)
            # "0 result(s) found for « … »" -- OCR reads that 0 as an O (measured)
            if any(re.match(r"^[0Oo]\s*result", p[0].strip()) for p in seen):
                return None                                   # the search does not list it
            hit = next((p for p in seen if _norm(p[0]) == want and p[2] > sy + 60), None)
        if hit is None:
            return None
        _idbd.tap_point(udid, hit[1] + hit[3] / 2, hit[2] + hit[4] / 2)
        if not self._restaurant_page(udid, 10.0):
            return None
        return f"searched {self._SEARCH_TERM!r}, tapped its result"

    def _open_restaurant(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Home → the restaurant's booking page: by Home's search first; when that
        does not get there, by its card on Home's list.

        MEASURED 2026-10-27 (flow1, run 6d743962): the card was not on Home's first
        screen (Home's list renders lazily), the verified idb tap found nothing and
        the Appium lookup after it hung the step for 240s. Then (run 744f3e7e) six
        scrolls showed other restaurants but not this one. The user's procedure:
        if the kitchen is not on screen, scroll down; when "Load More Restaurants"
        shows, tap it and look again; if it is still not found, open Favourites
        and search there. Never the Appium full-tree lookup."""
        udid = (getattr(self, "_cur_udid", "") or self._consumer_udid())
        name = self._RESTAURANT
        t0 = time.time()
        by_search = self._search_restaurant(udid)
        if by_search:
            notes.append(f"[ok] click {name} — opened the restaurant ({by_search})")
            return True
        self._close_search(udid)
        searched = f"search did not open it in {time.time() - t0:.0f}s; "
        cands = self._id_candidates(name)
        deadline = t0 + self.RESTAURANT_BUDGET_S          # one budget for both ways

        def swipe(up: bool) -> None:
            w, h = _idbd.app_size(_idbd.describe_all(udid))
            if w and h:            # finger UP = the list scrolls down, and back
                y1, y2 = (h * 0.72, h * 0.32) if up else (h * 0.32, h * 0.80)
                _idbd.swipe(udid, w / 2, y1, w / 2, y2, 0.6 if up else 0.4)

        def search(where: str) -> Optional[str]:
            scrolls = loads = 0
            while time.time() < deadline:
                ok, how = _idbd.tap(udid, cands, wait=4.0 if not (scrolls or loads) else 1.5)
                if ok:
                    steps = [f"scrolled {scrolls}×"] if scrolls else []
                    steps += [f"Load More {loads}×"] if loads else []
                    return f"{where}" + (f", {', '.join(steps)}" if steps else "") + f", {how}"
                if scrolls >= self.RESTAURANT_SCROLLS:
                    return None
                more, _ = _idbd.tap(udid, self._LOAD_MORE)
                if more:
                    loads += 1
                    time.sleep(2.5)           # the next page comes from the server
                    continue
                swipe(up=True)
                scrolls += 1
                time.sleep(0.8)
            return None

        found = search("Home")
        fav_note = ""
        if found is None and time.time() < deadline:
            # Favourites: its chip sits at the top of Home -- scroll back up to it.
            tapped = False
            for _ in range(self.RESTAURANT_SCROLLS + 2):
                tapped, _how = _idbd.tap(udid, self._FAVOURITES)
                if tapped or time.time() >= deadline:
                    break
                swipe(up=False)
                time.sleep(0.6)
            if tapped:
                time.sleep(2.0)                   # the favourites feed loads
                found = search("Favourites")
            else:
                fav_note = "; the Favourites chip (favoriteButton) was not found either"

        if found is not None:
            if self._restaurant_page(udid, 10.0):
                notes.append(f"[ok] click {name} — opened the restaurant ({searched}{found})")
                return True
            notes.append(f"[FAIL] click {name} — tapped the card ({searched}{found}) but the "
                         f"restaurant page did not open in 10s")
            return False
        seen = [n for n in (_idbd.name(e) for e in _idbd.describe_all(udid)) if n][:25]
        notes.append(f"[FAIL] click {name} — {searched}not found on Home (scrolling down, "
                     f"Load More) nor in Favourites{fav_note}. On screen: {seen}")
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
                self._wait_gone(("orderLater", "pickUpOrderConfirm"), 2.5)
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
                    # Poll instead of a fixed 1.5s: done the moment Home is up, and a
                    # Home that takes 2-3s no longer falls through to the Appium
                    # whole-tree lookups below (10-40s each on this app) -- the
                    # 46s @consumer_home seen in live runs.
                    if self._wait_on_home(3.0):
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
            if self._wait_on_home(2.0):
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
        # Defensive: a caller that hands us a non-string (the _OPENED_VIA_SIDEBAR
        # sentinel once did) must not take the whole segment down with an
        # AttributeError deep inside a helper.
        if not isinstance(label, str):
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
        viewport_w = self._window_w(r)           # bounded (see _window_size)
        if not viewport_w:
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
        # NOT `with ThreadPoolExecutor()`: its __exit__ waits for a wedged call, so
        # the 45s ceiling never held (AGENTS.md #5; review 2026-10-19).
        return bool(_bounded_call(_do, 45.0, False))

    # Steps that settle or close a booking. When the diner has already paid in full
    # (a card pre-order in the C-App), "notify payment" COMPLETES the booking and the
    # app returns to the board -- measured on 4777: the card read 'Completed' right
    # after notifyPaymentBtn. There is then no bill to take and no table to close.
    _SETTLE_IDS = {"closeTableBtn"}

    # Order Summary header. Tablet (App/Screens/Event/OrderSummary.js:628-643) and
    # phone (App/MobileScreens/Event/OrderSummary.js) spellings, then the text.
    _SELECT_ALL = ("selectAll", "selectAllItemsBtn", "Select All")
    _UNSELECT = ("unSelectAll", "unSelectItemsBtn", "Unselect")

    def _select_all_items(self, notes: List[str], wait: float = 8.0) -> bool:
        """Every item on the order selected, ready for SEND or SERVE.

        The header shows Select All when nothing is selected and Unselect as soon
        as ANYTHING is -- so Unselect alone does not mean everything is. Then:
        Unselect, and Select All (the app selects every item that can be sent or
        served). Fails in seconds, not STEP_TIMEOUT, when neither is on screen."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()

        def on_screen(names):
            els = _idbd.describe_all(udid)
            return els, next((e for n in names for e in els
                              if _idbd.name(e) == n or (e.get("AXLabel") or "").strip() == n),
                             None)

        def wait_for(names):
            deadline = time.time() + wait
            while True:
                els, e = on_screen(names)
                if e is not None or time.time() >= deadline:
                    return els, e
                time.sleep(0.5)

        els, e = wait_for(self._SELECT_ALL + self._UNSELECT)
        if e is None:
            notes.append("[FAIL] select all items — neither Select All nor Unselect is "
                         "on screen (is the order open, and does it have items?)")
            return False
        how = ""
        if _idbd.name(e) in self._UNSELECT or (e.get("AXLabel") or "").strip() in self._UNSELECT:
            ok, _ = _idbd.tap(udid, self._UNSELECT, els)
            els, e = wait_for(self._SELECT_ALL) if ok else (els, None)
            if e is None:
                # Something is selected and Unselect did not respond: leave it as is.
                notes.append("[ok] select all items — items were already selected "
                             "(Unselect shown; could not reset the selection)")
                return True
            how = "reset the selection, then "
        ok, why = _idbd.tap(udid, self._SELECT_ALL, els)
        if not ok:
            notes.append(f"[FAIL] select all items — could not tap Select All ({why})")
            return False
        if wait_for(self._UNSELECT)[1] is None:
            notes.append("[FAIL] select all items — tapped Select All but nothing was "
                         "selected (Unselect never appeared)")
            return False
        notes.append(f"[ok] select all items — {how}tapped Select All; every item is "
                     f"selected (idb)")
        return True

    def _unfold_event(self, udid: str, slot: str, key: str) -> List[dict]:
        """Open THIS run's event (the one showing *slot*) on the Wallet's Upcoming list
        and return the screen; the screen as it is when no event shows it.

        A diner who ACCEPTED an invitation sees every event folded: one Button per
        event, `${restaurant}Card` (MEASURED 2026-10-29, the participant phone:
        'NylaiKitchen2Card' ×2, 123pt tall), whose time shows only once unfolded.
        Another event left open hides the list (its cards leave the tree), so it is
        folded first with `${host}Close` (*key* = the host's name, lower case)."""
        name, frame = _idbd.name, _idbd.frame
        shown = lambda es: any(name(e) == slot for e in es)  # noqa: E731

        def shut(es) -> Optional[dict]:
            return next((e for e in es if name(e).endswith("Close")
                         and name(e).lower().startswith(key)), None)

        els = _idbd.describe_all(udid)
        if shown(els):
            return els
        if shut(els) is not None:
            _idbd.tap_el(udid, shut(els), els, scroll=False)
            time.sleep(1.2)
            els = _idbd.describe_all(udid)
        folded = sorted((e for e in els if e.get("type") == "Button"
                         and name(e).endswith("Card") and "Invite" not in name(e)
                         and frame(e)[3] > 80), key=lambda e: frame(e)[1])
        for card in folded[:4]:
            fresh = next((e for e in _idbd.describe_all(udid)
                          if name(e) == name(card) and frame(e)[1] == frame(card)[1]), card)
            _idbd.tap_el(udid, fresh, scroll=False)
            els, hit = self._wait_els(udid, shown, 4.0)
            if hit:
                return els
            if shut(els) is not None:
                _idbd.tap_el(udid, shut(els), els, scroll=False)
                time.sleep(1.0)
        return _idbd.describe_all(udid)

    def _after_booking(self, choice: str, notes: List[str]) -> bool:
        """Pre-order or order later, right after BOOK NOW.

        A 1 hr booking asks in a dialog (orderLater / preOrderBooking). Any other
        duration -- 'Not Sure', used in every scenario -- goes straight to the
        Wallet: ordering later then needs nothing, and pre-ordering is the booking
        card's Menu button, which opens the same restaurant menu."""
        udid = self._consumer_udid()
        name, frame = _idbd.name, _idbd.frame
        els = _idbd.describe_all(udid)
        if any(name(e) == choice for e in els):
            ok, how = _idbd.tap(udid, [choice], els)
            notes.append(f"[{'ok' if ok else 'FAIL'}] {choice} — tapped in the booking "
                         f"dialog ({how})")
            return ok
        if not any(name(e) in ("walletUpcomingSearchInput", "upcomingBlock") for e in els):
            notes.append(f"[FAIL] {choice} — neither the booking dialog nor the Wallet "
                         f"is on screen")
            return False
        if choice == "orderLater":
            notes.append("[skip] order later — the booking is already in the Wallet; "
                         "there is no prompt for this duration")
            return True
        # Pre-order: the Menu button of THIS booking's card -- the one under its time.
        key = CONSUMER_NAME.replace(" ", "").lower()          # 'Roopa' -> 'RoopaD...'
        slot = getattr(self, "_booked_slot", "")

        def booked_menu(es) -> Optional[dict]:
            """The Menu button under the booked slot's time. Every booking on the
            (shared) account carries the same 'RoopaDmenuOrderCard', so it is
            picked by position under the time, never by name alone."""
            menus = [e for e in es if re.search(r"(menuOrderCard|preOrderCard)$", name(e))
                     and name(e).lower().startswith(key)]
            when = [e for e in es if slot and name(e) == slot]
            if when and menus:
                ty = frame(when[0])[1]
                below = [m for m in menus if frame(m)[1] > ty] or menus
                menus = sorted(below, key=lambda m: frame(m)[1] - ty)
            return menus[0] if menus else None

        target = booked_menu(els)
        if target is None:
            # MEASURED 2026-10-19 (flow1, run bcac5b28): BOOK NOW confirmed in 6s and
            # the Wallet came up before its list loaded (search box and coupons, no
            # cards); one read failed the step in 0.3s. Wait for the booked card.
            els, target = self._wait_els(udid, booked_menu, 15.0)
        if target is None and slot:
            els = self._unfold_event(udid, slot, key)
            target = booked_menu(els) if any(name(e) == slot for e in els) else None
            if target is not None:
                notes.append(f"    · pre-order: unfolded the {slot} event on the Wallet")
        if target is None:
            notes.append("[FAIL] pre-order — no Menu button on the booking's Wallet card "
                         "(waited 15s for the Wallet to list it)")
            return False
        label = name(target)
        # Two tries. MEASURED 2026-10-17 (PAY4, run 4824d34d): one tap on the card's
        # Menu, right after BOOK NOW landed on the Wallet, left the Wallet as it was --
        # the card was still settling. The tap's own result was not even recorded.
        def press(t, es) -> Tuple[bool, str]:
            """Tap the Menu / Pre-Order button. Its inner text answers a hit-test at its
            centre, which tap_el's strict check reads as 'covered' -- MEASURED
            2026-10-21 (flow1/flow2, runs 2618714e, cc098587): the open event's
            Pre-Order, fully on screen, refused twice. _tap_inside accepts a child
            inside the button's own frame (the same trap as the Wallet's Close)."""
            ok, how = _idbd.tap_el(udid, t, es, scroll=False)
            if ok:
                return True, how
            if self._tap_inside(udid, t)[0]:
                return True, "idb, inside its frame"
            ok, how = _idbd.tap_el(udid, t, es)          # off-screen: scroll it into view
            if ok:
                return True, how
            fresh = booked_menu(_idbd.describe_all(udid) or es)
            if fresh is not None and self._tap_inside(udid, fresh)[0]:
                return True, "idb, scrolled into view, inside its frame"
            return False, how

        tries = []
        for attempt, wait in ((1, 8.0), (2, 12.0)):
            if attempt > 1:
                # Re-pick from a FRESH read: the list may have settled since, and
                # "the Menu nearest the old y" can be another booking's (review
                # 2026-10-19). If the booked card's Menu is gone, the first tap is
                # navigating: do not tap (and scroll) the menu page that is opening.
                els = _idbd.describe_all(udid) or els
                target = booked_menu(els) if any(name(e) == slot for e in els) else None
                if target is None:
                    tries.append("no second tap: the Wallet card is gone")
                    deadline = time.time() + wait
                    while time.time() < deadline:
                        time.sleep(0.5)
                        if any(name(e).endswith("Inc") for e in _idbd.describe_all(udid)):
                            notes.append(f"[ok] pre-order — opened the menu from the booking's "
                                         f"Wallet card ({label}; it opened late)")
                            return True
                    break
            ok, how = press(target, els)
            tries.append(how if ok else f"tap refused: {how}")
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(0.5)
                if any(name(e).endswith("Inc") for e in _idbd.describe_all(udid)):
                    notes.append(f"[ok] pre-order — opened the menu from the booking's Wallet "
                                 f"card ({label}, {how}"
                                 + (", second tap" if attempt > 1 else "") + ")")
                    return True
        notes.append(f"[FAIL] pre-order — the menu did not open from {label} "
                     f"({'; '.join(tries)})")
        return False

    def _send_to_kitchen(self, notes: List[str], wait: float = 12.0) -> bool:
        """SEND the selected items to the kitchen, and confirm they went.

        SEND renders only while items are selected (OrderSummary.js: servedData
        .length > 0) and its onPress posts the order, then refreshes the screen,
        which clears the selection -- so the button going away is the sign the
        order was sent. It stays on a failed post ('Error sending data')."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()

        def send_shown() -> bool:
            return any(_idbd.name(e) == "sendItemsBtn" for e in _idbd.describe_all(udid))

        if not send_shown() and not self._select_all_items(notes):
            notes.append("[FAIL] send to kitchen — no SEND button: nothing is selected")
            return False
        for attempt in (1, 2):
            ok, why = _idbd.tap(udid, ["sendItemsBtn"])
            if not ok:
                notes.append(f"[FAIL] send to kitchen — could not tap SEND ({why})")
                return False
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(1.0)
                if not send_shown():
                    notes.append(f"[ok] send to kitchen — tapped SEND; the order was "
                                 f"sent (button cleared{', 2nd tap' if attempt == 2 else ''})")
                    return True
        notes.append(f"[FAIL] send to kitchen — tapped SEND twice but the order was not "
                     f"sent (SEND still shown after {wait:.0f}s each time)")
        return False

    # Payment section of the waiter's order screen (App/Screens/Event/PaymentDetails.js).
    # Note 'epaymentBtn' -- lower-case p; the old lookup tried 'ePaymentBtn' and the
    # 'E-Payment' caption, found neither, and failed "payment method not found".
    _PAY_BUTTONS = {
        # A GUEST card names them paid*: paidEpaymentBtn … (PaymentDetails:3698-3829).
        "epay": ("epaymentBtn", "ePaymentBtn", "paidEpaymentBtn", "E-Payment"),
        "cash": ("cashPaymentBtn", "paidCashPaymentBtn", "Cash"),
        "voucher": ("foodVoucherBtn", "paidVoucherBtn", "Food Voucher"),
    }
    _MONEY_RE = re.compile(r"^\s*(\d+(?:[.,]\d{1,2})?)\s*€\s*$")

    def _pay_business(self, method: str, notes: List[str]) -> bool:
        """Settle the diner's bill on the waiter's order screen.

        The payment methods live INSIDE the diner's card ('R RoopaDaccordionCard'),
        which starts collapsed -- the old step looked for them without opening it
        (measured: 40s, "payment method not found"). So: open the card, pick the
        method, type the bill total on the app's own keypad (a sheet of plain
        digit keys; the amount shows in 'userAmountInput'), press Input, reopen
        the card if it folded, and press Confirm Payment."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name = _idbd.name
        buttons = self._PAY_BUTTONS[method]

        def find(names, els):
            return next((e for n in names for e in els
                         if name(e) == n or (e.get("AXLabel") or "").strip() == n), None)

        def diner_card(els):
            # The profile that took over the others' share (@pay_for_all), if any:
            # it owes the whole bill now, the others nothing.
            cards = [e for e in els if "accordionCard" in name(e)]
            want = getattr(self, "_payer_card", "")
            return next((e for e in cards if want and want in name(e).split()),
                        cards[0] if cards else None)

        def wait_for(pick, secs=8.0):
            deadline = time.time() + secs
            while True:
                els = _idbd.describe_all(udid)
                hit = pick(els)
                if hit is not None or time.time() >= deadline:
                    return els, hit
                time.sleep(0.5)

        # 1. The diner's card, opened to its payment methods.
        els, btn = wait_for(lambda es: find(buttons, es), 2.0)
        if btn is None:
            card = diner_card(els)
            if card is None:
                notes.append(f"[FAIL] @pay:{method} — no diner card on screen "
                             f"(is the order open?)")
                return False
            _idbd.tap_el(udid, card, els)
            els, btn = wait_for(lambda es: find(buttons, es))
            if btn is None:
                notes.append(f"[FAIL] @pay:{method} — opened the diner's card but no "
                             f"payment methods appeared (payment not requested yet?)")
                return False
        self._payment_accepted = None                    # see the Confirm outcome below

        def read_bill(es) -> Optional[float]:
            b = self._payer_card_total(es, diner_card(es))
            return b if b is not None and b >= 0.005 else self._bill_total_idb(es)

        bill = read_bill(els)
        if bill is not None and bill < 0.005:
            # Right after Pay For -> Apply the panel re-renders and can print 0 for a
            # moment (the footer prints 0 while totalCost is not > 0,
            # PaymentDetails.js:4997-5002): read again before calling it 0.
            els, _ = wait_for(lambda es: True if (read_bill(es) or 0) >= 0.005 else None, 6.0)
            bill = read_bill(els)
        if bill is None:
            notes.append(f"[FAIL] @pay:{method} — could not read the bill total")
            return False
        if bill < 0.005:
            # A 0.00 bill entered as a payment wipes nothing and pays nothing.
            notes.append(f"[FAIL] @pay:{method} — the payer's card reads a bill of 0.00 € "
                         f"(nothing to pay, or the wrong card is open)")
            return False
        # Cash is tendered over the bill so the change calculation gets checked.
        if method == "cash":
            amount = round(bill + PAY_OVER_BY, 2)
        elif getattr(self, "_paid_for_others", None):
            amount = round(bill + PAY_FOR_OTHERS_OVER_BY, 2)
            notes.append(f"    · {method}: paying for others — {amount:.2f} € for a bill of "
                         f"{bill:.2f} € (staging ignores an exact pay-for-all payment; "
                         f"see known_issues.md)")
        else:
            amount = bill

        text = f"{amount:.2f}"

        def on_method(es) -> Optional[float]:
            """The amount shown UNDER the method button ('0 €' -> 36.05 €): what the
            bill actually took from the keypad (measured: x as the button, ~12pt
            below it)."""
            b = find(buttons, es)
            if b is None:
                return None
            bx, by, bw, bh = _idbd.frame(b)
            for e in es:
                ex, ey, _w, _h = _idbd.frame(e)
                m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*€", name(e) or "")
                if m and abs(ex - bx) <= 12 and by + bh <= ey <= by + bh + 40:
                    return float(m.group(1).replace(",", "."))
            return None

        def enter_amount() -> bool:
            """Method -> keypad -> the amount, read back -> Input."""
            es, b = wait_for(lambda x: find(buttons, x), 4.0)
            if b is None:
                notes.append(f"[FAIL] @pay:{method} — the payment methods are not on screen")
                return False
            ok, why = _idbd.tap_el(udid, b, es)
            es, pad = wait_for(lambda x: find(("userInputBtn",), x))
            if not ok or pad is None:
                notes.append(f"[FAIL] @pay:{method} — tapped {name(b)} but the amount "
                             f"keypad did not open ({why})")
                return False
            got = self._keypad_type(udid, text)
            if got is None or text not in got.replace(",", "."):
                notes.append(f"[FAIL] @pay:{method} — typed {text} on the keypad but the "
                             f"amount reads {got!r}")
                return False
            _idbd.tap(udid, ["userInputBtn"])
            wait_for(lambda x: None if find(("userInputBtn",), x) else True)
            notes.append(f"    · {method}: entered {text} € for a bill of {bill:.2f} € "
                         f"and pressed Input")
            return True

        # 2-3. The method opens the keypad; the amount, read back; Input.
        if not enter_amount():
            return False
        els, _ = wait_for(lambda es: None, 0.5)

        # Several guests: a 'pay for' picker may ask whose share this is.
        if find(("Apply",), els) is not None:
            me = next((e for e in els if name(e).endswith("select")
                       and name(e).lower().startswith(CONSUMER_NAME.lower().replace(" ", ""))), None)
            if me is not None:
                _idbd.tap_el(udid, me, els)
            _idbd.tap(udid, ["Apply"])
            els, _ = wait_for(lambda es: None, 1.0)

        # 4. Confirm Payment. Measured on the iPad: the card stays OPEN after Input
        # and Confirm appears a moment later, once it re-renders. Tapping the card
        # "to reopen it" then CLOSED it. So: wait for Confirm first; reopen the card
        # only when it has really folded (its payment methods are gone); and before
        # confirming, check the amount is on the method -- if it reads 0 € (lost
        # when the card folded), enter it again (as asked, 2026-10-01).
        confirm, shown = None, None
        for round_ in (1, 2):
            els, confirm = wait_for(lambda es: find(("paymentConfirmBtn",), es), 6.0)
            if find(buttons, els) is None:              # the card folded: reopen it
                card = diner_card(els)
                if card is not None:
                    _idbd.tap_el(udid, card, els)
                    notes.append(f"    · {method}: the profile card folded — reopened it")
                els, _ = wait_for(lambda es: find(buttons, es))
                els, confirm = wait_for(lambda es: find(("paymentConfirmBtn",), es), 4.0)
            # The amount under the method IS what Confirm will charge
            # (payingWholeAmount, PaymentDetails.js:2265). Confirm is greyed until it
            # covers the bill, and a greyed Confirm takes a tap silently (no
            # accessibility state) -- so check it here, not by tapping.
            shown = on_method(els)
            if shown is not None and abs(shown - amount) > 0.005:
                if round_ == 1:
                    notes.append(f"    · {method}: the payment shows {shown:.2f} € after "
                                 f"Input of {text} € — entering it again")
                    if not enter_amount():
                        return False
                    continue
                notes.append(f"[FAIL] @pay:{method} — entered {text} € twice but the payment "
                             f"shows {shown:.2f} €: the app did not keep the amount; not "
                             f"confirming")
                return False
            break
        if confirm is None:
            notes.append(f"[FAIL] @pay:{method} — Confirm Payment not found after "
                         f"entering the amount")
            return False
        ok, why = _idbd.tap_el(udid, confirm, els)
        if not ok:
            notes.append(f"[FAIL] @pay:{method} — could not tap Confirm Payment ({why})")
            return False

        # Settled: the close-table control appears, or -- when the whole bill is
        # settled -- the app shows the receipt ('TICKET CLIENT') and closes the
        # booking itself. ACCEPTED: the call returned 2xx -- onConfirmPay zeroes the
        # entered amount (Screens/Event/index.js:1093-1095; MEASURED 2026-10-19 on
        # 5237: 85.8 € -> 0 € within ~4s) or the card refreshes and folds. Accepted is
        # NOT applied: when one profile pays for several, staging ignores an amount
        # that equals the bill (see _close_table) and the booking stays PAYMENT.
        def outcome(es):
            if find(("closeTableBtn",), es) or self._settled_screen(es):
                return "settled"
            if not find(("paymentConfirmBtn",), es):
                return "accepted"
            now = on_method(es)
            if shown is not None and shown >= 0.005 and now is not None and now < 0.005:
                return "accepted"
            return None

        els, res = wait_for(outcome, 45.0)
        if res:
            self._payment_accepted = amount
        change = f", change {amount - bill:.2f} €" if amount - bill >= 0.005 else ""
        notes.append(f"[ok] Bill check: total €{bill:.2f}, paid €{amount:.2f} by "
                     f"{method}{change}; Confirm Payment "
                     + ("accepted; the booking settled" if res == "settled" else
                        "accepted (the app cleared the entered amount)" if res else
                        "tapped, but the app did not react in 45s (result checked by "
                        "close table)"))
        return True

    @staticmethod
    def _settled_screen(els: List[dict]) -> str:
        """'receipt' / 'board' when the app has finished the booking on its own after
        the whole bill was paid (receipt shown, then back to My Bookings); else ''."""
        names = {_idbd.name(e) for e in els}
        if "TICKET CLIENT" in names:
            return "receipt"
        if "My Bookings" in names and "paymentConfirmBtn" not in names:
            return "board"
        return ""

    def _payer_card_total(self, els: List[dict], card: Optional[dict]) -> Optional[float]:
        """The 'Total' printed INSIDE the payer's open card: their own share plus
        everyone they pay for (PaymentDetails.js:1133-1141).

        MEASURED 2026-10-19 (run 309869d5): the bottom-most 'Total' on screen was a
        paid-for guest's card ('Total 0€', PaymentDetails.js:1147-1153), so the bill
        read 0.00 and 0.00 was entered. The card's own Total is the first small
        'Total' row below its header and above the next card's header; the panel's
        footer Total (h~74) sits outside the cards."""
        if card is None:
            return None
        name, frame = _idbd.name, _idbd.frame
        top = frame(card)[1]
        heads = sorted(frame(e)[1] for e in els
                       if "accordionCard" in name(e) and frame(e)[1] > top + 1)
        bottom = heads[0] if heads else float("inf")
        rows = sorted((e for e in els if name(e).strip().lower() == "total"
                       and top < frame(e)[1] < bottom and frame(e)[3] < 40),
                      key=lambda e: frame(e)[1])
        for t in rows:
            ty = frame(t)[1] + frame(t)[3] / 2
            for e in els:
                m = self._MONEY_RE.match(name(e) or "")
                if m and frame(e)[3] < 40 and frame(e)[0] > frame(t)[0] \
                        and abs(frame(e)[1] + frame(e)[3] / 2 - ty) < 12:
                    return float(m.group(1).replace(",", "."))
        return None

    def _bill_total_idb(self, els: List[dict]) -> Optional[float]:
        """The order's Total: the '36.05  €' text on the row of a 'Total' label
        (the bottom-most such row, which is the order total, not a guest share)."""
        name, frame = _idbd.name, _idbd.frame
        best = None
        for t in (e for e in els if name(e).strip().lower() == "total"):
            ty = frame(t)[1] + frame(t)[3] / 2
            for e in els:
                m = self._MONEY_RE.match(name(e) or "")
                if m and abs(frame(e)[1] + frame(e)[3] / 2 - ty) < 12 and frame(e)[0] > frame(t)[0]:
                    if best is None or ty > best[0]:
                        best = (ty, float(m.group(1).replace(",", ".")))
        return best[1] if best else None

    def _keypad_type(self, udid: str, text: str) -> Optional[str]:
        """Type *text* on the payment keypad; returns what the amount box shows.

        Keys carry no ids (App/Components/Keyboard): digits are plain '0'-'9'
        texts, the decimal key is the icon LEFT of '0' and backspace the one to its
        RIGHT. Only elements below the amount box count -- the header has a '0'."""
        name, frame = _idbd.name, _idbd.frame

        def keys():
            els = _idbd.describe_all(udid)
            box = next((e for e in els if name(e) == "userAmountInput"), None)
            top = frame(box)[1] + frame(box)[3] if box else 0
            pad = [e for e in els if frame(e)[1] > top and e.get("type") != "Application"
                   and name(e) != "userInputBtn"]
            digits = {name(e): e for e in pad if re.fullmatch(r"\d", name(e))}
            zero = digits.get("0")
            row = [e for e in pad if zero is not None and name(e) not in digits
                   and abs(frame(e)[1] - frame(zero)[1]) < frame(zero)[3]]
            dot = max((e for e in row if frame(e)[0] < frame(zero)[0]),
                      key=lambda e: frame(e)[0], default=None) if zero else None
            back = min((e for e in row if frame(e)[0] > frame(zero)[0]),
                       key=lambda e: frame(e)[0], default=None) if zero else None
            return els, box, digits, dot, back

        def shown():
            box = keys()[1]
            return str((box or {}).get("AXValue") or name(box or {}) or "") if box else None

        for attempt in (1, 2):
            els, box, digits, dot, back = keys()
            if box is None or len(digits) < 10:
                return None
            if attempt == 2 and back is not None:        # clear a bad entry, then retype
                for _ in range(len(text) + 2):
                    _idbd.tap_el(udid, back, els, scroll=False)
                    time.sleep(0.15)
            for ch in text:
                key = dot if ch == "." else digits.get(ch)
                if key is None:
                    return None
                _idbd.tap_el(udid, key, els, scroll=False)
                time.sleep(0.2)
            got = shown()
            if got and text in got.replace(",", "."):
                return got
        return shown()

    def _optional_step(self, r: ScenarioRunner, inner: str, notes: List[str]) -> bool:
        """A step marked '?' applies to some builds/states only ('?click Yes').

        Wait briefly for its target; tap it if it appears, otherwise record that it
        did not apply and move on. Measured: this build has NO 'Yes' after notify
        payment (it shows a 'Payment Notified' toast), and the unconditional step
        hung the segment for its full 240s looking for one."""
        m = re.match(r"^\s*click\s+(.+?)\s*$", inner)
        target = (m.group(1) if m else inner).strip()
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        deadline = time.time() + 6.0
        while time.time() < deadline:
            els = _idbd.describe_all(udid)
            if any(_idbd.name(e) == target or (e.get("AXLabel") or "").strip() == target
                   for e in els):
                ok, note, _fl = self._smart_click(r, inner)
                notes.append(f"[{'ok' if ok else 'FAIL'}] {inner} — {note}")
                return ok
            time.sleep(1.0)
        notes.append(f"[skip] {inner} — not shown here (optional step; this build "
                     f"has no such control at this point)")
        return True

    def _already_settled(self) -> str:
        """'booking 4777 is Completed' when the booked booking needs no more
        payment, else ''. Read from the My Orders panel (exact start time + status),
        or the diner's board card in the booked hour. Never while the payment or
        close controls are still on screen."""
        slot = getattr(self, "_booked_slot", "") or ""
        want = re.match(r"^(\d{1,2}):(\d{2})", slot)
        if not want:
            return ""
        want_hhmm = f"{int(want.group(1)):02d}:{want.group(2)}"
        hour_lbl = f"{int(want.group(1)):02d}:00"
        udid = self._business_udid()
        done = ("completed", "paymentdone")
        diner = _norm(CONSUMER_NAME).split()[0] if CONSUMER_NAME else ""
        for _ in range(4):                 # the panel refreshes a beat after completion
            els = _idbd.describe_all(udid)
            names = {_idbd.name(e) for e in els}
            # 'epaymentBtn' is the real id (PaymentDetails.js:2210); 'ePaymentBtn'
            # is kept for an older build.
            if names & {"epaymentBtn", "ePaymentBtn", "cashPaymentBtn", "foodVoucherBtn",
                        "E-Payment", "Cash", "Food Voucher"}:
                return ""                  # a bill is open: there is something to pay
            # Close Table renders ONLY once the bill is paid:
            #   !serve && !inprogress && payment_completed && reminder
            #   && visited_restaurant                (Screens/Event/OrderSummary)
            # Measured on 4780: after notify payment the order stayed up with
            # PRE-ORDERED items, 'Total', closeTableBtn -- and no payment methods.
            if "closeTableBtn" in names:
                return "the bill is already paid (the app offers only Close Table)"
            for e in els:
                lbl = _idbd.name(e)
                mm = _SIDEBAR_ROW_RE.search(lbl)
                if not mm:
                    continue
                start = mm.group(0).split("-")[0].strip()
                if f"{int(start.split(':')[0]):02d}:{start.split(':')[1]}" != want_hhmm:
                    continue
                st = _row_status(lbl)
                if st in done:
                    return f"booking {lbl.split()[0]} ({want_hhmm}) is already {st}"
            hours = [(_idbd.name(e), _idbd.frame(e)[1]) for e in els
                     if re.match(r"^\d{1,2}:00$", _idbd.name(e))]
            for e in els:
                nm, st = self._split_card(_idbd.name(e))
                if not nm or not (st == "completed" or st.startswith("payment")) or \
                        (diner and not _norm(nm).startswith(diner)):
                    continue
                top = _idbd.frame(e)[1]
                near = min(hours, key=lambda h: abs(h[1] - top), default=None)
                if near and near[0] == hour_lbl:
                    return f"the diner's {hour_lbl} booking is already {st}"
            time.sleep(1.5)
        return ""

    def _skip_if_settled(self, step: str, notes: List[str]) -> bool:
        settled = self._already_settled()
        if settled:
            notes.append(f"[skip] {step} — nothing left to pay: {settled} (the diner paid "
                         f"in full in the C-App, so notify payment completed it)")
            return True
        return False

    # Status words as the panel PRINTS them, most specific first: 'SERVE' is inside
    # 'RESERVED', so RESERVED must be tried before it.
    _PANEL_STATUS_WORDS = ("CONFIRMATION PENDING", "PAYMENT DONE", "PAYMENT", "IN PROGRESS",
                           "RESERVED", "COMPLETED", "CANCELLED", "EXPIRED", "DECLINED",
                           "SERVE")

    def _own_panel_card(self, cands: list, want_hhmm: str, notes: List[str],
                        where: str = "My Orders panel") -> tuple:
        """Of several panel bookings at the booked slot, the one THIS run made.

        Each card prints when its booking last changed, right of its window
        (OrderCard: moment(updatedAt).format('HH:mm')). This run changed its own
        booking from its BOOK NOW / Save until this segment began, so the card that
        changed inside that span is ours. MEASURED 2026-10-19 (Guests): 5232 --
        another diner on the shared 'Roopa D' account, 1 person, 11x pre-orders --
        sat RESERVED at 11:40 beside this run's booking and was opened instead.
        cands: [(ticket, status, window_line, block_lines)], in panel order."""
        if len(cands) == 1:
            return cands[0]
        tickets = ", ".join(c[0] for c in cands)
        since = getattr(self, "_touched_since", None)
        until = getattr(self, "_seg_started_at", None)
        if since and until:
            def minute(epoch: float) -> int:
                t = time.localtime(epoch)
                return t.tm_hour * 60 + t.tm_min

            lo, hi = minute(since) - 1, minute(until) + 1

            def changed(block) -> Optional[int]:
                t = next((t for t in block
                          if re.fullmatch(r"\d{1,2}[:.]\d{2}", t[0].strip())), None)
                if t is None:
                    return None
                h, m = re.split(r"[:.]", t[0].strip())
                return int(h) * 60 + int(m)

            ours = [c for c in cands if (changed(c[3]) is not None
                                         and lo <= changed(c[3]) <= hi)]
            # Only a time that was READ can rule a card out: a missed or merged OCR
            # time is "unknown", not "someone else's" (review 2026-10-19).
            unread = [c for c in cands if changed(c[3]) is None]
            if len(ours) == 1 and not unread:
                at = changed(ours[0][3])
                notes.append(f"    · {where}: {len(cands)} bookings at {want_hhmm} "
                             f"({tickets}); took {ours[0][0]} — it last changed at "
                             f"{at // 60:02d}:{at % 60:02d}, while this run was booking")
                return ours[0]
        notes.append(f"    · {where}: {len(cands)} bookings at {want_hhmm} "
                     f"({tickets}) and none is clearly this run's — took the first")
        return cands[0]

    def _open_from_panel(self, slot: str, statuses: tuple, what: str,
                         notes: List[str], pages: int = 8, budget: float = 75.0) -> bool:
        """Open the booking straight from the right-hand My Orders panel.

        The panel lists today's bookings with ticket, status and time window --
        the fastest place to find one (no calendar scroll, no hour list). Its
        cards expose NONE of that to accessibility (labelled `${el?.id}`, which is
        'undefined' for a reserved booking), so the card text is READ from the
        screen (screen_text) and grouped into cards by ticket number. The card
        element under that text is tapped when accessibility has one, else the
        text itself.

        Measured 2026-09-29: 4794 RESERVED 12:50 was plainly in the panel and the
        step still scrolled the calendar for 106s -- it gave up here without a
        word, needing an accessibility card element for the match. Matching is
        now text-only, and every fallback says why.

        SEARCHED CAREFULLY, before the calendar is touched: page DOWN to the end of
        the list (it has ended when a page shows the same tickets as the last), then,
        if the booking was not there, page back UP to the top -- the panel may have
        been left scrolled part-way by an earlier step, so starting where it is and
        only going down missed everything above. It used to give up after 2 pages
        and fall back to scrolling the calendar."""
        from automation.scenarios import screen_text, idb_coords
        want = re.match(r"^(\d{1,2}):(\d{2})", slot or "")
        if not want:
            return False
        want_hhmm = f"{int(want.group(1)):02d}:{want.group(2)}"
        udid = self._business_udid()
        name, frame = _idbd.name, _idbd.frame

        def skip(why: str) -> bool:
            notes.append(f"    · My Orders panel: {why} — trying the calendar")
            return False

        def ticket_lines(lines) -> list:
            """The cards' ticket numbers, top first. A card's SECOND row carries its
            window, its table badge and its time ('11:20 - 12:20 | I16 | 11:18');
            MEASURED 2026-10-17 (runs 30c434a6, e0685fc6, ...): OCR reads table 'I16'
            as '116', which split card 5203 in two -- 5203 lost its window, '116'
            got it with no status, and the booking "read ?" and was skipped. A
            number on a window's row is that card's table, never a ticket."""
            def mid(t):
                return t[2] + t[4] / 2

            rows = [t for t in (lines or [])
                    if len(re.findall(r"(\d{1,2})[:.](\d{2})", t[0])) >= 2]
            return sorted((t for t in (lines or []) if re.fullmatch(r"\d{3,6}", t[0].strip())
                           and not any(abs(mid(t) - mid(w)) < max(t[4], w[4], 8.0)
                                       for w in rows)),
                          key=lambda t: t[2])

        # A TIME budget, not just a page count. MEASURED 2026-10-22 (flow3, run
        # 0aadbb00): on a busy iPad a page (read + OCR + swipe + settle) took 20-30s;
        # the panel walked down and back up for the whole 240s step and the calendar
        # route -- which found the same booking in segment 2 -- never ran.
        give_up_at = time.time() + budget
        seen, direction, turned = None, "down", False
        for page in range(pages * 2):
            if page and time.time() > give_up_at:
                return skip(f"no {want_hhmm} booking found in {budget:.0f}s of searching")
            els = _idbd.describe_all(udid)
            if not els:
                return skip("could not read the screen (idb)")
            w_app, h_app = _idbd.app_size(els)
            head = next((e for e in els
                         if name(e).startswith("My Orders")
                         or (e.get("AXLabel") or "").strip().startswith("My Orders")), None)
            # The panel is the right third of the landscape screen (x~795 of 1210).
            x0 = (frame(head)[0] - 30) if head else w_app * 0.64
            lines = screen_text.read_text(udid, (x0, 0, w_app, h_app), els)
            tickets = ticket_lines(lines)
            if not tickets and lines is not None:
                # Unreadable text usually means the screenshot was turned the wrong
                # way: re-measure the rotation once and read again.
                idb_coords.forget(udid)
                lines = screen_text.read_text(udid, (x0, 0, w_app, h_app), els)
                tickets = ticket_lines(lines)
            if not lines:
                return skip("could not read its text")
            if head is None and not any("MY ORDERS" in t[0].upper() for t in lines):
                return skip("not on this screen")
            if not tickets:
                return skip("no bookings listed")
            # This run's own booking, when it is known and on this page. Staging is
            # shared and the earliest bookable slot is the same for everyone:
            # MEASURED 2026-10-19 (flow2): 5230 RESERVED sat at 11:25 next to this
            # run's 5229 -- another diner's. Status alone picked the right one only
            # because 5230 was not served yet. (Only when the ticket is READ on this
            # page: a misread number must not hide the booking.)
            mine = getattr(self, "_booked_ticket", "")
            mine = mine if any(t[0].strip() == mine for t in tickets) else ""
            cands = []
            for i, tk in enumerate(tickets):
                y_top = tk[2] - 15
                y_end = tickets[i + 1][2] - 15 if i + 1 < len(tickets) else h_app
                block = [t for t in lines if y_top <= t[2] < y_end]
                window = next((t for t in block
                               if len(re.findall(r"(\d{1,2})[:.](\d{2})", t[0])) >= 2), None)
                if window is None:
                    continue
                hh, mm = re.findall(r"(\d{1,2})[:.](\d{2})", window[0])[0]
                if f"{int(hh):02d}:{mm}" != want_hhmm:
                    continue
                up = " ".join(t[0].upper() for t in block)
                status = next((w for w in self._PANEL_STATUS_WORDS
                               if re.search(rf"\b{w}\b", up)), "")
                ticket = tk[0].strip()
                if mine and ticket != mine:
                    notes.append(f"    · My Orders panel: the {want_hhmm} booking {ticket} "
                                 f"is not this run's ({mine}) — skipped")
                    continue
                st = _norm(status)
                if not any(st == ok or st.startswith(ok) for ok in statuses):
                    notes.append(f"    · My Orders panel: the {want_hhmm} booking "
                                 f"{ticket} reads {status or '?'} — not "
                                 f"{'/'.join(statuses)}")
                    continue
                cands.append((ticket, status, window, block))
            if cands:
                ticket, status, window, _block = self._own_panel_card(cands, want_hhmm, notes)
                notes.append(f"    · found the {want_hhmm} booking in the My Orders "
                             f"panel: {ticket} {status}".rstrip())
                wx, wy = window[1] + window[3] / 2, window[2] + window[4] / 2
                card = next((e for e in els if e.get("type") != "Application"
                             and frame(e)[2] > 200 and frame(e)[3] > 60
                             and frame(e)[0] <= wx <= frame(e)[0] + frame(e)[2]
                             and frame(e)[1] <= wy <= frame(e)[1] + frame(e)[3]), None)
                ok, _how = (_idbd.tap_el(udid, card, els) if card is not None
                            else _idbd.tap_point(udid, wx, wy))
                if ok:
                    self._remember_ticket(ticket, notes)
                    self._remember_status(status)
                return ok and self._reservation_opened(notes, what, slot, "My Orders panel")
            # Not on this page. The header says how many the list holds ('In Queue
            # 05'); when the page already shows them all there is nothing to scroll to.
            # MEASURED 2026-10-22: five cards, all on screen, and the search still paged
            # down and back up for minutes on a busy iPad.
            queue = None
            iq = next((t for t in lines if t[0].strip().lower().startswith("in queue")), None)
            if iq is not None:
                m = re.search(r"(\d{1,3})\s*$", iq[0])
                num = m.group(1) if m else next(
                    (t[0].strip() for t in lines if re.fullmatch(r"\d{1,3}", t[0].strip())
                     and t[1] > iq[1] and abs(t[2] - iq[2]) < 14), None)
                queue = int(num) if num else None
            if queue is not None and len(tickets) >= queue:
                return skip(f"no {'/'.join(statuses)} booking at {want_hhmm} among the "
                            f"{queue} listed")
            # Has the list stopped moving? Then this end is done: turn round once and
            # walk back up to the top.
            sig = tuple(t[0].strip() for t in tickets)
            if sig == seen:
                if turned:
                    break                          # searched both ends of the list
                direction, turned = "up", True
                notes.append(f"    · My Orders panel: {want_hhmm} not found down to the "
                             f"end of the list — checking above, back to the top")
            seen = sig
            col = x0 + (w_app - x0) / 2
            if direction == "down":                # finger up: later bookings come into view
                _idbd.swipe(udid, col, h_app * 0.85, col, h_app * 0.45, 1.8)
            else:                                  # finger down: earlier bookings
                _idbd.swipe(udid, col, h_app * 0.45, col, h_app * 0.85, 1.8)
            # The panel scrolls with momentum: read it only once its cards stop
            # moving (same frames twice), or the text is read mid-glide.
            last = None
            for _ in range(5):
                time.sleep(0.4)
                cards = tuple(frame(e) for e in _idbd.describe_all(udid)
                              if frame(e)[0] >= x0 - 5 and frame(e)[2] > 200)
                if cards == last:
                    break
                last = cards
        return skip(f"no {'/'.join(statuses)} booking at {want_hhmm}")

    # BookingCard.onPress: a booking opens only from 30 min before its start; earlier
    # the app shows this toast and stays on the board.
    _TOO_EARLY_RE = re.compile(r"only be clickable before 30 minutes", re.I)

    def _too_early_toast(self, els: Optional[list] = None) -> bool:
        els = els if els is not None else self._idb_els()
        return any(self._TOO_EARLY_RE.search((e.get("label") or "") + " " + (e.get("id") or ""))
                   for e in els)

    def _window_not_open(self, slot: str) -> bool:
        """Is the booking's start still more than 30 minutes away?

        The app's gate (BookingCard.onPress) compares now with from_time - 30 min,
        both on the UTC clock -- the same as comparing local times here."""
        from datetime import datetime as _dt, timedelta as _td
        m = re.match(r"^(\d{1,2}):(\d{2})", slot or "")
        if not m:
            return False
        now = _dt.now()
        start = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=0, microsecond=0)
        return now < start - _td(minutes=30)

    def _mark_too_early(self, notes: List[str], slot: str, by_clock: bool = False) -> None:
        """The run cannot go on until the booking's window opens: say when."""
        from datetime import datetime as _dt, timedelta as _td
        opens = ""
        m = re.match(r"^(\d{1,2}):(\d{2})", slot or "")
        if m:
            start = _dt.now().replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                      second=0, microsecond=0)
            opens = (start - _td(minutes=30)).strftime("%H:%M")
        ticket = getattr(self, "_booked_ticket", "")
        self._too_early = (
            f"booking {ticket + ' ' if ticket else ''}at {slot or '?'} can only be opened "
            f"from {opens or '30 min before its start'} (the app allows it 30 minutes "
            f"before the start). Stopped at {_dt.now().strftime('%H:%M')} — come back "
            f"after {opens or 'then'} and press Resume from failure.")
        notes.append(f"[WAIT] {self._too_early}")
        if by_clock:
            notes.append("    · the app's message shows for ~3s and was not caught; the tap "
                         "did not open the booking and its start is more than 30 min away")

    def _remember_status(self, text: str) -> None:
        """The opened booking's status ('PAYMENT', 'SERVE', ...), from its row/card."""
        up = (text or "").upper()
        word = next((w for w in self._PANEL_STATUS_WORDS if re.search(rf"\b{w}\b", up)), "")
        self._booking_status = _norm(word)

    # Steps a booking at PAYMENT has already been through: its items were served
    # (and comped) and payment was requested. Measured 2026-10-01: a resumed
    # segment opened booking 4954 at PAYMENT and failed on Select All, which the
    # app disables at that stage, with nothing left to select or serve.
    _DONE_BY_PAYMENT = {"@select_all_items", "@serve_items", "@comp_item", "@notify_payment"}

    def _remember_ticket(self, ticket: str, notes: List[str]) -> None:
        """This run's booking ticket, from the row/card the waiter opened. (The order
        header shows it too, but the table sheet covers the header on first open.)"""
        if re.fullmatch(r"\d{3,6}", ticket or "") and getattr(self, "_booked_ticket", "") != ticket:
            self._booked_ticket = ticket
            notes.append(f"    · booking ticket {ticket}")

    def _note_ticket(self, notes: List[str]) -> None:
        """Remember the open order's ticket number (the header's '4798').

        The kitchen then marks THIS ticket Ready. Measured 2026-09-30: a ticket
        left on the board by a failed run (4837) was first in the queue, and three
        later runs worked on it instead of the order they had just sent."""
        try:
            els = _idbd.describe_all(self._business_udid())
            heads = [e for e in els if e.get("type") == "StaticText"
                     and re.fullmatch(r"\d{3,6}", _idbd.name(e)) and _idbd.frame(e)[1] < 110]
            if len(heads) == 1:
                self._remember_ticket(_idbd.name(heads[0]), notes)
        except Exception:
            logger.debug("could not read the order's ticket number", exc_info=True)

    def _reservation_opened(self, notes: List[str], what: str, slot: str,
                            where: str) -> bool:
        """Did tapping a booking row open the reservation? Verified against markers
        that exist ONLY on the reservation screen. 'closeEventModal' is deliberately
        excluded: it is the events list's OWN close button, so treating it as
        "opened" would report success the instant the list appeared."""
        SIDEBAR_SAFE = ("selectAllItemsBtn", "addItemsBtn", "assignToBtn",
                        "sendToKitchenBtn", "AssignTableBtn", "closeModal",
                        # The iPad's order screen (MEASURED 2026-10-29, run of 15:45: it
                        # opened, none of the above showed, and the step went hunting
                        # in the calendar): its header and controls.
                        "ORDER SUMMARY", "selectAll", "unSelectAll", "modifyTable",
                        "notifyPaymentBtn", "serveItemsBtn", "sendItemsBtn",
                        "assignOrSplitBtn", "closeTableBtn")
        ticket = getattr(self, "_booked_ticket", "")
        # The "only clickable 30 minutes before" toast is up for ~3s, so look at once
        # after the tap and then often; the old 1s-then-read loop could miss it. 25s:
        # on slow staging the order took longer than 16s to draw.
        deadline = time.time() + 25.0
        first = True
        while time.time() < deadline:
            if not first:
                time.sleep(0.3)
            first = False
            els_now = self._idb_els()
            if self._too_early_toast(els_now):
                self._mark_too_early(notes, slot)
                return False
            seen = {e["id"] for e in els_now} | {e["label"] for e in els_now}
            if any(m in seen for m in SIDEBAR_SAFE) or (ticket and ticket in seen) \
                    or self._table_modal_up():
                notes.append(f"    · {what} — opened the {slot} booking from the {where}")
                self._note_ticket(notes)
                return True
        if self._window_not_open(slot):          # the toast came and went unseen
            self._mark_too_early(notes, slot, by_clock=True)
            return False
        notes.append(f"    · {what} — tapped the {slot} row in the {where} but the "
                     f"reservation did not open")
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

        # Already on THIS booking's order screen: nothing to open. MEASURED 2026-10-29
        # (run 93ff5ffa): booking 5568 was open, the step went looking for it on the
        # board and spent minutes in the calendar. The order's header shows its ticket.
        ticket = getattr(self, "_booked_ticket", "")
        if ticket:
            here = _idbd.describe_all(getattr(self, "_cur_udid", "") or self._business_udid())
            if self._has(here, "ORDER SUMMARY") and self._has(here, ticket):
                notes.append(f"[ok] {what} — booking {ticket} is already open")
                return True

        # The bookings list renders a few seconds AFTER the board appears, so a
        # scan fired immediately reads an empty board. Keep this cheap: each
        # _idb_els() is a subprocess, and this step has a 150s ceiling that a
        # 20-iteration poll blows on its own.
        board_empty = False
        for _ in range(5):
            if any("card" in (e.get("label") or "").lower() for e in self._idb_els()):
                break
            time.sleep(2.0)
        else:
            board_empty = True
            notes.append("[warn] booking board still empty — no cards rendered")

        name = CONSUMER_NAME.lower()
        slot = getattr(self, "_booked_slot", "") or ""           # e.g. '18:20'
        ms = re.match(r"^(\d{1,2})", slot)
        hour_lbl = f"{int(ms.group(1)):02d}:00" if ms else ""    # booked hour row, e.g. '18:00'
        bundle = self.business_bundle

        # FAST PATH -- the right-hand panel. The waiter's home lists today's bookings
        # there, each with its exact window ('4776 SERVE 17:05 - 18:05 I2 17:00').
        # When the booked slot is listed, open it straight away: no calendar scroll,
        # no events badge. A few pages of that list are checked (a late booking sits
        # further down); anything else falls back to the calendar route below.
        if slot:
            # An hour's events list may already be open in that panel (its rows DO
            # carry their window in accessibility) -- else read the panel's cards.
            if self._click_sidebar_row(slot, statuses, notes, max_pages=1,
                                       where="events list"):
                if self._reservation_opened(notes, what, slot, "events list"):
                    return True
            elif self._open_from_panel(slot, statuses, what, notes):
                return True
            if getattr(self, "_too_early", None):
                return False                     # the booking's window is not open yet

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
            _h = self._window_h(r)
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
                    h = self._window_h(r)
                    if not h:
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
            h2 = self._window_h(r)
            if not h2:
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

        # One budget for the whole step, so the second search (after a relaunch)
        # cannot run past STEP_TIMEOUT and lose its notes to "step hung".
        _step_budget = time.time() + 0.85 * STEP_TIMEOUT

        def _select_today_and_find(stale_ok: bool = False):
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
            _deadline = min(time.time() + 0.55 * STEP_TIMEOUT, _step_budget)
            for _ in range(14):                     # ~35s for the diner's card to appear/sync
                if time.time() > _deadline:
                    notes.append(f"    · giving up the card search after "
                                 f"{0.55 * STEP_TIMEOUT:.0f}s so the step can report "
                                 f"why, rather than being killed as 'hung'")
                    return None
                if self._table_modal_up():
                    notes.append("    · table sheet was covering the board — dismissing it")
                    self._dismiss_table_modal(r, notes)
                    if time.time() > _deadline:          # a slow dismissal (relaunch)
                        notes.append("    · giving up the card search: dismissing the "
                                     "table sheet used up its time")
                        return None
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
                        if stale_ok:
                            # Measured 2026-09-30: a 16:55 booking was confirmed on
                            # the consumer side and missing here until the app was
                            # relaunched. Polling this board for 132s first is what
                            # pushed the step past its limit -- refresh now.
                            return _BOARD_STALE
                lbl = pick_label(self._idb_els())
                if lbl:
                    return lbl
                time.sleep(2.5)
            return None

        # The sidebar row was tapped directly: the reservation is already opening, so
        # there is no board card to locate or click. Skip straight to verification.
        #
        # This MUST wrap every call to _select_today_and_find(), not just the first.
        # The retry after the relaunch below can return the sentinel too, and the
        # second result used to flow straight into _scroll_into_view(r, label) --
        # which crashed with "'object' object has no attribute 'replace'" and took
        # the whole segment down.
        def _opened_from_sidebar() -> bool:
            return self._reservation_opened(notes, what, slot, "events list")

        # Measured 2026-10-17 (new iPad Pro M5): after the login the board stayed
        # empty and the panel read "Orders Not Found"; the calendar search below then
        # polled the empty board for ~100s before relaunching, and the relaunch found
        # booking 5206 at once. Still empty after the panel check = stale: relaunch now.
        if board_empty and not any("card" in (e.get("label") or "").lower()
                                   for e in self._idb_els()):
            label = _BOARD_STALE
        else:
            label = _select_today_and_find(stale_ok=True)
        if label is _OPENED_VIA_SIDEBAR:
            if _opened_from_sidebar():
                return True
            if getattr(self, "_too_early", None):
                return False
            label = None
        stale = label is _BOARD_STALE
        if stale:
            label = None
        if not label:
            notes.append("    · " + ("the board has not received the booking"
                                     if stale else "card not found")
                         + " — relaunching business app once and retrying")
            if not self._relaunch_business(notes, what):
                notes.append(f"[FAIL] {what} — could not relaunch the business app; not "
                             f"continuing on a board that was never refreshed")
                return False
            time.sleep(8)
            # The fresh board: the My Orders panel first, as on the way in.
            if slot and self._open_from_panel(slot, statuses, what, notes):
                return True
            if getattr(self, "_too_early", None):
                return False
            label = _select_today_and_find()
            if label is _OPENED_VIA_SIDEBAR:
                if _opened_from_sidebar():
                    return True
                label = None
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
            _vw = self._window_w(r)              # bounded (see _window_size)
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
            for i in range(20):                   # ~9s for the summary to render
                if i:
                    time.sleep(0.45)
                if self._too_early_toast():       # up for ~3s only: look often
                    self._mark_too_early(notes, slot)
                    return False
                if opened():
                    # The click landing is NOT the reservation opening — opened() is what
                    # proves it, by the controls the following steps depend on.
                    notes.append("    [card search] reservation opened successfully")
                    notes.append(f"[ok] {what} — opened '{label[:40]}' (auto-scrolled) at "
                                 f"slot '{slot or '?'}'"
                                 + ("" if attempt == 1 else f" (attempt {attempt})"))
                    return True
            if self._window_not_open(slot):
                self._mark_too_early(notes, slot, by_clock=True)
                return False
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

        # MEASURED 2026-10-22 (flow5, run af87b74a): the Wallet opened on the
        # COUPONS sub-tab, not Upcoming -- invites render only under Upcoming
        # (vya-consumer Wallet/index.js), and nothing had asked for Coupon. Cause:
        # Wallet unmounts on every blur (BottomNavigator.js, unmountOnBlur) but a
        # stale route param from an earlier Coupon navigation survives and wins on
        # remount (Wallet/index.js resolveWalletTabName). A bare tap of the Wallet
        # icon can land back there. Force Upcoming before searching for the invite.
        tab_labels = labels()
        if ("walletUpcomingSearchInput" not in tab_labels
                and "walletCouponSearchInput" in tab_labels):
            tab = next((e for e in self._idb_els() if e["label"] == "upcomingBlock"), None)
            if tab is not None:
                self._idb_tap(tab["cx"], tab["cy"])
                for _ in range(6):
                    time.sleep(1.0)
                    if "walletUpcomingSearchInput" in labels():
                        break
                notes.append("    · accept appointment — was on the Coupons sub-tab; "
                             "switched to Upcoming")

        before = labels()
        cards = invites(before)
        if not cards:
            # MEASURED 2026-10-21 (flow5/flow6, runs 8b62a304, 61c5bf06): the Wallet
            # opened with an OLD event expanded -- the previous flow's Completed one --
            # and while an event is open the list's cards are not in the tree. One read
            # then failed the step although the waiter had just saved the invitation.
            # Close the open event, then give the invitation time to arrive (one pull
            # to refresh half-way).
            key = _norm(CONSUMER_NAME)
            close = next((e for e in self._idb_els() if e["label"].endswith("Close")
                          and " " not in e["label"] and _norm(e["label"]).startswith(key)),
                         None)
            if close is not None:
                # The button's own text sits inside it, so a plain tap at its centre
                # presses it (the verified tap would read that text as 'covered').
                self._idb_tap(close["cx"], close["cy"])
                notes.append("    · accept appointment — closed an open event to see the list")
            for i in range(14):                          # ~20s
                time.sleep(1.5)
                els = self._idb_els()
                before = [e["label"] for e in els if e["label"]]
                cards = invites(before)
                if cards:
                    break
                if i == 6:                               # pull to refresh, once
                    app = next((e for e in els if e.get("type") == "Application"), None)
                    if app is not None and app.get("w") and app.get("h"):
                        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
                        _idbd.swipe(udid, app["w"] / 2, app["h"] * 0.30,
                                    app["w"] / 2, app["h"] * 0.75, 0.6)
        if not cards and getattr(self, "_booked_slot", ""):
            # A resumed run: the run it resumes accepted this invitation already, and
            # this run's event is in the Upcoming list (unfolded to read its time).
            slot0 = self._booked_slot
            udid0 = getattr(self, "_cur_udid", "") or self._consumer_udid()
            els0 = self._unfold_event(udid0, slot0, _norm(CONSUMER_NAME))
            if any(_idbd.name(e) == slot0 for e in els0) and any(
                    re.search(r"(menuOrderCard|preOrderCard)$", _idbd.name(e)) for e in els0):
                notes.append(f"[ok] accept appointment — nothing pending: the {slot0} "
                             f"event is already in this diner's Upcoming list (accepted "
                             f"earlier)")
                return True
        if not cards:
            # Nothing pending. Distinguish "already accepted" from "never arrived":
            # an accepted invite is in the upcoming list as '<Restaurant>Card'.
            notes.append(
                "[FAIL] accept appointment — no pending appointment on the wallet "
                f"(no '<name>{self._INVITE_SUFFIX}'). The waiter-created appointment "
                f"never reached this account, or it was already accepted. "
                f"On screen: {before[:18]}")
            return False
        # Several invitations can be pending (MEASURED 2026-10-22: a dashboard run that
        # stopped after creating one left it there), and they all carry the same
        # '<Restaurant>InviteCard' label -- the card hides its own time. The dialog's
        # header does show the booking (TimeLine), so open them in turn and accept the
        # one for THIS run's slot.
        slot = getattr(self, "_booked_slot", "") or ""

        def invite_els() -> List[dict]:
            return sorted((e for e in self._idb_els()
                           if e["label"].endswith(self._INVITE_SUFFIX)), key=lambda e: e["cy"])

        def open_invite(el) -> bool:
            for attempt in range(1, 4):
                self._idb_tap(el["cx"], el["cy"])
                for _ in range(5):                   # ~7.5s for the modal to render
                    time.sleep(1.5)
                    if self._ACCEPT_ID in labels():
                        if attempt > 1:
                            notes.append(f"    · accept appointment — modal opened on "
                                         f"attempt {attempt}")
                        return True
                self._clear_logbox()
                same = [e for e in invite_els() if abs(e["cy"] - el["cy"]) < 20]
                if not same:
                    return False
                el = same[0]
            return False

        def close_invite() -> None:
            x = next((e for e in self._idb_els() if e["label"] == "inviteclose"), None)
            if x is not None:
                self._idb_tap(x["cx"], x["cy"])
                for _ in range(4):
                    time.sleep(1.0)
                    if self._ACCEPT_ID not in labels():
                        break

        def already_accepted() -> bool:
            """This run's slot is already an accepted reservation in the upcoming list:
            its time on the same row as 'RESERVATION' (read from the screen -- the cards
            hide their text from accessibility)."""
            if not slot:
                return False
            from automation.scenarios import screen_text
            udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
            lines = screen_text.read_text(udid) or []
            return any(t[0].strip() == slot and any(
                "RESERVATION" in u[0].upper() and abs(u[2] - t[2]) < 20 for u in lines)
                for t in lines)

        pending = invite_els()
        n_before = len(pending)
        card = pending[0]["label"] if pending else cards[0]
        opened, openable = False, 0
        # This run's slot already accepted -- a resumed run: then the pending card is
        # ANOTHER run's invitation, and accepting it "for" this run is wrong. MEASURED
        # 2026-10-29 (run 8ac098d4, the participant): 11:55 was accepted, the one
        # pending card was an older 11:45 event, and it got accepted.
        done_before = bool(slot and pending) and already_accepted()
        for idx, el in enumerate(pending[:4]):
            # A CANCELLED invitation is listed but disabled (Upcoming.js: disabled=
            # {el?.canceled}); its tap does nothing. MEASURED 2026-10-22: the only card
            # left was a cancelled 11:15 one. Try the next.
            if not open_invite(el):
                continue
            openable += 1
            if (not done_before and (n_before == 1 or not slot)) or re.search(
                    rf"(?<!\d){re.escape(slot)}(?!\d)", " ".join(labels())):
                if n_before > 1:
                    notes.append(f"    · accept appointment — {n_before} invitations pending; "
                                 f"#{idx + 1} is this run's ({slot})")
                opened = True
                break
            close_invite()
        if not opened and openable and not done_before:
            notes.append(f"    · accept appointment — {n_before} invitations pending and "
                         f"none shows {slot}; accepting the first that opens")
            for el in invite_els()[:4]:
                if open_invite(el):
                    opened = True
                    break
        if not opened and already_accepted():
            # MEASURED 2026-10-22 (flow6, 11:45): the waiter's booking was already an
            # accepted reservation before this step ran -- on the shared account someone
            # else (or the app) accepted it. The booking is RESERVED either way.
            notes.append(f"[ok] accept appointment — the {slot} booking is already accepted "
                         f"(it is a reservation in the upcoming list; not accepted by this run)")
            return True
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
            if self._ACCEPT_ID not in now and (len(invites(now)) < n_before
                                               or self._ACCEPT_TOAST in now):
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
        # Count, not label: every pending invitation carries the same label.
        if len(invites(after)) >= n_before:
            notes.append(
                "[FAIL] accept appointment — pending appointment found, but acceptance action "
                f"did not transition to the expected state: still {len(invites(after))} "
                f"pending invitation(s) after ACCEPT (was {n_before}). On screen: {after[:18]}")
            return False
        notes.append(f"[ok] accept appointment — {card!r} accepted; it left the pending list"
                     + (" (booking-confirmed modal dismissed via orderLater)" if post_modal else ""))
        return True

    # vya-consumer Components/Modal/index.js (InvitaionScreenModal): next to ACCEPT is
    # 'eventDecline' -> Wallet/Upcoming.js decline() POSTs
    # /usersapi/api?query=declineInvitation, closes the modal (invVis false) and reloads.
    _DECLINE_ID = "eventDecline"

    def _decline_appointment(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Open THIS run's pending wallet invitation and DECLINE it, then verify it is no
        longer pending. Unlike accept, it never falls back to "the first that opens":
        the consumer account is shared, and declining someone else's invitation would
        destroy their booking -- so the invitation must show this run's slot."""
        tag = "@decline_appointment"
        slot = getattr(self, "_booked_slot", "") or ""

        def labels() -> List[str]:
            return [e["label"] for e in self._idb_els() if e["label"]]

        def invite_els() -> List[dict]:
            return sorted((e for e in self._idb_els()
                           if e["label"].endswith(self._INVITE_SUFFIX)), key=lambda e: e["cy"])

        def modal_up(ls: List[str]) -> bool:
            return any(i in ls for i in ("inviteclose", self._ACCEPT_ID, self._DECLINE_ID))

        def ours(ls: List[str]) -> bool:
            return bool(re.search(rf"(?<!\d){re.escape(slot)}(?!\d)", " ".join(ls)))

        def open_invite(el) -> bool:
            # A cancelled invitation is listed but disabled; its tap opens nothing.
            for _ in range(2):
                self._idb_tap(el["cx"], el["cy"])
                for _ in range(5):
                    time.sleep(1.5)
                    if modal_up(labels()):
                        return True
                self._clear_logbox()
            return False

        def close_invite() -> None:
            x = next((e for e in self._idb_els() if e["label"] == "inviteclose"), None)
            if x is not None:
                self._idb_tap(x["cx"], x["cy"])
                for _ in range(4):
                    time.sleep(1.0)
                    if not modal_up(labels()):
                        break

        if not slot:
            notes.append(f"[FAIL] {tag} — this run booked no slot, so its invitation cannot "
                         f"be told from others on the shared account; refusing to decline")
            return False
        self._clear_logbox()
        ls = labels()
        if "walletUpcomingSearchInput" not in ls and "walletCouponSearchInput" in ls:
            tab = next((e for e in self._idb_els() if e["label"] == "upcomingBlock"), None)
            if tab is not None:
                self._idb_tap(tab["cx"], tab["cy"])
                time.sleep(2.0)
        pending = invite_els()
        for i in range(14):                              # ~20s for the invitation to arrive
            if pending:
                break
            time.sleep(1.5)
            pending = invite_els()
            if i == 6:                                   # pull to refresh, once
                els = self._idb_els()
                app = next((e for e in els if e.get("type") == "Application"), None)
                if app is not None and app.get("w") and app.get("h"):
                    udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
                    _idbd.swipe(udid, app["w"] / 2, app["h"] * 0.30,
                                app["w"] / 2, app["h"] * 0.75, 0.6)
        if not pending:
            notes.append(f"[FAIL] {tag} — no pending invitation on the wallet (no "
                         f"'<name>{self._INVITE_SUFFIX}'). On screen: {labels()[:18]}")
            return False
        n_before = len(pending)
        found = False
        for el in pending[:6]:
            if not open_invite(el):
                continue
            now = labels()
            if ours(now) and self._DECLINE_ID in now:
                found = True
                break
            close_invite()
        if not found:
            notes.append(f"[FAIL] {tag} — none of the {n_before} pending invitation(s) is for "
                         f"this run's slot {slot} with DECLINE offered; nothing declined. "
                         f"On screen: {labels()[:18]}")
            return False
        btn = next((e for e in self._idb_els() if e["label"] == self._DECLINE_ID), None)
        if btn is None:
            notes.append(f"[FAIL] {tag} — {self._DECLINE_ID!r} vanished before it could be "
                         f"tapped. On screen: {labels()[:18]}")
            return False
        self._idb_tap(btn["cx"], btn["cy"])
        closed = False
        for _ in range(12):                              # ~24s: a network round-trip
            time.sleep(2)
            if not modal_up(labels()):
                closed = True
                break
        if not closed:
            notes.append(f"[FAIL] {tag} — tapped DECLINE on the {slot} invitation but the "
                         f"modal stayed up (declineInvitation did not land). "
                         f"On screen: {labels()[:18]}")
            return False
        self._clear_logbox()
        n_after = len(invite_els())
        if n_after < n_before:
            notes.append(f"[ok] {tag} — the {slot} invitation was declined; it left the "
                         f"pending list ({n_before} → {n_after})")
            return True
        # Still listed: a declined invitation may stay as a disabled (cancelled) row.
        # Then it must no longer offer ACCEPT/DECLINE for this slot.
        for el in invite_els()[:6]:
            if not open_invite(el):
                continue
            now = labels()
            if ours(now) and (self._DECLINE_ID in now or self._ACCEPT_ID in now):
                notes.append(f"[FAIL] {tag} — tapped DECLINE and the modal closed, but the "
                             f"{slot} invitation still offers ACCEPT/DECLINE. "
                             f"On screen: {now[:18]}")
                return False
            close_invite()
        notes.append(f"[ok] {tag} — the {slot} invitation was declined; it is still listed "
                     f"but no longer offers ACCEPT/DECLINE")
        return True

    def _wallet_event_shows(self, want: str, notes: List[str]) -> bool:
        """Wallet -> open THIS run's event (TODAY at the booked slot) and check that it
        prints *want*. An open event prints '<dining area> .<duration>', e.g. 'Any .2 hr'
        (vya-consumer Wallet/UpcomingCard.js:3363-3365), so a booking's stored duration
        can be checked here. Other diners book the same slot on the shared account, so
        an event at the slot that lacks *want* is closed and the next one tried."""
        udid = self._consumer_udid()
        name, frame = _idbd.name, _idbd.frame
        tag = f"@wallet_event_shows:{want}"
        slot = getattr(self, "_booked_slot", "") or ""
        if not want or not slot:
            notes.append(f"[FAIL] {tag} — needs a text to look for and this run's booked "
                         f"slot (slot: {slot or 'none'})")
            return False
        pat = re.compile(rf"(?<![\w]){re.escape(want)}(?![\w])", re.I)
        key = _norm(CONSUMER_NAME)
        els = _idbd.describe_all(udid)
        if not any(name(e) == "walletUpcomingSearchInput" for e in els):
            _idbd.tap(udid, ["walletTab", "Wallet"], els)
            els, _ = self._wait_els(udid, lambda es: any(
                name(e) == "walletUpcomingSearchInput" for e in es), 10.0)
        _w, H = _idbd.app_size(els)

        def close_open(es) -> None:
            btn = next((e for e in es if name(e).endswith("Close") and " " not in name(e)
                        and _norm(name(e)).startswith(key)), None)
            if btn is not None:
                self._tap_inside(udid, btn)
                self._wait_els(udid, lambda x: self._event_open(x) is None, 6.0)

        seen: List[str] = []
        for _page in range(4):
            tried_y: set = set()
            for _ in range(10):
                els = _idbd.describe_all(udid)
                ev = self._event_open(els)
                if ev:
                    if ev == ("TODAY", slot):
                        hit = next((name(e) for e in els if pat.search(name(e))), "")
                        if hit:
                            notes.append(f"[ok] {tag} — this run's event (TODAY {slot}) "
                                         f"reads {hit!r}")
                            close_open(els)
                            return True
                        seen.append(f"TODAY {slot} without {want!r}")
                    close_open(els)
                    continue
                cards = [c for c in els if name(c) == self._RESTAURANT_CARD
                         and 190 <= frame(c)[1] + frame(c)[3] / 2 <= H - 110
                         and round(frame(c)[1]) not in tried_y]
                if not cards:
                    break
                card = min(cards, key=lambda c: frame(c)[1])
                tried_y.add(round(frame(card)[1]))
                ok, _how = self._tap_inside(udid, card)
                if ok:
                    self._wait_els(udid, self._event_open, 15.0)
            _idbd.swipe(udid, 200, H * 0.70, 200, H * 0.35, 1.2)     # next page
            time.sleep(1.0)
        notes.append(f"[FAIL] {tag} — no Wallet event TODAY {slot} reads {want!r}; "
                     f"opened at the slot: {', '.join(seen) or 'none'}")
        return False

    # ── Consumer: Checkout on THIS booking's Wallet card ───────────────────
    # vya-consumer Screens/Wallet/UpcomingCard.js: once the waiter has notified
    # payment, the booking's card shows Checkout as `${username}finishedCard`
    # (:4475, onPress -> handlePayingUser) or `${username}checkoutCard` (:4586).
    # `${username}consumerCheckout` (:4345) has NO onPress -- it does nothing. There
    # is no element named 'Checkout'. MEASURED 2026-10-07: the card's Checkout sat at
    # y=834, under the tab bar (780-840), so it must be scrolled into view.
    _CHECKOUT_RE = re.compile(r"(finishedCard|checkoutCard)$")

    # The Wallet lists events as COLLAPSED cards: `<Restaurant>Card` with the status
    # printed just below ('NOW', 'PAYMENT REQUESTED', 'PAYMENT COMPLETED'). Opening
    # one shows its date ('TODAY' or '06 OCT 2026') and time ('15:20') at the top;
    # `<user>Close` folds it back (showDetails(false), UpcomingCard.js:3285).
    # MEASURED 2026-10-07: 'PAYMENT REQUESTED' alone is not this run's event -- the
    # first such card was a 06 OCT 16:20 booking for 4 (90.56€) left from an older run.
    _RESTAURANT_CARD = "NylaiKitchen2Card"
    _DATE_RE = re.compile(r"^(TODAY|\d{1,2} [A-Z]{3} \d{4})$")

    def _event_open(self, es) -> Optional[Tuple[str, str]]:
        """(date, time) of the event card that is OPEN, or None in the list view."""
        name, frame = _idbd.name, _idbd.frame
        if not any(name(e).endswith("Close") and " " not in name(e) for e in es):
            return None
        dates = [e for e in es if self._DATE_RE.match(name(e))]
        if not dates:
            return None
        d = min(dates, key=lambda e: frame(e)[1])
        t = next((e for e in es if re.fullmatch(r"\d{1,2}:\d{2}", name(e))
                  and abs(frame(e)[1] - frame(d)[1]) < 12), None)
        return name(d), (name(t) if t is not None else "")

    def _tap_inside(self, udid: str, e: dict) -> Tuple[bool, str]:
        """Tap *e* when the point under its centre lies INSIDE it. The Wallet's
        cards and Close buttons expose their inner text ('Close'), so a probe at
        the centre returns that child, which idb_driver's strict check reads as
        'covered' (MEASURED 2026-10-07: hit 'Close' inside RoopaDClose). A child
        inside the frame is the element itself; anything reaching outside is not."""
        name, frame = _idbd.name, _idbd.frame
        x, y, w, h = frame(e)
        hit = _idbd._probe(udid, e)
        if not hit:
            return False, "nothing under the point"
        hx, hy, hw, hh = frame(hit)
        if hx >= x - 1 and hy >= y - 1 and hx + hw <= x + w + 1 and hy + hh <= y + h + 1:
            return _idbd.tap_point(udid, x + w / 2, y + h / 2)
        return False, f"covered by {name(hit)!r}"

    @staticmethod
    def _event_code(es) -> str:
        """The booking code an open event prints (e.g. 'MEPHSR'), its identity."""
        return next((_idbd.name(e) for e in es
                     if re.fullmatch(r"[A-Z0-9]{6}", _idbd.name(e))
                     and not _idbd.name(e).isdigit()), "")

    def _consumer_checkout(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Wallet -> open THIS run's event (TODAY at the booked time) -> scroll -> Checkout."""
        udid = self._consumer_udid()
        name, frame = _idbd.name, _idbd.frame
        tag = "@consumer_checkout"
        slot = getattr(self, "_booked_slot", "") or ""
        key = _norm(CONSUMER_NAME)
        els = _idbd.describe_all(udid)
        if not any(name(e) == "walletUpcomingSearchInput" for e in els):
            _idbd.tap(udid, ["walletTab", "Wallet"], els)
            els, _ = self._wait_els(udid, lambda es: any(
                name(e) == "walletUpcomingSearchInput" for e in es), 10.0)
        _w, H = _idbd.app_size(els)

        def ours(ev) -> bool:
            return bool(ev) and ev[0] == "TODAY" and (not slot or ev[1] == slot)

        # The consumer account is SHARED on staging: MEASURED 2026-10-19 another
        # diner's booking sat at this run's exact slot. A pre-ordered event prints
        # its ticket ('TICKET: 5229', UpcomingCard.js:3668); when it is readable and
        # this run knows its own (the waiter noted it), a different one is not ours.
        mine = getattr(self, "_booked_ticket", "")

        def ticket_of(es) -> str:
            for e in es:
                m = re.match(r"^TICKET:\s*(\d{3,6})$", name(e).strip())
                if m:
                    return m.group(1)
            return ""

        def someone_elses(es) -> str:
            tk = ticket_of(es)
            return tk if (mine and tk and tk != mine) else ""

        def close_open(es) -> None:
            btn = next((e for e in es if name(e).endswith("Close") and " " not in name(e)
                        and _norm(name(e)).startswith(key)), None)
            if btn is not None:
                self._tap_inside(udid, btn)
                self._wait_els(udid, lambda x: self._event_open(x) is None, 6.0)

        def visible_cards(es):
            """The restaurant's cards on screen (clear of the header and tab bar),
            PAYMENT REQUESTED first -- Checkout exists only after Notify Payment."""
            out = []
            for c in (e for e in es if name(e) == self._RESTAURANT_CARD):
                cy = frame(c)[1] + frame(c)[3] / 2
                if not (190 <= cy <= H - 110):
                    continue
                bottom = frame(c)[1] + frame(c)[3]
                st = next((name(e) for e in es if bottom - 2 <= frame(e)[1] <= bottom + 30
                           and name(e).isupper() and len(name(e)) > 2), "")
                out.append((c, st))
            return sorted(out, key=lambda cs: (cs[1] != "PAYMENT REQUESTED", frame(cs[0])[1]))

        seen_codes, seen = set(), []
        opened = self._event_open(els)
        if opened and (not ours(opened) or someone_elses(els)):
            seen_codes.add(self._event_code(els))
            other = someone_elses(els)
            seen.append(" ".join(opened) + (f" (ticket {other})" if other else ""))
            close_open(els)
            opened = None
        opens = 0
        for page in range(5):
            if opened:
                break
            tried_y = set()
            while opens < 12:
                els = _idbd.describe_all(udid)
                # MEASURED 2026-10-22 (run 5d85b999): a card tapped on the PRIOR
                # iteration can take longer than the open-wait to render; the old
                # code then saw no cards left (`todo` empty, the open event hides
                # the list) and swiped over it without ever classifying it -- this
                # run's own ticket 5348 was lost that way, in favour of the one
                # decoy (ticket 5347) that happened to open fast. Classify any
                # event already open before looking for more cards to tap.
                ev = self._event_open(els)
                if ev:
                    opens += 1
                    code = self._event_code(els)
                    other = someone_elses(els)
                    if ours(ev) and not other:
                        opened = ev
                        break
                    if code not in seen_codes:
                        seen_codes.add(code)
                        seen.append(" ".join(ev) + (f" (ticket {other})" if other else ""))
                    close_open(els)
                    continue
                todo = [(c, st) for c, st in visible_cards(els)
                        if round(frame(c)[1]) not in tried_y]
                if not todo:
                    break
                card, status = todo[0]
                tried_y.add(round(frame(card)[1]))
                ok, _how = self._tap_inside(udid, card)
                if not ok:
                    continue
                opens += 1
                els, ev = self._wait_els(udid, self._event_open, 15.0)
                if not ev:
                    continue
                code = self._event_code(els)
                other = someone_elses(els)
                if ours(ev) and not other:
                    opened = ev
                    break
                if code not in seen_codes:
                    seen_codes.add(code)
                    seen.append(" ".join(ev) + (f" (ticket {other})" if other else
                                                f" ({status.lower()})" if status else ""))
                close_open(els)
            if not opened:
                _idbd.swipe(udid, 200, H * 0.70, 200, H * 0.35, 1.2)     # next page
                time.sleep(1.0)
        if not opened:
            notes.append(f"[FAIL] {tag} — no {self._RESTAURANT_CARD[:-4]} event TODAY"
                         f"{(' at ' + slot) if slot else ''} in the Wallet; opened: "
                         f"{', '.join(seen) or 'none'}")
            return False
        which = f"the event TODAY {opened[1]}"
        if seen:
            notes.append(f"    · {tag}: skipped {', '.join(seen)} — not this run's event")
        els, btn = self._wait_els(udid, lambda es: next(
            (e for e in es if self._CHECKOUT_RE.search(name(e))
             and _norm(name(e)).startswith(key)), None), 8.0)
        if btn is None:
            # MEASURED 2026-10-17 (flow2, run 56bf81f4): the pre-order was paid by card
            # at booking, so after Notify Payment nothing was left; the event showed
            # 'RoopaDorderCardCompleted' (the paid-in-full proof, see _consumer_epay)
            # and no Checkout. That is the right outcome, not a missing button.
            if any(name(e).endswith("orderCardCompleted") and _norm(name(e)).startswith(key)
                   for e in els):
                self._consumer_paid_in_full = True
                tk = ticket_of(els)
                notes.append(f"[ok] {tag} — {which} is already paid in full (the card reads "
                             f"Completed; the pre-order covered the bill) — no Checkout needed"
                             + (f" (ticket {tk}, this run's)" if tk and tk == mine else ""))
                return True
            notes.append(f"[FAIL] {tag} — opened {which} but it has no Checkout (Checkout "
                         f"shows only after the waiter's Notify Payment)")
            return False
        before = {name(e) for e in els}
        ok, how = _idbd.tap_el(udid, btn, els)               # scrolls it clear of the tab bar
        if not ok and r is not None and self._appium_click_id(r, name(btn)):
            ok, how = True, "Appium click"
        if not ok:
            notes.append(f"[FAIL] {tag} — opened {which} but could not tap Checkout ({how})")
            return False
        els, changed = self._wait_els(
            udid, lambda es: ({name(e) for e in es} - before) or self._toast(es), 8.0)
        new = sorted(n for n in ({name(e) for e in els} - before) if n)[:6]
        toast = self._toast(els)
        if not changed:
            notes.append(f"[FAIL] {tag} — tapped Checkout on {which} ({how}) but nothing "
                         f"opened")
            return False
        notes.append(f"[ok] {tag} — opened {which} and tapped Checkout ({how}); "
                     + (f"the app said {toast!r}" if toast else f"opened: {', '.join(new)}"))
        return True

    # ── Consumer: leave a review on a finished order ───────────────────────
    # AI Agent draft (2026-10-26) — no flow touched this screen before (only
    # `screenBackBtn`, generically, as a stuck-screen fallback). vya-consumer
    # Screens/Wallet/Finished.js: an unreviewed finished card shows a star-rating
    # prompt labelled 'Restuarantrating' (sic); tapping it calls handleRating and
    # navigates straight into Screens/Wallet/Review.js, pre-seeded with whichever
    # star the tap landed on. Review.js's own controls are real, individual ids:
    # `star1`..`star5`, `writeFeedback` (accessibilityLabel only, no testID),
    # `addUploadPhoto`, `submitReview`. Submit is gated ONLY by
    # `disabled={submitting || success}` (Review.js:1228) — there is no check on
    # the star count anywhere in `confirmRating` or the server payload. That is
    # documented app behaviour, not a bug: a low (2-star) review submits exactly
    # like any other, with no extra validation step.
    def _leave_review(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """Wallet → Finished tab → the first order still needing a review →
        pick a low rating on the Review screen → write feedback → Submit."""
        tag = "@leave_review"
        WALLET_MARKERS = {"upcomingBlock", "finishedBlock", "walletUpcomingSearchInput"}

        def labels(els) -> set:
            return ({(e.get("label") or "").strip() for e in els}
                    | {(e.get("id") or "").strip() for e in els})

        def find(els, want):
            return next((e for e in els if (e.get("label") or "").strip() == want), None)

        els = self._idb_els()
        if not (labels(els) & WALLET_MARKERS):
            tab = find(els, "walletTab")
            if tab is None:
                notes.append(f"[FAIL] {tag} — not on the Wallet and no walletTab to open it")
                return False
            self._idb_tap(tab["cx"], tab["cy"])
            time.sleep(1.5)
            els = self._idb_els()
            if not (labels(els) & WALLET_MARKERS):
                notes.append(f"[FAIL] {tag} — tapped walletTab but the Wallet did not open")
                return False

        fin = find(els, "finishedBlock")
        if fin is None:
            notes.append(f"[FAIL] {tag} — the Wallet's Finished tab is not on screen")
            return False
        self._idb_tap(fin["cx"], fin["cy"])
        time.sleep(1.2)

        # The star-rating prompt is hidden once a card has been reviewed or
        # skipped (Finished.js), so the first finished card is not necessarily
        # an actionable one — scroll a few pages looking for one that still is.
        prompt = None
        for _scroll in range(6):
            els = self._idb_els()
            prompt = find(els, "Restuarantrating")
            if prompt is not None:
                break
            try:
                r.run_one("scroll down", 0)
            except Exception:
                break
            time.sleep(0.8)
        if prompt is None:
            notes.append(f"[FAIL] {tag} — no finished order on the Wallet still needs a "
                         f"review (every card is already reviewed/skipped, or the Finished "
                         f"list is empty)")
            return False

        self._idb_tap(prompt["cx"], prompt["cy"])
        opened = False
        for _ in range(10):
            time.sleep(0.8)
            if "submitReview" in labels(self._idb_els()):
                opened = True
                break
        if not opened:
            notes.append(f"[FAIL] {tag} — tapped the star-rating prompt but the Review "
                         f"screen did not open")
            return False

        # Pick the rating EXPLICITLY, by the screen's own real id, rather than
        # trusting whatever the entry tap happened to preset.
        els = self._idb_els()
        star2 = find(els, "star2")
        if star2 is None:
            notes.append(f"[FAIL] {tag} — on the Review screen but 'star2' is not on screen")
            return False
        self._idb_tap(star2["cx"], star2["cy"])
        time.sleep(0.4)

        try:
            fields = r.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "writeFeedback")
            if fields:
                fields[0].click()
                fields[0].send_keys("Automated regression check.")
            r.d.hide_keyboard()
        except Exception:
            pass

        els = self._idb_els()
        submit = find(els, "submitReview")
        if submit is None:
            notes.append(f"[FAIL] {tag} — submitReview is no longer on screen before "
                         f"tapping it")
            return False
        self._idb_tap(submit["cx"], submit["cy"])

        for _ in range(18):
            time.sleep(1.0)
            if "submitReview" not in labels(self._idb_els()):
                notes.append(f"[ok] {tag} — submitted a 2-star review with feedback; "
                             f"Submit was never blocked by the star count (documented "
                             f"behaviour: no minimum rating is enforced before Submit is "
                             f"enabled)")
                return True
        notes.append(f"[FAIL] {tag} — tapped Submit Review but the Review screen never "
                     f"closed (still shows submitReview after ~18s)")
        return False

    # ── Consumer: pay the bill by E-Payment (Stripe) ───────────────────────
    # After Checkout the Wallet shows 'Payment / Select Payment Method' (ePayment,
    # eCash, eFoodVoucher, Total, ePaymentConfirm). ePayment (Payment/
    # PaymentStripe.js) opens Stripe's PaymentSheet; paying succeeds with the toast
    # 'Your order is confirmed!' or fails with an alert 'Error code: ...'. MEASURED
    # 2026-10-07: the sheet showed TEST MODE, 'Visa ending in 4 2 4 2' preselected,
    # 'Pay with Link' and 'Pay €40.31' -- the amount of the card's Total.
    _STRIPE_PAY_RE = re.compile(r"^Pay\s*€\s*([\d.,]+)$")

    def _consumer_epay(self, notes: List[str]) -> bool:
        udid = self._consumer_udid()
        name, tag = _idbd.name, "@consumer_epay"
        has = lambda es, n: any(name(e) == n for e in es)  # noqa: E731

        def pay_btn(es):
            return next((e for e in es if self._STRIPE_PAY_RE.match(name(e))), None)

        if getattr(self, "_consumer_paid_in_full", False):
            notes.append(f"[skip] {tag} — nothing left to pay: @consumer_checkout found the "
                         f"booking already paid in full")
            return True
        els = _idbd.describe_all(udid)
        # Older builds ask first: 'Re-Initiate Checkout?' and 'Me Only' / Proceed.
        for opt in ("YES, RE-CHECKOUT", "Mepay", "proceedPayment"):
            if has(els, opt):
                _idbd.tap(udid, [opt], els)
                els, _ = self._wait_els(udid, lambda es, o=opt: not has(es, o), 6.0)
                notes.append(f"    · {tag}: answered '{opt}'")
        btn = pay_btn(els)
        bill = None
        if btn is None:
            els, up = self._wait_els(udid, lambda es: has(es, "ePayment"), 10.0)
            if not up:
                notes.append(f"[FAIL] {tag} — the payment sheet (Select Payment Method / "
                             f"E-Payment) is not on screen")
                return False
            bill = self._bill_total_idb(els)
            ok, how = _idbd.tap(udid, ["ePayment"], els)
            if not ok:
                notes.append(f"[FAIL] {tag} — could not tap E-Payment ({how})")
                return False
            els, btn = self._wait_els(
                udid, lambda es: pay_btn(es) or next((e for e in es if "error"
                                                      in name(e).lower()), None), 20.0)
            if btn is None or not self._STRIPE_PAY_RE.match(name(btn)):
                err = name(btn) if btn is not None else ""
                notes.append(f"[FAIL] {tag} — tapped E-Payment but Stripe's payment sheet "
                             f"did not open" + (f" ({err!r})" if err else ""))
                return False
        amount = self._STRIPE_PAY_RE.match(name(btn)).group(1).replace(",", ".")
        card = next((name(e) for e in els if name(e).lower().startswith("visa ending")), "")
        if bill is not None and abs(float(amount) - bill) > 0.01:
            notes.append(f"    · {tag}: Stripe asks €{amount} for a bill of €{bill:.2f}")
        # Let Stripe's sheet finish sliding up first: the same frame on two reads.
        # MEASURED 2026-10-22 (flow6, run of 05:44Z): a tap ~1s after the sheet appeared
        # was refused 'covered or scrolled out' with 'Pay €35.02' plainly on screen; on
        # 2026-10-19 a tap that early landed and the sheet ignored it.
        last = None
        for _ in range(8):
            if last is not None and _idbd.frame(btn) == _idbd.frame(last):
                break
            last = btn
            time.sleep(0.5)
            els = _idbd.describe_all(udid) or els
            btn = pay_btn(els) or btn
        ok, how = _idbd.tap_el(udid, btn, els)
        if not ok and self._tap_inside(udid, btn)[0]:
            ok, how = True, "idb, inside its frame"         # its own text answers the probe
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap '{name(btn)}' ({how})")
            return False

        def outcome(es):
            for e in es:
                n = name(e).lower()
                if "order is confirmed" in n or n == "success":
                    return "ok", name(e)
                if n.startswith("error code") or "error code:" in n:
                    return "error", name(e)
            # Closed = Stripe's own header gone too: while it processes, the Pay
            # button turns into a spinner but the sheet (TEST MODE) is still up.
            if not pay_btn(es) and not has(es, "ePayment") and not has(es, "Pay with Link") \
                    and not has(es, "TEST MODE"):
                return "ok", "the payment sheets closed"
            return None

        els, res = self._wait_els(udid, outcome, 10.0)
        if not res and pay_btn(els) is not None:
            # MEASURED 2026-10-17 (flow4, run 141bf152): the sheet listed 'Visa ending
            # in 4 2 4 2' twice and sat for 45s with 'Pay €35.02' still showing -- the
            # tap went in while the sheet was still settling, or with no saved card
            # selected. The button still showing (not Stripe's spinner) means nothing
            # is processing: select the saved card and tap Pay once more.
            visa = next((e for e in els if name(e).lower().startswith("visa ending")), None)
            if visa is not None:
                _idbd.tap_el(udid, visa, els)
                time.sleep(1.0)
                els = _idbd.describe_all(udid) or els
            again = pay_btn(els)
            if again is not None:
                ok2, how2 = _idbd.tap_el(udid, again, els)
                notes.append(f"    · {tag}: the sheet did not react to Pay — "
                             + ("selected the saved card, " if visa is not None else "")
                             + f"tapped Pay again ({how2 if ok2 else 'tap failed: ' + how2})")
            els, res = self._wait_els(udid, outcome, 35.0)
        elif not res:
            els, res = self._wait_els(udid, outcome, 35.0)
        if not res or res[0] != "ok":
            why = res[1] if res else "Stripe's sheet is still open after 45s"
            notes.append(f"[FAIL] {tag} — tapped 'Pay €{amount}' but the payment did not "
                         f"go through ({why!r})")
            return False
        # PROOF on the booking's card, not just closed sheets. MEASURED 2026-10-07:
        # after paying, 'Left to pay' read 0.00€ and the card's button became
        # 'RoopaDorderCardCompleted'.
        def settled(es):
            if any(name(e).endswith("orderCardCompleted") for e in es):
                return "the booking shows Completed"
            lbl = next((e for e in es if name(e).strip().lower() == "left to pay"), None)
            if lbl is not None:
                ly = _idbd.frame(lbl)[1]
                for e in es:
                    m = re.match(r"^\s*([\d.,]+)\s*€\s*$", name(e))
                    if m and abs(_idbd.frame(e)[1] - ly) < 12:
                        return ("Left to pay 0.00 €"
                                if float(m.group(1).replace(",", ".")) == 0 else None)
            return None

        els, proof = self._wait_els(udid, settled, 15.0)
        if not proof:
            left = next((name(e) for e in els if "€" in name(e)), "?")
            notes.append(f"[FAIL] {tag} — Stripe accepted €{amount} but the booking does not "
                         f"show it as paid (still {left!r})")
            return False
        res = ("ok", f"{res[1]}; {proof}")
        notes.append(f"[ok] {tag} — paid €{amount} by E-Payment"
                     + (f" ({card.replace(' ', '').replace('ending', ' ending ')})" if card else "")
                     + f"; {res[1]}")
        return True

    # ── Consumer: the duration slider ──────────────────────────────────────
    # Reservation.js (Oct 2026): Duration is a 4-stop bar -- 1 hr, 2 hr, 3 hr, Not
    # Sure. The knob (a MultiSlider marker) has no id; each label under a stop is a
    # TouchableOpacity `duration1`..`duration4` whose onPress calls the same
    # durationIndexChange as releasing the knob. The knob sits 32pt above its
    # label's top (labels top:32, knob 30pt tall at top:0 of the 52pt bar).
    _DURATION_STOP = {"1": 1, "2": 2, "3": 3, "not_sure": 4, "notsure": 4, "4": 4}

    def _set_duration(self, which: str, notes: List[str]) -> bool:
        """Slide the duration knob to a stop; confirm the time slots appear."""
        udid = self._consumer_udid()
        name, frame = _idbd.name, _idbd.frame
        tag = f"@duration:{which}"
        stop = self._DURATION_STOP.get(_norm(which).replace("hr", "").replace("hrs", ""))
        if stop is None:
            notes.append(f"[FAIL] {tag} — unknown duration (use 1, 2, 3 or not_sure)")
            return False
        els, labels = self._wait_els(udid, lambda es: [
            e for e in es if re.fullmatch(r"duration[1-4]", name(e))], 10.0)
        by = {name(e): e for e in labels}
        if "duration1" not in by or f"duration{stop}" not in by:
            notes.append(f"[FAIL] {tag} — the duration bar is not on screen (is the "
                         f"reservation form open and a dining area chosen?)")
            return False

        # The form gates Duration on a dining area (durationIndexChange toasts
        # 'Please select a dining area first !!' and refuses), and its prompt says
        # which is missing: 'Select a dining area to see available time slots'.
        # MEASURED 2026-10-07 (run 488e1127): 'select Any' was tapped, the
        # selection did not stick (the form re-rendered as its data arrived), and the
        # slide was refused -- reported as "no time slots". Re-select Any first.
        def needs_area(es) -> bool:
            return any("select a dining area" in name(e).lower() for e in es)

        if needs_area(els):
            for _ in range(2):
                ok, how = _idbd.tap(udid, ["Any"])
                els, chosen = self._wait_els(udid, lambda es: not needs_area(es), 6.0)
                if chosen:
                    notes.append(f"    · {tag}: no dining area was selected (the form "
                                 f"still asked for one) — tapped Any again ({how})")
                    break
            else:
                notes.append(f"[FAIL] {tag} — the form asks for a dining area and tapping "
                             f"Any did not select it")
                return False
            by = {name(e): e for e in els if re.fullmatch(r"duration[1-4]", name(e))} or by

        def prompt_gone(es) -> bool:
            # 'Select a duration to see available time slots' goes; slot chips come.
            return not any("select a duration" in name(e).lower() for e in es) and \
                any(re.fullmatch(r"\d{1,2}:\d{2}", name(e)) for e in es)

        # Drag from wherever the knob is now (the stop whose label is selected is
        # not readable, so start from 1 hr -- the default -- and sweep right; the
        # knob only moves if the drag starts on it, so also try from each stop).
        tx = _idbd.centre(by[f"duration{stop}"])[0]
        how = ""
        for start in ("duration1", "duration2", "duration3", "duration4"):
            if start not in by or start == f"duration{stop}":
                continue
            sx, _ = _idbd.centre(by[start])
            ky = frame(by[start])[1] - 17          # the knob's centre line
            _idbd.swipe(udid, sx, ky, tx, ky, 0.8)
            els, ok = self._wait_els(udid, prompt_gone, 4.0)
            if ok:
                how = f"slid the bar from {start[-1]} to stop {stop}"
                break
        if not how:
            # The labels select the same way the slide does (durationIndexChange).
            ok, tap_how = _idbd.tap(udid, [f"duration{stop}"])
            els, ok2 = self._wait_els(udid, prompt_gone, 6.0) if ok else (els, False)
            if not ok2 and needs_area(els):
                # MEASURED 2026-10-17 (run fabc22b9): the form had no area prompt when
                # this step began, then re-rendered as its data arrived and dropped
                # 'Any' -- the slide and the label tap were both refused. Re-select
                # the area now and set the stop again.
                _idbd.tap(udid, ["Any"])
                els, chosen = self._wait_els(udid, lambda es: not needs_area(es), 6.0)
                if chosen:
                    notes.append(f"    · {tag}: the form dropped the dining area after "
                                 f"loading — tapped Any again")
                    ok, tap_how = _idbd.tap(udid, [f"duration{stop}"])
                    els, ok2 = self._wait_els(udid, prompt_gone, 6.0) if ok else (els, False)
            if not ok2:
                toast = self._toast(els)
                prompt = next((name(e) for e in els
                               if name(e).lower().startswith("select a")), "")
                notes.append(f"[FAIL] {tag} — slid the bar and tapped its label but no time "
                             f"slots appeared"
                             + (f"; the form says {prompt!r}" if prompt else
                                "; no prompt either — maybe no slots are left today")
                             + (f" ({toast!r})" if toast else ""))
                return False
            how = f"the slide did not take; tapped the stop's label ({tap_how})"
        label = {1: "1 hr", 2: "2 hr", 3: "3 hr", 4: "Not Sure"}[stop]
        notes.append(f"[ok] {tag} — duration set to {label} ({how}); time slots shown")
        return True

    # ── Consumer: invite guests on the reservation form ────────────────────
    # A guest chip on the My Contacts sheet: `${invite.userName}cancel`, 'Guest 1cancel'
    # (Components/Contacts/ContactListView.js:934).
    _GUEST_CHIP_RE = re.compile(r"^Guest\s*(\d+)\s*cancel$")

    def _adult_count(self, els: List[dict]) -> Optional[int]:
        """The adult count on the reservation form: the bare number drawn between the
        adult '−' and '+' (CustomCounter.js:49 -- a Text with no id). The child stepper
        uses the same counterMinus/counterPlus labels, so take the LEFTMOST pair."""
        name, frame = _idbd.name, _idbd.frame
        minus = [e for e in els if name(e) == "counterMinus"]
        plus = [e for e in els if name(e) == "counterPlus"]
        if not minus or not plus:
            return None
        m = min(minus, key=lambda e: frame(e)[0])
        p = min(plus, key=lambda e: frame(e)[0])
        left, right = frame(m)[0] + frame(m)[2], frame(p)[0]
        cy = frame(p)[1] + frame(p)[3] / 2
        for e in els:
            x, y, w, h = frame(e)
            if re.fullmatch(r"\d{1,2}", name(e)) and left <= x + w / 2 <= right \
                    and abs(y + h / 2 - cy) < max(h, 20):
                return int(name(e))
        return None

    def _invite_guests(self, count: int, notes: List[str],
                       contacts: List[str] = ()) -> bool:
        """Add *count* unnamed guests -- and the phone *contacts* named, e.g. the
        participant -- to the booking from the reservation form.

        Source (vya-consumer, Screens/StoreView/Reservation.js + Components/Contacts):
          * the adult '+' does not count up by itself -- it opens the 'My Contacts'
            sheet (onChange -> openContactsWithPermission, Reservation.js:1350);
          * each tap on the 'Guest (Reserve on your name for others)' row, id
            `guestAdd`, adds one 'Guest N' chip with no name to type
            (ContactListView.js:181);
          * Invite (`inviteUsers`) closes the sheet and sets Persons to 1 + the
            number of invitees (Reservation.js:2478) -- that count is the proof.
        The Guest row is drawn only when the phone has at least one contact the app
        can read; with none the sheet just says 'No Contacts'.

        A named contact (the user's procedure, 2026-10-29): type it into the sheet's
        search bar `contactSearch` (Components/Contacts/Components/SearchBar:116, it
        searches the PHONE's address book) and tap its row -- testID
        `${given}${family}Invite`, spaces stripped (ContactListView.js:344) -- which
        adds the chip `${given}cancel` (:903). The contact must be in the phone's
        Contacts with the account's number; the server then sends that registered
        user the invitation."""
        udid = self._consumer_udid()
        name, frame = _idbd.name, _idbd.frame
        tag = f"@invite_guests:{count}" + "".join(f"+{c}" for c in contacts)

        def wait_for(pick, secs: float):
            deadline = time.time() + secs
            while True:
                els = _idbd.describe_all(udid)
                hit = pick(els)
                if hit or time.time() >= deadline:
                    return els, hit
                time.sleep(0.6)

        def chips(els) -> set:
            return {name(e) for e in els if self._GUEST_CHIP_RE.match(name(e))}

        def sheet_up(els) -> bool:
            return any(name(e) in ("guestAdd", "inviteUsers", "contactSearch") for e in els)

        # 1. The adult '+' -- the LEFT one of the two counterPlus on the form.
        els, pluses = wait_for(lambda es: [e for e in es if name(e) == "counterPlus"], 15.0)
        if not pluses:
            notes.append(f"[FAIL] {tag} — no Persons '+' on screen (is the reservation "
                         f"form open?)")
            return False
        before = self._adult_count(els)
        adult_plus = min(pluses, key=lambda e: frame(e)[0])
        ok, how = _idbd.tap_el(udid, adult_plus, els)
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap the adult '+' ({how})")
            return False

        # 2. The My Contacts sheet, with its Guest row.
        els, add = wait_for(lambda es: next((e for e in es if name(e) == "guestAdd"), None),
                            15.0)
        if add is None:
            texts = " ".join(name(e) for e in els).lower()
            if "no contacts" in texts:
                why = ("the My Contacts sheet says 'No Contacts', so it has no Guest row -- "
                       "the simulator needs a contact with a phone number and contacts "
                       "permission for the app (xcrun simctl privacy <udid> grant contacts "
                       "<bundle>)")
            elif "contacts permission" in texts:
                why = "the app reports contacts permission is denied"
            elif "maximum" in texts:
                why = "the restaurant's maximum persons is already reached"
            else:
                why = "the My Contacts sheet did not open"
            notes.append(f"[FAIL] {tag} — tapped the adult '+' but {why}")
            return False
        notes.append(f"    · {tag}: adult '+' opened My Contacts ({how})")

        # 3. One tap on Guest per guest, each confirmed by its new chip.
        have = chips(els)
        start = len(have)
        while len(have) < start + count:
            n0 = len(have)
            ok, how = _idbd.tap(udid, ["guestAdd"])
            if not ok:
                notes.append(f"[FAIL] {tag} — could not tap the Guest row ({how}); "
                             f"{n0 - start} of {count} guest(s) added")
                return False
            els, _ = wait_for(lambda es: len(chips(es)) > n0, 6.0)
            have = chips(els)
            if len(have) <= n0:
                texts = " ".join(name(e) for e in els).lower()
                why = ("the restaurant's maximum persons is reached"
                       if "maximum" in texts else "no new guest chip appeared")
                notes.append(f"[FAIL] {tag} — tapped Guest but {why}; {n0 - start} of "
                             f"{count} guest(s) added")
                return False
        names = sorted((self._GUEST_CHIP_RE.match(c).group(1) for c in have), key=int)
        if names:
            notes.append(f"    · {tag}: added Guest " + ", Guest ".join(names))

        # 3b. Each named contact: search it, tap its row, see its chip.
        for person in contacts:
            given = person.split()[0]
            row_id = self._squash(person) + "Invite"
            # `contactSearch` is the search bar's container (idb: 'contactSearch \ue62e',
            # a GenericElement) -- a verified fill cannot read it back. Tap it and type
            # (MEASURED 2026-10-29: 'Noolu' left one row, the Button 'Noolu Naga').
            els = _idbd.describe_all(udid)
            bar = next((e for e in els if name(e).startswith("contactSearch")), None)
            ok, how = _idbd.tap_el(udid, bar, els) if bar else (False, "no search bar")
            if not ok:
                notes.append(f"[FAIL] {tag} — could not open the contacts search ({how})")
                return False
            time.sleep(0.8)
            _idbd._idb(["ui", "text", "--udid", udid, given])
            # The row is the contact's name (its testID `${given}${family}Invite` is
            # not what idb reports).
            els, row = wait_for(lambda es: next((e for e in es if name(e) in (person, row_id)
                                                 and frame(e)[3] > 0), None), 8.0)
            if row is None:
                rows = [name(e) for e in els if e.get("type") == "Button"]
                notes.append(f"[FAIL] {tag} — searched {given!r} but {person!r} is not in "
                             f"this phone's contacts (rows: {', '.join(rows) or 'none'}); add "
                             f"them to the simulator's Contacts with their number")
                return False
            ok, how = _idbd.tap_el(udid, row, els)
            els, chip = wait_for(lambda es: self._has(es, f"{given}cancel"), 6.0)
            if not ok or not chip:
                toast = self._toast(els)
                notes.append(f"[FAIL] {tag} — tapped {person} ({how}) but no {given} chip "
                             f"appeared" + (f" ({toast!r})" if toast else ""))
                return False
            notes.append(f"    · {tag}: searched and selected {person}")

        # 4. Invite -- the sheet closes and Persons becomes 1 + invitees.
        ok, how = _idbd.tap(udid, ["inviteUsers"])
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap Invite ({how})")
            return False
        els, gone = wait_for(lambda es: not sheet_up(es), 10.0)
        if not gone:
            notes.append(f"[FAIL] {tag} — tapped Invite but the My Contacts sheet is "
                         f"still open")
            return False
        want = 1 + len(have) + len(contacts)
        els, after = wait_for(lambda es: self._adult_count(es) == want, 6.0)
        got = self._adult_count(els)
        who = f"{len(have)} guest(s)" + "".join(f" + {c}" for c in contacts)
        if got != want:
            notes.append(f"[FAIL] {tag} — invited {who} but Persons reads {got} "
                         f"(expected {want})")
            return False
        notes.append(f"[ok] {tag} — invited {who}; Persons {before} → {got}")
        return True

    # ── Waiter: VOID / COMP / SPLIT on an order row; assign to every profile ──
    # Order Summary rows are react-native-gesture-handler Swipeables
    # (Screens/Event/OrderSummary.js:757). Their VOID / COMP / SPLIT buttons carry no
    # id, only their text (OrderSummary.js:498), and while a row is closed the library
    # moves them 10000pt off-screen (Swipeable.tsx:201) -- so a button inside the
    # screen, level with a row, means THAT row is open.
    _ROW_ACTIONS = ("VOID", "COMP", "SPLIT")
    # Controls named like a profile ('...select') that are not one.
    _NOT_PROFILES = {"selectAll", "unSelect", "unSelectAll"}
    # Words in the toasts these actions answer with when the app refuses
    # (Screens/Event/index.js:1709 splitCall, :1949 compData).
    _TOAST_HINTS = ("can't", "cannot", "please select", "not sent", "already",
                    "out of the quantity", "sorry")

    @staticmethod
    def _pretty(ident: str) -> str:
        """'PennePollo' -> 'Penne Pollo', 'Guest1' -> 'Guest 1'."""
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", ident or "")
        return re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)

    def _wait_els(self, udid: str, pick, secs: float):
        """(els, pick(els)) as soon as pick() is truthy, or at the deadline.

        An empty dump is not a screen: describe_all gives [] when idb times out (20s)
        on an overloaded rig, and "<x> is gone" would pass on it, "<x> shows" fail on
        it. MEASURED 2026-10-29 (run f28812d1, load ~16): the order read as 0 rows
        while it showed all 23. So it is skipped, and the last real screen returned."""
        deadline = time.time() + secs
        last: List[dict] = []
        while True:
            els = _idbd.describe_all(udid)
            if els:
                last = els
                hit = pick(els)
                if hit:
                    return els, hit
            if time.time() >= deadline:
                return last, (pick(last) if last else None)
            time.sleep(0.6)

    @staticmethod
    def _has(els: List[dict], ident: str) -> bool:
        return any(_idbd.name(e) == ident for e in els)

    def _toast(self, els: List[dict]) -> str:
        for e in els:
            t = _idbd.name(e)
            if len(t) > 12 and any(k in t.lower() for k in self._TOAST_HINTS):
                return t
        return ""

    def _order_rows(self, els: List[dict]) -> List[dict]:
        """The open Order Summary's product rows, top first: each is a radio labelled
        `<ProductNameNoSpaces>card` (OrderSummary.js:780). Same-named rows are NOT
        unique -- one dish assigned to four people is four 'PennePollocard' -- so rows
        are handled as elements, never looked up by name. Left panel only: the payment
        panel's 'RoopaDaccordionCard' ends in 'Card', not 'card', and sits right."""
        name, frame = _idbd.name, _idbd.frame
        w, _h = _idbd.app_size(els)
        rows = [e for e in els if name(e).endswith("card") and " " not in name(e)
                and frame(e)[2] > 0 and frame(e)[3] > 0 and frame(e)[0] < w * 0.6]
        return sorted(rows, key=lambda e: frame(e)[1])

    def _same_row(self, els: List[dict], row: dict) -> Optional[dict]:
        """*row* in a fresh snapshot: same name, nearest height. A swipe moves a row
        sideways, never up or down, so its y identifies it among same-named rows."""
        y0 = _idbd.frame(row)[1]
        same = [e for e in self._order_rows(els) if _idbd.name(e) == _idbd.name(row)]
        best = min(same, key=lambda e: abs(_idbd.frame(e)[1] - y0), default=None)
        return best if best is not None and abs(_idbd.frame(best)[1] - y0) < 20 else None

    def _row_line(self, els: List[dict], row: dict, tol: float = 24.0) -> List[dict]:
        """Short elements level with *row* (its price, its COMP mark, its buttons)."""
        frame = _idbd.frame
        cy = frame(row)[1] + frame(row)[3] / 2
        return [e for e in els if frame(e)[3] < 90
                and abs(frame(e)[1] + frame(e)[3] / 2 - cy) <= tol]

    def _dish_total(self, els: List[dict], row_id: str) -> float:
        """Σ the prices beside every order row of one dish (`${name}card`)."""
        total = 0.0
        for row in (e for e in self._order_rows(els) if _idbd.name(e) == row_id):
            p = self._row_price(els, row)
            m = self._MONEY_RE.match(_idbd.name(p) or "") if p is not None else None
            if m:
                total += float(m.group(1).replace(",", "."))
        return round(total, 2)

    def _row_price(self, els: List[dict], row: dict) -> Optional[dict]:
        # The order pane only: a row level with the payment panel's 'Total 442.91 €'
        # (x 1066 on the iPad) took that as its price (MEASURED 2026-10-29, run
        # 710732d6: Penne Pollo read +105.06 € for a 52.53 € split).
        w, _h = _idbd.app_size(els)
        money = [e for e in self._row_line(els, row)
                 if self._MONEY_RE.match(_idbd.name(e) or "")
                 and _idbd.frame(row)[0] < _idbd.frame(e)[0] < w * 0.62]
        return max(money, key=lambda e: _idbd.frame(e)[0], default=None)

    def _row_actions(self, els: List[dict], row: dict) -> Dict[str, dict]:
        """VOID / COMP / SPLIT of *row*, if it is swiped open: on screen, and spanning
        the row's centre line."""
        frame, name = _idbd.frame, _idbd.name
        w, _h = _idbd.app_size(els)
        cy = frame(row)[1] + frame(row)[3] / 2
        out: Dict[str, dict] = {}
        for e in els:
            if name(e) in self._ROW_ACTIONS:
                x, y, ew, eh = frame(e)
                if x >= 0 and x + ew <= w + 1 and y - 4 <= cy <= y + eh + 4:
                    out.setdefault(name(e), e)
        return out

    def _row_into_view(self, udid: str, els: List[dict], row: dict):
        """(els, row) with *row* on screen between the order's header and its ADD
        button -- or (els, None). A swipe on a row that is not on screen starts on the
        screen's EDGE, which iOS takes as a system gesture: MEASURED 2026-10-29 (run
        e786709b) the business app went to the background and the next 'row' swipes
        paged the home screen to the App Library. Rows under ADD open ADD."""
        name, frame = _idbd.name, _idbd.frame
        w, h = _idbd.app_size(els)
        add = next((e for e in els if name(e) == "addItemsBtn" and frame(e)[0] < w * 0.6
                    and 0 < frame(e)[1] < h), None)
        low = (frame(add)[1] if add is not None else h - 140) - 12
        high = 140.0
        for _ in range(6):
            x, y, rw, rh = frame(row)
            if high <= y and y + rh <= low:
                return els, row
            shift = (y + rh / 2) - (high + low) / 2
            mag = max(-(low - high) * 0.8, min((low - high) * 0.8, shift))
            y0 = low - 10 if mag > 0 else high + 10
            # The rows' label column: a vertical drag there scrolls the list.
            _idbd.swipe(udid, x + 120, y0, x + 120, y0 - mag, max(1.0, abs(mag) / 200.0))
            time.sleep(0.7)
            els = _idbd.describe_all(udid)
            want_y = y - mag
            same = [e for e in self._order_rows(els) if name(e) == name(row)]
            if not same:
                return els, None
            row = min(same, key=lambda e: abs(frame(e)[1] - want_y))
        return els, None

    def _open_row_actions(self, udid: str, row: dict):
        """Swipe *row* to the left until its VOID / COMP / SPLIT buttons show.

        The drag starts on the row's PRICE, not its radio: a touch that fails to
        become a pan is a tap, and a tap on the radio would toggle the row's
        selection. The library opens the row once the drag passes half the
        buttons' width (rightThreshold default, Swipeable.tsx:230), and
        overshootRight={false} caps a long drag -- so drag well past it."""
        frame = _idbd.frame
        els = _idbd.describe_all(udid)
        els, row = self._row_into_view(udid, els, row)
        if row is None:
            return els, None, {}
        for attempt in range(3):
            row = self._same_row(els, row) or row
            acts = self._row_actions(els, row)
            if len(acts) == len(self._ROW_ACTIONS):
                return els, row, acts
            w, _h = _idbd.app_size(els)
            rx, ry, rw, rh = frame(row)
            cy = ry + rh / 2
            price = self._row_price(els, row)
            x1 = _idbd.centre(price)[0] if price else min(rx + rw + 200, w * 0.6)
            x2 = max(x1 - 460, 12)
            _idbd.swipe(udid, x1, cy, x2, cy, (0.5, 0.8, 1.2)[attempt])
            time.sleep(1.0)
            els = _idbd.describe_all(udid)
        row = self._same_row(els, row) or row
        return els, row, self._row_actions(els, row)

    def _row_action(self, udid: str, row: dict, action: str, ready, tag: str,
                    notes: List[str]):
        """Swipe *row* open, press *action*, and wait until ready(els) -- its dialog.
        Returns the snapshot with the dialog up, or None (with the reason noted)."""
        els, opened, acts = self._open_row_actions(udid, row)
        if opened is None:
            notes.append(f"[FAIL] {tag} — could not bring the "
                         f"{self._pretty(_idbd.name(row)[:-4])} row onto the screen to swipe it")
            return None
        row = opened
        btn = acts.get(action)
        if btn is None:
            notes.append(f"[FAIL] {tag} — swiped the {self._pretty(_idbd.name(row)[:-4])} "
                         f"row left 3 times but its VOID / COMP / SPLIT buttons did not show")
            return None
        # The dialog this opens hides every control from accessibility (see
        # _OVERLAY_BOX), so the iPad's rotation cannot be measured while it is up.
        # Measure it now, against this screen's named controls.
        self._fresh_rotation(udid)
        ok, how = _idbd.tap_el(udid, btn, els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — the row opened but {action} could not be tapped "
                         f"({how})")
            return None
        # Wait for the dialog itself. Not for 'a toast': a LogBox line or any other
        # stray text would end the wait early and fail a dialog that was opening.
        els, _ = self._wait_els(udid, ready, 8.0)
        if not ready(els):
            toast = self._toast(els) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — tapped {action} but "
                         + (f"the app said {toast!r}" if toast else "its dialog did not open"))
            return None
        time.sleep(0.8)                        # let the dialog finish animating in
        return els

    # ── Dialogs drawn in a magnus Overlay ──────────────────────────────────
    # CompAndVoidModal (VOID / COMP reason), AssignSplitProductModal (SPLIT from a
    # row) and CompAndVoidProductModal (which units) are magnus Overlays, whose
    # backdrop is a touchable -- accessible by default -- so accessibility, idb and
    # Appium alike, sees ONE screen-sized element labelled with every child id run
    # together. MEASURED on the VOID dialog (booking 4947): 'Specify VOID reason
    # entryError customerChangedMind itemUnavailable duplicateOrder
    # allergyDietaryConcern managerOverride voidOtherReasonInput ...
    # assignProductsBtn'. Touches still reach the controls. So each control is found
    # by the text it SHOWS (screen OCR, inside the dialog's box) and tapped there,
    # and the label's ids confirm what happened (a picked profile adds '<Name>close').
    #: (width, height) the source gives each Overlay (Modal/index.js:5831, :2223,
    #: :6005); magnus centres it on the screen.
    _OVERLAY_BOX = {"void": (500, 730), "comp": (500, 700), "split": (550, 350),
                    "units": (600, 600)}
    _REASON_TEXT = {
        "entryError": "Entry Error", "customerChangedMind": "Customer Changed Mind",
        "itemUnavailable": "Item Unavailable", "duplicateOrder": "Duplicate Order",
        "allergyDietaryConcern": "Allergy / Dietary Concern",
        "managerOverride": "Manager Override", "birthday": "Birthday",
        "managerDiscretion": "Manager Discretion", "serviceRecovery": "Service Recovery",
        "birthdayDiscount": "Birthday Discount", "specialEventDiscount": "Special Event Discount",
    }
    # The COMP reasons differ between builds -- MEASURED 2026-10-29 on the installed
    # iPad build: managerDiscretion, specialEventDiscount, birthdayDiscount (older:
    # birthday, managerDiscretion, serviceRecovery). Take the one asked for when the
    # dialog has it, else the first of these it has.
    _COMP_REASONS = ("managerDiscretion", "birthdayDiscount", "birthday",
                     "specialEventDiscount", "serviceRecovery")

    def _comp_reason(self, els: List[dict], wanted: str) -> Optional[str]:
        ov = self._overlay(els, "5Btn")
        ids = _idbd.name(ov).split() if ov is not None else []
        return next((r for r in (wanted,) + self._COMP_REASONS if r in ids), None)

    def _comp_pick(self, udid: str, els: List[dict], reason: str, pct: int, tag: str,
                   notes: List[str]) -> Optional[List[dict]]:
        """On the open COMP dialog: the reason, the pct chip, Apply; then wait for it to
        close. The chips are a horizontal row whose last one, 100 %, sits cut off at
        the dialog's edge (MEASURED 2026-10-29) -- the row is swiped left for it."""
        from automation.scenarios import screen_text
        box = self._box(els, "comp")
        ok, how = self._ocr_tap(udid, box, self._REASON_TEXT[reason])
        if not ok:
            notes.append(f"[FAIL] {tag} — {how}")
            return None
        time.sleep(0.7)
        ok, how = self._ocr_tap(udid, box, f"{pct} %")
        if not ok:
            five = next((t for t in screen_text.read_text(udid, region=box, accurate=True)
                         if self._text_matches(t[0], "5 %")), None)
            if five is not None:
                cy = five[2] + five[4] / 2
                _idbd.swipe(udid, box[2] - 40, cy, five[1] + 10, cy, 0.8)
                time.sleep(0.8)
                ok, how = self._ocr_tap(udid, box, f"{pct} %")
        if not ok:
            notes.append(f"[FAIL] {tag} — {how}")
            return None
        time.sleep(0.7)
        return self._overlay_taps(udid, _idbd.describe_all(udid), "comp", ["Apply"], "5Btn",
                                  tag, notes)

    @staticmethod
    def _overlay(els: List[dict], *markers: str) -> Optional[dict]:
        """The collapsed Overlay whose ids include every one of *markers*, if up."""
        for e in els:
            ids = _idbd.name(e).split()
            if len(ids) > 2 and all(m in ids for m in markers):
                return e
        return None

    def _box(self, els: List[dict], kind: str) -> Tuple[float, float, float, float]:
        w, h = _idbd.app_size(els)
        bw, bh = self._OVERLAY_BOX[kind]
        x0, y0 = max(0.0, (w - bw) / 2), max(0.0, (h - bh) / 2)
        return (x0, y0, min(w, x0 + bw), min(h, y0 + bh))

    @staticmethod
    def _fresh_rotation(udid: str) -> None:
        """Re-measure which way the iPad is turned (idb_coords), so every tap made
        while an Overlay hides the controls uses a measured direction, not the
        fallback guess. Its cache outlives one dialog."""
        try:
            from automation.scenarios import idb_coords
            idb_coords.forget(udid)
            idb_coords.to_device(udid, 1, 1)
        except Exception:
            pass

    # A discount chip as OCR reads it. MEASURED on the COMP dialog (booking 4949):
    # Vision reads '%' as '0/0' ('40 0/0'), and its FAST mode skipped '5 %', '10 %'
    # and '15 %' outright while the accurate mode read all seven.
    _PCT_TEXT_RE = re.compile(r"^\s*(\d{1,3})\s*(?:%|0/0|o/o|°/o|9/0)?\s*$", re.I)

    @classmethod
    def _text_matches(cls, seen: str, text: str) -> bool:
        """*seen* (OCR) is *text*. A radio's circle can read as a leading 'O'
        ('O Customer Changed Mind'); a percentage is matched on its NUMBER, so
        '5 %' never matches '25 %'. Nothing else is fuzzy."""
        pct = re.fullmatch(r"\s*(\d{1,3})\s*%\s*", text)
        if pct:
            m = cls._PCT_TEXT_RE.match(seen)
            return bool(m) and int(m.group(1)) == int(pct.group(1))
        n, want = _norm(seen), _norm(text)
        return n == want or (len(n) == len(want) + 1 and n[0] in "o0" and n[1:] == want)

    def _ocr_tap(self, udid: str, box, text: str) -> Tuple[bool, str]:
        """Tap *text* where the dialog inside *box* shows it: fast OCR (~1s) first,
        the accurate one (~1-5s) when the fast pass does not see it."""
        from automation.scenarios import screen_text
        found = []
        for accurate in (False, True):
            found = screen_text.read_text(udid, region=box, accurate=accurate)
            for t, x, y, w, h in found:
                if self._text_matches(t, text):
                    ok, _how = _idbd.tap_point(udid, x + w / 2, y + h / 2)
                    return ok, (f"{text!r} at ({int(x + w / 2)}, {int(y + h / 2)})"
                                + (" (accurate OCR)" if accurate else ""))
        seen = ", ".join(repr(t) for t, *_ in found[:16]) or "nothing"
        return False, f"{text!r} is not on the dialog (it reads: {seen})"

    @staticmethod
    def _ocr_sees(udid: str, text: str, box=None) -> bool:
        from automation.scenarios import screen_text
        return any(_norm(t) == _norm(text) for t, *_ in screen_text.read_text(udid, region=box))

    def _screen_toast(self, udid: str) -> str:
        """A refusal toast, read off the pixels (the Overlay hides it from idb)."""
        try:
            from automation.scenarios import screen_text
            for t, *_ in screen_text.read_text(udid):
                if len(t) > 12 and any(k in t.lower() for k in self._TOAST_HINTS):
                    return t
        except Exception:
            pass
        return ""

    SEE_SECS = 12.0

    def _see_text(self, text: str, notes: List[str]) -> bool:
        """`@see:<text>` — the outcome a scenario checks, read off the live screen.

        `verify <text>` goes through ScenarioRunner's fuzzy resolver: MEASURED
        2026-10-27 (cc_seat_capacity_guard, run cbc65fce) it answered after 49s, long
        after a toast is gone. This polls the idb tree (~1s a read) for a label that
        CONTAINS the text (case, spacing and punctuation ignored), then reads the
        pixels once: a magnus Overlay hides its text from idb."""
        tag = f"@see:{text}"
        want = _norm(text)
        if not want:
            notes.append(f"[FAIL] {tag} — no text to look for")
            return False
        udid = (getattr(self, "_cur_udid", "") or self._consumer_udid())

        def seen(es) -> str:
            for e in es:
                for label in (e.get("AXLabel") or "", e.get("AXIdentifier") or ""):
                    if want in _norm(label):
                        return label.strip()
            return ""

        _els, hit = self._wait_els(udid, seen, self.SEE_SECS)
        if hit:
            notes.append(f"[ok] {tag} — on screen: {hit[:80]!r}")
            return True
        try:
            from automation.scenarios import screen_text
            hit = next((t for t, *_ in screen_text.read_text(udid, accurate=True)
                        if want in _norm(t)), "")
        except Exception:
            hit = ""
        if hit:
            notes.append(f"[ok] {tag} — on screen (read by OCR): {hit[:80]!r}")
            return True
        notes.append(f"[FAIL] {tag} — not on screen after {self.SEE_SECS:.0f}s "
                     f"(idb, then OCR)")
        return False

    def _overlay_taps(self, udid: str, els: List[dict], kind: str, texts: List[str],
                      marker: str, tag: str, notes: List[str]) -> Optional[List[dict]]:
        """Tap *texts* in order on the open Overlay, then wait for it to close
        (its *marker* id gone). Returns the snapshot after, or None (noted)."""
        box = self._box(els, kind)
        done = []
        for text in texts:
            ok, how = self._ocr_tap(udid, box, text)
            if not ok:
                notes.append(f"[FAIL] {tag} — {how}")
                return None
            done.append(how)
            time.sleep(0.7)
        els, gone = self._wait_els(udid, lambda es: not self._overlay(es, marker), 6.0)
        if not gone:
            toast = self._toast(els) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — tapped {', '.join(done)} but the dialog stayed "
                         f"open" + (f"; the app said {toast!r}" if toast else ""))
            return None
        notes.append(f"    · {tag}: tapped {', '.join(done)}")
        return els

    def _units_dialog(self, udid: str, els: List[dict], marker: str, tag: str,
                      notes: List[str]) -> Optional[List[dict]]:
        """A line with quantity > 1 asks which units (CompAndVoidProductModal):
        'Next' -- `ApplyBtn` after VOID, `assignProductsBtn` after COMP."""
        els, ov = self._wait_els(udid, lambda es: self._overlay(es, marker), 2.5)
        if ov is None:
            return els
        time.sleep(0.8)
        return self._overlay_taps(udid, els, "units", ["Next"], marker, tag, notes)

    def _profiles(self, els: List[dict]) -> List[dict]:
        """Profile avatars on an assign / split / pay-for dialog, left to right:
        `${username}select`, spaces stripped -- 'RoopaDselect', 'Guest1select'
        (Components/Modal/index.js:1718, :2297, :5564)."""
        name, frame = _idbd.name, _idbd.frame
        w, h = _idbd.app_size(els)
        out = [e for e in els if name(e).endswith("select") and " " not in name(e)
               and name(e) not in self._NOT_PROFILES and frame(e)[2] > 0
               and 0 <= frame(e)[0] + frame(e)[2] / 2 <= w and 0 <= frame(e)[1] <= h]
        return sorted(out, key=lambda e: (frame(e)[1] // 40, frame(e)[0]))

    @staticmethod
    def _picked(els: List[dict], prof: dict) -> bool:
        """A selected profile shows its remove X: `${username}close`."""
        want = _idbd.name(prof)[:-len("select")] + "close"
        return any(_idbd.name(e) == want for e in els)

    def _select_profiles(self, udid: str, els: List[dict], which: str, tag: str,
                         notes: List[str]):
        """Select profiles on the open dialog: which='all' or 'one' (the first one
        not yet selected). Each tap is confirmed by the profile's X appearing.
        Returns (els, [pretty names now selected]) or (els, None) on failure."""
        if which == "all":
            # The dialog can still be laying out its avatars when it is first read.
            # MEASURED 2026-10-19 (run 518bffc6): "all" picked only Guest 1 of three.
            last = None
            for _ in range(6):
                now = tuple(_idbd.name(e) for e in self._profiles(els))
                if now and now == last:
                    break
                last = now
                time.sleep(0.5)
                els = _idbd.describe_all(udid) or els
        profiles = self._profiles(els)
        if not profiles:
            notes.append(f"[FAIL] {tag} — the dialog shows no profiles to pick")
            return els, None
        todo = [p for p in profiles if not self._picked(els, p)]
        if which == "one":
            todo = todo[:1]
        for prof in todo:
            nm = _idbd.name(prof)
            cur = next((e for e in self._profiles(els) if _idbd.name(e) == nm), prof)
            ok, how = _idbd.tap_el(udid, cur, els)
            if not ok:
                notes.append(f"[FAIL] {tag} — could not tap {self._pretty(nm[:-6])} ({how})")
                return els, None
            els, done = self._wait_els(udid, lambda es: self._picked(es, cur), 5.0)
            if not done:
                notes.append(f"[FAIL] {tag} — tapped {self._pretty(nm[:-6])} but it did not "
                             f"get selected")
                return els, None
        picked = [self._pretty(_idbd.name(p)[:-6]) for p in self._profiles(els)
                  if self._picked(els, p)]
        return els, picked

    def _void_item(self, notes: List[str], reason: str = "entryError") -> bool:
        """Swipe the first order row -> VOID -> a reason -> Apply.

        CompAndVoidModal (Components/Modal/index.js:5742) preselects NO reason --
        Apply with none is refused 'Please select required options'. It is an
        Overlay (see _OVERLAY_BOX): the reason and Apply are tapped by their text.
        A line with quantity > 1 then asks which units ('Next', `ApplyBtn`). Done
        when the dish is listed under 'Void Products'."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, tag = _idbd.name, "@void_item"
        els, rows = self._wait_els(udid, self._order_rows, 8.0)
        if not rows:
            notes.append(f"[FAIL] {tag} — the order has no item rows to void")
            return False
        row, before = rows[0], len(rows)
        dish = self._pretty(name(row)[:-4])
        els = self._row_action(udid, row, "VOID", lambda es: self._overlay(es, reason),
                               tag, notes)
        if els is None:
            return False
        els = self._overlay_taps(udid, els, "void", [self._REASON_TEXT[reason], "Apply"],
                                 reason, tag, notes)
        if els is None:
            return False
        els = self._units_dialog(udid, els, "ApplyBtn", tag, notes)
        if els is None:
            return False
        els, done = self._wait_els(
            udid, lambda es: any(name(e).strip().lower() == "void products" for e in es)
            and len(self._order_rows(es)) < before, 10.0)
        if not done:
            notes.append(f"[FAIL] {tag} — the VOID dialog closed but {dish} was not moved to "
                         f"Void Products ({len(self._order_rows(els))} rows, was {before})")
            return False
        notes.append(f"[ok] {tag} — voided {dish} (reason: {self._pretty(reason).lower()}); "
                     f"it is listed under Void Products")
        return True

    def _sheet_products(self, els: List[dict]) -> List[dict]:
        """Product rows on the ADD NEW ITEM sheet, top first: `${name}Item`, spaces
        stripped (AddNewItem.js:89). Category chips share the suffix, so keep the
        products' own column -- the x shared by the most 'Item' elements (the rule
        _add_items_sheet_products measured)."""
        name, frame = _idbd.name, _idbd.frame
        cand = [e for e in els if name(e).endswith("Item") and frame(e)[2] > 0
                and name(e) not in ("addNewItemClose",)]
        if not cand:
            return []
        xs: Dict[float, int] = {}
        for e in cand:
            xs[frame(e)[0]] = xs.get(frame(e)[0], 0) + 1
        col = max(xs, key=lambda k: (xs[k], -k))
        return sorted((e for e in cand if frame(e)[0] == col), key=lambda e: frame(e)[1])

    def _add_item_for_all(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """ADD -> one dish -> ASSIGN / SPLIT -> select EVERY profile -> Assign.

        Assign with several people selected gives each of them one of the dish
        (assaignProducts, Screens/Event/index.js:1121) -- one row per person. The two
        buttons of AssignProductModal share the id `assignProductsBtn`
        (Components/Modal/index.js:1802, :1827): Assign is the LEFT one. A dish not
        yet on the order is preferred so its rows are countable."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, frame, tag = _idbd.name, _idbd.frame, "@add_item_for_all"
        els = _idbd.describe_all(udid)
        # Counted HERE: the sheet replaces the order panel while it is open.
        rows_before = [name(e) for e in self._order_rows(els)]
        on_order = {n[:-4] for n in rows_before}
        ok, how = _idbd.tap(udid, ["addItemsBtn"], els)
        if not ok and not self._appium_click_id(r, "addItemsBtn"):
            notes.append(f"[FAIL] {tag} — could not tap ADD ({how})")
            return False
        els, products = self._wait_els(udid, self._sheet_products, 15.0)
        if not products:
            notes.append(f"[FAIL] {tag} — the ADD NEW ITEM sheet "
                         + ("has no products" if self._add_items_sheet_up() else "did not open"))
            return False
        prod = next((p for p in products if name(p)[:-4] not in on_order), products[0])
        dish = name(prod)[:-4]
        before = rows_before.count(dish + "card")
        ok, how = _idbd.tap_el(udid, prod, els)
        if not ok and not self._appium_click_id(r, name(prod)):
            notes.append(f"[FAIL] {tag} — could not tap {self._pretty(dish)} ({how})")
            return False
        # A dish with options opens 'Add Options' first (handleProductFeature,
        # Screens/Event/index.js:844); it is staged only after its Apply. Staged =
        # its row in the sheet's Summary, `${name}card` (AddNewItemSummary.js:173) --
        # NOT assignToBtn's enabled flag, which accessibility reports as true while
        # the button is disabled (the same trap as the kitchen's Ready).
        def staged(es):
            return any(name(e) == dish + "card" for e in es)

        els, _ = self._wait_els(udid, lambda es: staged(es)
                                or self._has(es, "applyOptionBtn"), 8.0)
        if not staged(els) and self._has(els, "applyOptionBtn"):
            _idbd.tap(udid, ["applyOptionBtn"], els)
            els, _ = self._wait_els(udid, staged, 8.0)
        if not staged(els):
            notes.append(f"[FAIL] {tag} — tapped {self._pretty(dish)} but it did not land "
                         f"in the sheet's Summary")
            return False
        ok, how = _idbd.tap(udid, ["assignToBtn"])
        if not ok:
            notes.append(f"[FAIL] {tag} — added {self._pretty(dish)} but ASSIGN / SPLIT could "
                         f"not be tapped ({how})")
            return False
        els, up = self._wait_els(udid, lambda es: self._profiles(es)
                                 and self._has(es, "assignProductsBtn"), 10.0)
        if not up:
            notes.append(f"[FAIL] {tag} — tapped ASSIGN / SPLIT but the 'Assign to or split "
                         f"among' dialog did not open")
            return False
        els, picked = self._select_profiles(udid, els, "all", tag, notes)
        if picked is None:
            return False
        btns = sorted((e for e in els if name(e) == "assignProductsBtn"),
                      key=lambda e: frame(e)[0])
        ok, how = _idbd.tap_el(udid, btns[0], els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap Assign ({how})")
            return False
        want = before + len(picked)

        def landed(es):
            return not any(name(e) in self._ADD_ITEM_SHEET for e in es) and \
                sum(1 for e in self._order_rows(es) if name(e) == dish + "card") >= want

        els, done = self._wait_els(udid, landed, 15.0)
        if not done:
            # MEASURED 2026-10-17 (run 9b4b29bf, a slow-staging run): 0 rows after
            # 15s, where eight earlier runs on this iPad saw all 4 in 14-22s. Still
            # in the dialog = the Assign tap was lost: tap it again. Dialog gone =
            # the order is late to refresh: give it longer.
            again = sorted((e for e in els if name(e) == "assignProductsBtn"),
                           key=lambda e: frame(e)[0])
            if again:
                ok2, how2 = _idbd.tap_el(udid, again[0], els, scroll=False)
                notes.append(f"    · {tag}: the Assign dialog was still open after 15s — "
                             + ("tapped Assign again" if ok2 else
                                f"Assign again was refused ({how2}); waiting"))
            els, done = self._wait_els(udid, landed, 20.0)
        got = sum(1 for e in self._order_rows(els) if name(e) == dish + "card")
        if not done:
            toast = self._toast(els)
            notes.append(f"[FAIL] {tag} — assigned {self._pretty(dish)} to {len(picked)} "
                         f"profile(s) but the order shows {got} row(s) of it, expected {want}"
                         + (f" ({toast!r})" if toast else ""))
            return False
        self._assigned_all = dish + "card"
        notes.append(f"[ok] {tag} — added {self._pretty(dish)} and assigned it to every "
                     f"profile ({', '.join(picked)}): {got} row(s) on the order")
        return True

    # An option radio on the B-App's options pane. The source names it `${name}Btn`
    # + (price ? ' ' + price : '') (SelectProductOptions.js:415, :506); the INSTALLED
    # build names it `${name}CheckBtn` (MEASURED 2026-10-29: largeCheckBtn,
    # vegCheckBtn, nonvegCheckBtn, InsalataconPolloCheckBtn, x≈807 on the pane).
    # Name stripped of spaces and non-ASCII.
    _W_OPTION_RE = re.compile(r"^(?P<opt>.+?)(?:CheckBtn|Check|Btn(?: -?\d+(?:\.\d+)?)?)$")
    # Controls ending like an option that are not one (the iPad's side bar, the pane).
    _W_NOT_OPTIONS = {"homeBtn", "historyBtn", "menuBtn", "applyOptionBtn", "closeOptionBtn",
                      "typeSplInstructionBtn", "assignToBtn", "addItemsBtn", "orderFilterBtn"}

    @staticmethod
    def _option_ticked(udid: str, row: dict, els: List[dict]) -> Optional[bool]:
        """Is this options-pane radio ticked? The tree does not say; the pixels do.
        MEASURED 2026-10-29 on the iPad pane (row x807, w173-207): the radio sits 40-52
        pt in from the row's left edge on its centre line -- a ticked one has a dark dot
        there (44-48 pt), an empty one only a pale ring. None = could not tell."""
        x, y, _w, h = _idbd.frame(row)
        cols = _idbd.sample_colors(udid, [(x + dx, y + h / 2) for dx in range(36, 58, 2)],
                                   els, radius=1.0)
        dark = [_idbd.is_dark(c, 200) for c in cols]
        if all(v is None for v in dark):
            return None
        return any(dark[i] and dark[i + 1] for i in range(len(dark) - 1))

    def _pane_into_view(self, udid: str, els: List[dict], row: dict, again):
        """Scroll the options pane until *row* sits between its top and the floating
        Apply. MEASURED 2026-10-29 (run 2ff7c13b): the pane opened at its top with
        'large', 'veg', 'non veg' below Apply (y 779-967 on an 834-high screen) --
        taps there pressed nothing and the pixel check read Apply's colour.
        *again(els)* finds the row in a fresh read."""
        name, frame = _idbd.name, _idbd.frame
        for _ in range(4):
            apply_ = next((e for e in els if name(e) == "applyOptionBtn"), None)
            low = (frame(apply_)[1] if apply_ is not None else _idbd.app_size(els)[1]) - 12
            high = 110.0
            x, y, w, h = frame(row)
            if high <= y and y + h <= low:
                return els, row
            mid = (high + low) / 2
            shift = (y + h / 2) - mid
            y0 = low - 10 if shift > 0 else high + 10
            _idbd.swipe(udid, x + min(w, 120) / 2, y0, x + min(w, 120) / 2, y0 - shift,
                        max(1.0, abs(shift) / 200.0))
            time.sleep(0.6)
            els = _idbd.describe_all(udid)
            row = again(els) or row
        return els, row

    def _summary_qty(self, udid: str, row_id: str, qty: int, tag: str,
                     notes: List[str]) -> bool:
        """Raise a staged line to *qty* with the Summary's '+' (the user's procedure,
        2026-10-29: add once, then '+', not one tap per unit).

        MEASURED on the installed build: the Summary row is `<pid>card`, `removeItem`
        or `decrementQuantity`, its count, `incrementQuantity`. That '+' opens the
        'Custom Selections' panel (AddNewItemSummary.js:723-762) -- one element
        '<name> <price> € Minus <n> Plus', whose own '+' is found by OCR and tapped
        until the label reads *qty* -- and the panel closes by its handle (49 pt above
        the 'Custom Selections' title, centred on the line). Done when the Summary's
        count reads *qty*."""
        from automation.scenarios import screen_text
        name, frame = _idbd.name, _idbd.frame
        els = _idbd.describe_all(udid)
        row = next((e for e in els if name(e) == row_id and frame(e)[0] > 700), None)
        if row is None:
            notes.append(f"[FAIL] {tag} — the Summary has no {row_id!r} line")
            return False
        cy = frame(row)[1] + frame(row)[3] / 2
        plus = next((e for e in els if name(e) == "incrementQuantity"
                     and abs(frame(e)[1] + frame(e)[3] / 2 - cy) < 20), None)
        if plus is None:
            notes.append(f"[FAIL] {tag} — the Summary line has no '+' (incrementQuantity)")
            return False
        _idbd.tap_el(udid, plus, els, scroll=False)
        line_re = re.compile(r"Minus (\d+) Plus")

        def line(es):
            return next((e for e in es if line_re.search(name(e) or "")), None)

        els, ln = self._wait_els(udid, line, 6.0)
        if ln is None:
            notes.append(f"[FAIL] {tag} — the Summary's '+' did not open Custom Selections")
            return False
        x, y, w, h = frame(ln)
        for _ in range(qty + 2):
            n = int(line_re.search(name(ln)).group(1))
            if n >= qty:
                break
            # OCR the panel from its title down: cropped to the line alone, Vision
            # dropped the 'X' and '+' glyphs (MEASURED 2026-10-29, run ecacfff9).
            # Failing a '+', the count's own position: '+' sits ~30 pt right of it.
            words = screen_text.read_text(udid, region=(max(0, x - 15), max(0, y - 45),
                                                         x + w, y + h + 17), accurate=True)
            spot = next(((t[1] + t[3] / 2, t[2] + t[4] / 2) for t in words
                         if t[0].strip() == "+"), None) or next(
                ((t[1] + t[3] / 2 + 30, t[2] + t[4] / 2) for t in words
                 if t[0].strip() == str(n) and t[1] > x + w * 0.3), None)
            if spot is None:
                notes.append(f"[FAIL] {tag} — Custom Selections shows no '+' to press "
                             f"(count {n}; read: {[t[0] for t in words][:8]})")
                return False
            _idbd.tap_point(udid, *spot)
            els, ln2 = self._wait_els(
                udid, lambda es: line(es) if line(es) is not None and int(
                    line_re.search(name(line(es))).group(1)) > n else None, 6.0)
            if ln2 is None:
                notes.append(f"[FAIL] {tag} — pressed '+' in Custom Selections but the "
                             f"count stayed {n}")
                return False
            ln = ln2
        title = next((e for e in els if name(e) == "Custom Selections"), None)
        hy = (frame(title)[1] - 49) if title is not None else y - 71
        _idbd.tap_point(udid, x + w / 2, hy)
        els, closed = self._wait_els(udid, lambda es: line(es) is None
                                     and self._has(es, row_id), 6.0)
        got = self._tile_count_between(els, row_id)
        if not closed or got != qty:
            notes.append(f"[FAIL] {tag} — set {qty} in Custom Selections but the Summary "
                         f"reads {got}" + ("" if closed else " (the panel stayed open)"))
            return False
        notes.append(f"    · {tag}: quantity {qty} via the Summary's '+'")
        return True

    @staticmethod
    def _tile_count_between(els: List[dict], row_id: str) -> Optional[int]:
        """The count on a Summary line: the number level with `<pid>card`, right of it."""
        name, frame = _idbd.name, _idbd.frame
        row = next((e for e in els if name(e) == row_id and frame(e)[0] > 700), None)
        if row is None:
            return None
        cy = frame(row)[1] + frame(row)[3] / 2
        nums = [e for e in els if (name(e) or "").isdigit() and frame(e)[0] > frame(row)[0]
                and abs(frame(e)[1] + frame(e)[3] / 2 - cy) < 20]
        return int(name(nums[0])) if nums else None

    def _w_add_item(self, r: ScenarioRunner, spec: str, notes: List[str]) -> bool:
        """`@w_add:<Product>*<qty>[option, …] > assign|split: Name, Name` — the waiter
        adds a NAMED product, *qty* units with these options, and gives it to the
        profiles named: Assign (each gets the line) or Split (they share it).

        ADD NEW ITEM (Screens/Event/AddNewItem.js): search `addNewItemInput`, product
        `${name}Item` (spaces stripped, :89). A product carrying features/modifiers
        opens the options pane (Event/index.js:844) -- radios `${name}Btn ${price}`,
        then `applyOptionBtn` (SelectProductOptions.js:672) -- and the SAME product
        with the same options again adds one more unit (Event/index.js:908). So each
        unit is one tap (+ options + Apply). `assignToBtn` opens AssignProductModal:
        profiles `${username}select` (spaces stripped; guests 'Guest1select'), and
        two `assignProductsBtn`: LEFT Assign (≥1), RIGHT Split (≥2) (Modal:1804,
        :1829). Done when the order shows a row of the dish for each profile."""
        tag = f"@w_add:{spec}"
        left, _, right = (spec or "").partition(">")
        m = self._C_ADD_RE.match(left)
        mode, _, who = right.partition(":")
        mode = mode.strip().lower()
        people = [p.strip() for p in who.split(",") if p.strip()]
        if not m or mode not in ("assign", "split") or not people \
                or (mode == "split" and len(people) < 2):
            notes.append(f"[FAIL] {tag} — write it as <Product>*<qty>[options] > "
                         f"assign|split: Name, Name (split needs 2+)")
            return False
        product, qty = m.group("name").strip(), int(m.group("qty") or 1)
        opts = [o.strip() for o in (m.group("opts") or "").split(",") if o.strip()]
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, frame = _idbd.name, _idbd.frame
        pid = self._squash(product)          # `${name}Item`, spaces stripped (AddNewItem.js:89)
        row_id = pid + "card"
        els = _idbd.describe_all(udid)
        before = sum(1 for e in self._order_rows(els) if name(e) == row_id)
        euros0 = self._dish_total(els, row_id)

        # 1. The ADD NEW ITEM sheet, searched for the product.
        if not any(name(e) in self._ADD_ITEM_SHEET for e in els):
            ok, how = _idbd.tap(udid, ["addItemsBtn"], els)
            if not ok and not self._appium_click_id(r, "addItemsBtn"):
                notes.append(f"[FAIL] {tag} — could not tap ADD ({how})")
                return False
            els, _ = self._wait_els(udid, self._sheet_products, 15.0)
        # The search (`addNewItemInput`, a real TextField: AddNewItem.js:136) narrows
        # the list; the whole list is in the tree anyway, so a search that did not
        # take still leaves the product reachable by its id.
        searched = ""
        if self._has(els, "addNewItemInput"):
            text = self._search_text(product)
            ok, _how = _idbd.fill(udid, "addNewItemInput", text)
            searched = f"searched {text!r}; " if ok else "the search did not take; "
            if not ok:                      # never leave a half-typed filter behind
                _idbd.fill(udid, "addNewItemInput", "")
        find = lambda es: next((e for e in es if name(e) == pid + "Item"), None)  # noqa: E731
        els, item = self._wait_els(udid, find, 8.0)
        if item is None:
            seen = [name(e)[:-4] for e in self._sheet_products(els)][:10]
            notes.append(f"[FAIL] {tag} — {product!r} is not on the ADD NEW ITEM sheet "
                         f"({searched}products shown: {', '.join(seen) or 'none'})")
            return False

        # 2. The product once, with its options; then its quantity in the Summary.
        def option_rows(es):
            rows = [e for e in es if self._W_OPTION_RE.match(name(e) or "")
                    and " " not in name(e).split("Btn")[0]
                    and name(e) not in self._W_NOT_OPTIONS and frame(e)[0] > 100]
            checks = [e for e in rows if re.search(r"Check(?:Btn)?$", name(e))]
            return checks or rows

        ok, how = _idbd.tap_el(udid, item, None)
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap {product!r} ({how})")
            return False
        # Every product opens the options pane -- one with no options too (an empty
        # list is truthy: Event/index.js:844), then it is only Apply.
        els, pane = self._wait_els(udid, lambda es: self._has(es, "applyOptionBtn")
                                   or self._has(es, row_id), 8.0)
        if self._has(els, "applyOptionBtn"):
            # The pane slides in: a tap while it moves is lost. MEASURED 2026-10-29
            # (run 07804c12): 'veg' took, 'large' (tapped first) did not.
            time.sleep(0.8)
            els = _idbd.describe_all(udid)
            for opt in opts:
                want = re.sub(r"[^\x00-\x7f]", "", self._squash(opt)).lower()

                def opt_row(es):
                    return next((e for e in option_rows(es) if self._W_OPTION_RE.match(
                        name(e)).group("opt").lower() == want), None)

                row = opt_row(els)
                if row is None:
                    offered = sorted({self._W_OPTION_RE.match(name(e)).group("opt")
                                      for e in option_rows(els)})
                    notes.append(f"[FAIL] {tag} — the options pane has no {opt!r} "
                                 f"(it offers: {', '.join(offered) or 'none'})")
                    return False
                els, row = self._pane_into_view(udid, els, row, opt_row)
                if self._option_ticked(udid, row, els):
                    continue
                for _ in range(2):
                    _idbd.tap_el(udid, row, els, scroll=False)
                    time.sleep(0.6)
                    els = _idbd.describe_all(udid)
                    row = opt_row(els) or row
                    if self._option_ticked(udid, row, els) is not False:
                        break
                else:
                    notes.append(f"[FAIL] {tag} — tapped option {opt!r} twice but its "
                                 f"radio stays empty")
                    return False
            _idbd.tap(udid, ["applyOptionBtn"], els)
            els, staged = self._wait_els(udid, lambda es: not self._has(es, "applyOptionBtn")
                                         and self._has(es, row_id), 8.0)
            if not staged:
                notes.append(f"[FAIL] {tag} — Apply on the options pane did nothing: a "
                             f"required option is missing (picked: {', '.join(opts) or 'none'})")
                return False
        elif not self._has(els, row_id):
            notes.append(f"[FAIL] {tag} — tapped {product!r} but it did not reach the "
                         f"sheet's Summary")
            return False
        if qty > 1 and not self._summary_qty(udid, row_id, qty, tag, notes):
            return False

        # 3. ASSIGN / SPLIT to the named profiles.
        ok, how = _idbd.tap(udid, ["assignToBtn"])
        if not ok:
            notes.append(f"[FAIL] {tag} — added {qty} × {product} but ASSIGN / SPLIT could not "
                         f"be tapped ({how})")
            return False
        els, up = self._wait_els(udid, lambda es: self._profiles(es)
                                 and self._has(es, "assignProductsBtn"), 10.0)
        if not up:
            notes.append(f"[FAIL] {tag} — the 'Assign to or split among' dialog did not open")
            return False
        for person in people:
            pick = self._squash(person) + "select"
            prof = next((e for e in self._profiles(els) if name(e) == pick), None)
            if prof is None:
                notes.append(f"[FAIL] {tag} — no {person!r} on the dialog (profiles: "
                             f"{', '.join(self._pretty(name(e)[:-6]) for e in self._profiles(els))})")
                return False
            if not self._picked(els, prof):
                _idbd.tap_el(udid, prof, els)
                els, done = self._wait_els(udid, lambda es: self._picked(es, prof), 5.0)
                if not done:
                    notes.append(f"[FAIL] {tag} — tapped {person} but they did not get selected")
                    return False
        extra = [self._pretty(name(p)[:-6]) for p in self._profiles(els)
                 if self._picked(els, p) and name(p) not in
                 {self._squash(x) + "select" for x in people}]
        if extra:
            notes.append(f"[FAIL] {tag} — the dialog also has {', '.join(extra)} selected")
            return False
        btns = sorted((e for e in els if name(e) == "assignProductsBtn"),
                      key=lambda e: frame(e)[0])
        btn = btns[0] if mode == "assign" else btns[-1]
        ok, how = _idbd.tap_el(udid, btn, els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — could not tap {mode.title()} ({how})")
            return False
        want = before + len(people)

        # Landed = the sheet is gone and the dish is on the order for them: a new row
        # per profile, or -- for a profile that already has the same line -- that
        # row's price going up (the server merges it; MEASURED 2026-10-29, run
        # 4da619ce: a second 'Coca-Cola ×2 to Guest 1, Noolu Naga' gave no new row).
        # The order row shows no quantity, only `${name}card` and its price.
        #
        # The app's own commit signal is the sheet closing: on success it closes and
        # clears its list; a refusal ('Out Of The Quantity For: …') keeps it open with
        # a toast (Event/index.js:1124-1151). The rows are the proof only while they
        # are in the tree -- a long order renders just its top rows (MEASURED
        # 2026-10-29, run 138f7122: 24 Coca-Cola rows before, none rendered after).
        def closed(es):
            return self._toast(es) or not any(name(e) in self._ADD_ITEM_SHEET for e in es)

        els, shut = self._wait_els(udid, closed, 20.0)
        toast = self._toast(els)
        if not shut or toast:
            notes.append(f"[FAIL] {tag} — pressed {mode.title()} for {', '.join(people)} but "
                         + (f"the app said {toast!r}" if toast else "the sheet stayed open"))
            return False

        # Then the rows, as evidence: after the sheet closes the order redraws EMPTY
        # and refills from the server -- MEASURED 2026-10-29 on slow staging: 45 s
        # and more (runs 7e3b2e72, 93764e35; the rows were there afterwards).
        def landed(es):
            rows = sum(1 for e in self._order_rows(es) if name(e) == row_id)
            return rows >= want or (rows >= before and rows and
                                    self._dish_total(es, row_id) > euros0 + 0.005)

        els, done = self._wait_els(udid, landed, 60.0)
        got = sum(1 for e in self._order_rows(els) if name(e) == row_id)
        if done:
            lines = [f"+{self._dish_total(els, row_id) - euros0:.2f} € on the order"]
        elif got:
            lines = [f"its rows read {self._dish_total(els, row_id):.2f} € (was {euros0:.2f})"]
        else:
            lines = ["the sheet closed; the order had not redrawn its rows after 60 s"]
        self._w_lines = getattr(self, "_w_lines", []) + [
            {"product": product, "qty": qty, "options": opts, "mode": mode, "to": people}]
        notes.append(f"[ok] {tag} — added {qty} × {product}"
                     + (f" ({', '.join(opts)})" if opts else "")
                     + f", {'assigned to' if mode == 'assign' else 'split among'} "
                     + f"{', '.join(people)}: {before} → {got} row(s), "
                     + ", ".join(lines))
        return True

    # `@w_qty:<Profile>/<Product> =N | +N | -N | remove`
    _W_QTY_RE = re.compile(r"^\s*(?P<who>[^/]+?)\s*/\s*(?P<what>.+?)\s+(?P<op>[+=-]\d+|remove)\s*$")

    def _profile_region(self, els: List[dict], profile: str):
        """(card, y_top, y_bottom) of *profile*'s accordion on the payment panel: from
        its `${username}accordionCard` down to the next card (the cards read
        'G Guest1accordionCard \\uf10c', so the id is matched inside the label)."""
        want = self._squash(profile) + "accordionCard"
        cards = sorted(((e, m.group(1)) for e in els
                        for m in [self._CARD_ID_RE.search(_idbd.name(e))] if m),
                       key=lambda c: _idbd.frame(c[0])[1])
        for i, (e, cid) in enumerate(cards):
            if cid == want:
                top = _idbd.frame(e)[1]
                bottom = _idbd.frame(cards[i + 1][0])[1] if i + 1 < len(cards) else 1e9
                return e, top, bottom
        return None, 0.0, 0.0

    def _panel_into_view(self, udid: str, els: List[dict], e: dict, again):
        """Drag the payment panel (its left column, clear of the controls) until *e*
        is on screen, 120 pt from either edge. *again(els)* re-finds it."""
        frame = _idbd.frame
        _w, h = _idbd.app_size(els)
        for _ in range(4):
            x, y, w, eh = frame(e)
            if 120 <= y and y + eh <= h - 120:
                return els, e
            shift = (y + eh / 2) - h / 2
            y0 = h * 0.8 if shift > 0 else h * 0.2
            _idbd.swipe(udid, 900, y0, 900, y0 - max(-h * 0.55, min(h * 0.55, shift)),
                        max(1.0, min(abs(shift), h * 0.55) / 200.0))
            time.sleep(0.6)
            els = _idbd.describe_all(udid)
            e = again(els) or e
        return els, e

    def _w_qty(self, spec: str, notes: List[str]) -> bool:
        """`@w_qty:<Profile>/<Product> =N|+N|-N|remove` — set / change a profile's
        quantity of a dish, or remove it, before it is sent to the kitchen. `=N` is the
        form to use in a flow: a resumed segment re-runs its steps, and '=6' on a
        line already at 6 has nothing to do, where '+1' would add another.

        PaymentDetails.js:1258-1270: inside the profile's accordion (tap
        `${username}accordionCard` to open it), an unsent, unsplit, not pre-ordered
        line carries CustomCounter's `${name}Increase`, `${name}Decrease` (count ≥ 2)
        and `${name}Remove` (count < 2) (Components/CustomCounter/index.js:9-12),
        which post assaignProducts '+' / 'x'. The ids repeat in every open accordion,
        so only the ones between this profile's card and the next are used. Done when
        the count between them moved (remove: the line left the accordion)."""
        tag = f"@w_qty:{spec}"
        m = self._W_QTY_RE.match(spec or "")
        if not m:
            notes.append(f"[FAIL] {tag} — write it as <Profile>/<Product> =N | +N | -N | remove")
            return False
        who, what, op = m.group("who"), m.group("what"), m.group("op")
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, frame = _idbd.name, _idbd.frame
        pid = self._squash(what)

        # MEASURED 2026-10-29 (installed build): the line is the text '<Product> x <n>'
        # inside the card, and its stepper is the generic `decrementQuantity` (n ≥ 2)
        # / `removeItem` (n = 1), the count, `incrementQuantity` -- not the source's
        # `${name}Increase`, which is kept as a fallback.
        ids = {"Increase": ("incrementQuantity", pid + "Increase"),
               "Decrease": ("decrementQuantity", pid + "Decrease"),
               "Remove": ("removeItem", pid + "Remove")}
        line_re = re.compile(rf"^{re.escape(what)} x (\d+)$")

        def line(es):
            card, top, bottom = self._profile_region(es, who)
            if card is None:
                return None
            return next((e for e in es if line_re.match(name(e) or "")
                         and top < frame(e)[1] < bottom
                         and frame(card)[0] - 10 <= frame(e)[0] <= frame(card)[0] + frame(card)[2]),
                        None)

        def mine(es, kind):
            ln = line(es)
            if ln is None:
                return None
            cy = frame(ln)[1] + frame(ln)[3] / 2
            return next((e for e in es if name(e) in ids[kind] and frame(e)[0] > frame(ln)[0]
                         and abs(frame(e)[1] + frame(e)[3] / 2 - cy) < 16), None)

        def count(es) -> Optional[int]:
            ln = line(es)
            return int(line_re.match(name(ln)).group(1)) if ln is not None else None

        els = _idbd.describe_all(udid)
        card, _top, _bottom = self._profile_region(els, who)
        if card is None:
            notes.append(f"[FAIL] {tag} — no {who!r} card on the payment panel")
            return False
        if mine(els, "Increase") is None:              # its accordion is closed
            _idbd.tap_el(udid, card, els)
            els, _ = self._wait_els(udid, lambda es: mine(es, "Increase"), 6.0)
        if mine(els, "Increase") is None:
            if op == "remove" and getattr(self, "_resume", None) and line(els) is None:
                notes.append(f"[ok] {tag} — {what} is not in {who}'s card: removed by the "
                             f"run this one resumes")
                return True
            notes.append(f"[FAIL] {tag} — {who}'s card shows no quantity control for "
                         f"{what} (only unsent, unsplit, not pre-ordered lines have one)")
            return False
        n0 = count(els)
        if op.startswith("="):
            k = int(op[1:]) - (n0 or 0)
            steps = ["Increase"] * k if k > 0 else ["Decrease"] * -k
            if not steps:
                notes.append(f"[ok] {tag} — {who}'s {what} is already {n0}")
                return True
        else:
            steps = (["Increase"] * int(op[1:]) if op.startswith("+")
                     else ["Decrease"] * int(op[1:]) if op.startswith("-")
                     else ["Decrease"] * max(0, (n0 or 1) - 1) + ["Remove"])
        for suffix in steps:
            ctl = mine(els, suffix)
            if ctl is None:
                notes.append(f"[FAIL] {tag} — no {suffix.lower()} control on {who}'s "
                             f"{what} line (count {count(els)}; − shows only from 2, remove "
                             f"only at 1)")
                return False
            before = count(els)
            # The payment panel scrolls: drag it so the control is on screen -- MEASURED
            # 2026-10-29 (run 91d9d03a): Guest 3's '−' below the fold was "not
            # reachable" for tap_el's own scroll.
            els, ctl = self._panel_into_view(udid, els, ctl, lambda es: mine(es, suffix))
            ok, how = _idbd.tap_el(udid, ctl, els, scroll=False)
            if not ok:
                notes.append(f"[FAIL] {tag} — could not tap {who}'s {suffix.lower()} on "
                             f"{what} ({how})")
                return False
            if suffix == "Remove":
                els, gone = self._wait_els(udid, lambda es: line(es) is None, 25.0)
                if not gone:
                    notes.append(f"[FAIL] {tag} — tapped Remove but {what} is still in "
                                 f"{who}'s card")
                    return False
                break
            # 25s: on slow staging the count changed after more than 10s (MEASURED
            # 2026-10-29, run bfaa5994: 'stayed at 5', and it read 6 a minute later).
            els, moved = self._wait_els(udid, lambda es: count(es) not in (None, before), 25.0)
            if not moved:
                notes.append(f"[FAIL] {tag} — tapped {suffix} but {who}'s {what} stayed at "
                             f"{before}")
                return False
        notes.append(f"[ok] {tag} — {who}'s {what}: "
                     + (f"{n0} → removed" if op == "remove" else f"{n0} → {count(els)}"))
        return True

    _CARD_LINE_RE = re.compile(r"^(?P<what>.+?) x \d+(?:/\d+)?$")

    def _owner_lines(self, udid: str, profile: str, raw: bool = False) -> Optional[List[str]]:
        """The dishes (ids; *raw*: the line texts) on *profile*'s card: its lines read
        '<Product> x <n>' or '... x <n>/<m>' for a share (MEASURED 2026-10-29:
        'Coca-Cola® Light 1.0l x 5/5', 'Pizza Salamees x 6/2'). The card is opened when
        its lines do not show."""
        # Wait for the card: right after the order opens, the payment panel draws its
        # cards a moment later (MEASURED 2026-10-29, run a2635b0c: 'no Roopa D card').
        els, found = self._wait_els(udid, lambda es: self._profile_region(es, profile)[0], 20.0)
        if not found:
            # Something covers the payment panel (a leftover ADD sheet replaces it with
            # its Summary): clear it once and look again. MEASURED 2026-10-29 (run
            # 42a50c32): 'no Guest 5 card' for 86s while the card was there afterwards.
            if any(_idbd.name(e) in self._ADD_ITEM_SHEET for e in els):
                _idbd.tap(udid, ["addNewItemClose"], els)
            self._list_view(udid)
            els, _ = self._wait_els(udid, lambda es: self._profile_region(es, profile)[0], 15.0)
        for attempt in (1, 2):
            card, top, bottom = self._profile_region(els, profile)
            if card is None:
                return None
            # Inside the card's own column: below the LAST card the region runs to the
            # screen's end, and the order pane's '<Product> x <n>' texts are there too
            # (MEASURED 2026-10-29, run a1d0235d: Guest 5 read 'Pizza Salamees x 1').
            cx, _cy, cw, _ch = _idbd.frame(card)
            lines = [(_idbd.name(e) if raw else self._squash(m.group("what"))) for e in els
                     for m in [self._CARD_LINE_RE.match(_idbd.name(e) or "")]
                     if m and top < _idbd.frame(e)[1] < bottom
                     and cx - 10 <= _idbd.frame(e)[0] <= cx + cw]
            if lines or attempt == 2:
                return lines
            els, card = self._panel_into_view(
                udid, els, card, lambda es: self._profile_region(es, profile)[0])
            _idbd.tap_el(udid, card, els, scroll=False)
            time.sleep(1.5)
            els = _idbd.describe_all(udid)
        return []

    def _long_press_card(self, udid: str, profile: str) -> bool:
        from automation.scenarios.idb_coords import to_device
        els = _idbd.describe_all(udid)
        card, _t, _b = self._profile_region(els, profile)
        if card is None:
            return False
        els, card = self._panel_into_view(udid, els, card,
                                          lambda es: self._profile_region(es, profile)[0])
        x, y, w, h = _idbd.frame(card)
        # Left of centre: the card's right end is its radio.
        px, py = to_device(udid, x + w * 0.35, y + h / 2)
        _idbd._idb(["ui", "tap", "--udid", udid, "--duration", "1.2", str(int(px)), str(int(py))])
        return True

    def _focus_profile(self, udid: str, profile: str, tag: str,
                       notes: List[str]) -> Optional[List[dict]]:
        """Show only *profile*'s lines on the order: LONG-press their
        `${username}accordionCard` (Accordion/index.js:111 handlePerson). MEASURED
        2026-10-29: a long-press on Guest 1 left their 5 rows (of 34), a second one
        all 34 again -- it TOGGLES -- and the order redraws 6-8 s later (longer on slow
        staging). A plain tap only opens the card.

        Judged by CONTENT, not by "the rows changed": the order is focused when its
        rows are exactly the dishes on the person's card. MEASURED (run e786709b): a
        long-press while another person was focused turned the focus OFF, the 33 rows
        were taken for Guest 1's, and the step swiped someone else's line."""
        name = _idbd.name
        owner = self._owner_lines(udid, profile)
        if owner is None:
            notes.append(f"[FAIL] {tag} — no {profile!r} card on the payment panel")
            return None
        if not owner:
            notes.append(f"[FAIL] {tag} — {profile}'s card lists no lines")
            return None

        def focused(es):
            rows = [name(e)[:-4] for e in self._order_rows(es)]
            return bool(rows) and sorted(rows) == sorted(owner)

        # A long-press toggles, so it is pressed again only when the order was READ
        # after the last one and is not focused on them: when it could not be read
        # (idb timing out), it is waited for -- a second press would undo a focus that
        # worked (MEASURED 2026-10-29, run f28812d1).
        els, hit = self._wait_els(udid, focused, 2.0)
        presses, deadline = 0, time.time() + 75.0
        while not hit and presses < 3 and time.time() < deadline:
            if presses == 0 or self._order_rows(els):
                if self._long_press_card(udid, profile):
                    presses += 1
            els, hit = self._wait_els(udid, focused, 14.0)
        if not hit:
            notes.append(f"[FAIL] {tag} — long-pressed {profile}'s card but the order does "
                         f"not show just their {len(owner)} line(s) "
                         f"(it shows {len(self._order_rows(els))})")
            return None
        self._focused = profile
        notes.append(f"    · {tag}: showing only {profile}'s lines ({len(owner)})")
        return els

    def _unfocus_profile(self, udid: str, tag: str, notes: List[str]) -> None:
        """Back to everyone's lines: long-press the focused card again (it toggles);
        done when the order lists more than that person's lines."""
        who = getattr(self, "_focused", "")
        if not who:
            return
        owner = self._owner_lines(udid, who) or []
        for _ in range(2):
            self._long_press_card(udid, who)
            els, more = self._wait_els(udid, lambda es: len(self._order_rows(es)) > len(owner),
                                       14.0)
            if more:
                self._focused = ""
                return
        notes.append(f"    · {tag}: could not show everyone's lines again")

    _W_ON_RE = re.compile(r"^\s*(?P<what>[^@]+?)\s*@\s*(?P<who>[^>*=]+?)\s*"
                          r"(?:\*\s*(?P<n>\d+|all)|=\s*(?P<keep>\d+))?\s*(?:>\s*(?P<to>.+))?$")

    def _row_money(self, els: List[dict], row: dict) -> Optional[float]:
        p = self._row_price(els, row)
        m = self._MONEY_RE.match(_idbd.name(p) or "") if p is not None else None
        return float(m.group(1).replace(",", ".")) if m else None

    def _done_before(self, notes: List[str], tag: str) -> bool:
        """In a RESUMED run, a row action the app refuses as 'already …' (split, comp)
        was done by the run this one resumes: the segment re-runs its steps. Turns
        that refusal into a pass, noted as such. Never in a fresh run."""
        last = notes[-1] if notes else ""
        if getattr(self, "_resume", None) and "[FAIL]" in last and "already" in last.lower():
            notes[-1] = (f"[ok] {tag} — already done by the run this one resumes "
                         f"(the app said: {last.split('said', 1)[-1].strip()[:80]})")
            return True
        return False

    def _w_split(self, spec: str, notes: List[str]) -> bool:
        """`@w_split:<Product>@<Owner> > Name, Name` — swipe the owner's line of a dish
        left, SPLIT it with the people named.

        The owner's line is found by showing only their lines (_focus_profile). The
        SPLIT dialog (AssignSplitProductModal) hides the owner and is an Overlay --
        people are tapped by their shown name, each confirmed by `${name}close`
        joining its ids; Apply is `assignProductsBtn`. splitProducts shares the WHOLE
        line among the people + the owner (Event/index.js:1770). Done when the
        owner's row costs its share: price / (k + 1)."""
        tag = f"@w_split:{spec}"
        m = self._W_ON_RE.match(spec or "")
        people = [p.strip() for p in ((m.group("to") or "") if m else "").split(",") if p.strip()]
        if not m or not people:
            notes.append(f"[FAIL] {tag} — write it as <Product>@<Owner> > Name, Name")
            return False
        what, who = m.group("what"), m.group("who")
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name = _idbd.name
        # A resumed segment re-runs this step: the card shows a split line as
        # '<Product> x <n>/<m>' (MEASURED 2026-10-29: 'Pizza Salamees x 6/2'), and the
        # app refuses to split it again (run 40a23d54) without a toast to read.
        if getattr(self, "_resume", None):
            share = re.compile(rf"^{re.escape(what)} x \d+/\d+$")
            if any(share.match(t) for t in self._owner_lines(udid, who, raw=True) or []):
                notes.append(f"[ok] {tag} — {who}'s {what} is already split (by the run "
                             f"this one resumes)")
                return True
        self._list_view(udid)                  # rows swipe only in the list view
        els = self._focus_profile(udid, who, tag, notes)
        if els is None:
            return False
        row = next((e for e in self._order_rows(els) if name(e) == self._squash(what) + "card"),
                   None)
        if row is None:
            notes.append(f"[FAIL] {tag} — {who} has no {what} line")
            return False
        price0 = self._row_money(els, row)

        def dialog_ids(es) -> List[str]:
            ov = self._overlay(es, "assignProductsBtn")
            ids = name(ov).split() if ov is not None else []
            return ids if any(t.endswith("select") and t not in self._NOT_PROFILES
                              for t in ids) else []

        els = self._row_action(udid, row, "SPLIT", dialog_ids, tag, notes)
        if els is None:
            done = self._done_before(notes, tag)
            self._unfocus_profile(udid, tag, notes)
            return done
        box = self._box(els, "split")
        for person in people:
            pick = self._squash(person)
            if f"{pick}select" not in dialog_ids(els):
                notes.append(f"[FAIL] {tag} — {person} is not offered (ids: "
                             f"{' '.join(t for t in dialog_ids(els) if t.endswith('select'))})")
                return False
            ok, how = self._ocr_tap(udid, box, person)
            els, sel = self._wait_els(udid, lambda es: f"{pick}close" in dialog_ids(es), 5.0)
            if not ok or not sel:
                notes.append(f"[FAIL] {tag} — {how}; {person} did not get selected")
                return False
        els = self._overlay_taps(udid, els, "split", ["Apply"], "assignProductsBtn", tag, notes)
        if els is None:
            return False
        want = round(price0 / (len(people) + 1), 2) if price0 else None

        def shared(es):
            r = self._same_row(es, row) or next(
                (e for e in self._order_rows(es) if name(e) == name(row)), None)
            p = self._row_money(es, r) if r is not None else None
            return p if p is not None and want is not None and abs(p - want) <= 0.02 else None

        els, now = self._wait_els(udid, shared, 15.0)
        if now is None:
            r = next((e for e in self._order_rows(els) if name(e) == name(row)), None)
            notes.append(f"[FAIL] {tag} — applied the split but {who}'s {what} costs "
                         f"{self._row_money(els, r) if r is not None else '?'} €, expected "
                         f"{want} ({price0} / {len(people) + 1})")
            return False
        self._unfocus_profile(udid, tag, notes)
        notes.append(f"[ok] {tag} — split {who}'s {what} with {', '.join(people)}: "
                     f"{price0:.2f} € → {now:.2f} € each")
        return True

    def _card_qty(self, udid: str, who: str, what: str) -> Tuple[int, str]:
        """(units, '<line>; <line>') of *what* on *who*'s card -- a share 'x 6/2' counts
        its 6 -- for before/after proofs."""
        lines = [t for t in self._owner_lines(udid, who, raw=True) or []
                 if self._CARD_LINE_RE.match(t)
                 and self._squash(self._CARD_LINE_RE.match(t).group("what")) == self._squash(what)]
        units = sum(int(re.search(r" x (\d+)", t).group(1)) for t in lines)
        return units, "; ".join(lines)

    _UNITS_COUNT_RE = re.compile(r"(?:removeItem|decrementQuantity) (\d+) incrementQuantity")

    def _units_set(self, udid: str, marker: str, want: Optional[int], tag: str,
                   notes: List[str]) -> Optional[int]:
        """On the 'Select which items to …' Overlay (CompAndVoidProductModal), raise
        the count to *want* units (None = all of them) and return it.

        MEASURED 2026-10-29: its ids read 'Select which items to Void Penne Pollo
        removeItem 1 incrementQuantity ApplyBtn' -- the COUNT is in there -- and the
        counter's glyphs OCR as one token '(× 1+'; its '+' is that token's right end.
        The count starts at 1 and stops at the line's quantity (3 for a 3-unit line
        split two ways)."""
        from automation.scenarios import screen_text
        name = _idbd.name

        def now(es) -> Optional[int]:
            ov = self._overlay(es, marker)
            m = self._UNITS_COUNT_RE.search(name(ov)) if ov is not None else None
            return int(m.group(1)) if m else None

        els = _idbd.describe_all(udid)
        n = now(els)
        if n is None:
            notes.append(f"[FAIL] {tag} — the 'which items' dialog shows no count")
            return None
        for _ in range(12):
            if want is not None and n >= want:
                break
            plus = next((t for t in screen_text.read_text(udid, region=self._box(els, "units"),
                                                          accurate=True) if "+" in t[0]), None)
            if plus is None:
                notes.append(f"[FAIL] {tag} — no '+' on the 'which items' dialog (count {n})")
                return None
            _idbd.tap_point(udid, plus[1] + plus[3] - 9, plus[2] + plus[4] / 2)
            els, _ = self._wait_els(udid, lambda es: (now(es) or 0) > n, 3.0)
            if (now(els) or 0) <= n:
                if want is None:
                    break                      # at the line's quantity: all of it
                notes.append(f"[FAIL] {tag} — the count stops at {n}, {want} asked")
                return None
            n = now(els)
        return n

    def _w_void(self, spec: str, notes: List[str], reason: str = "entryError") -> bool:
        """`@w_void:<Product>@<Owner>*<N|all>` — VOID N units (all = the whole line) of
        the owner's line of a dish; `=K` voids down to K units left, the form for a flow
        (a resumed segment re-runs it, and '=5' on a line already at 5 does nothing).
        Swipe it, VOID, a reason, Apply, the units (_units_set), Next (`ApplyBtn`). VOID is refused once the kitchen prepared the
        item (splitCall, Event/index.js:1709). Done when the owner's line left their
        lines, or (part of it) its price fell."""
        tag = f"@w_void:{spec}"
        m = self._W_ON_RE.match(spec or "")
        if not m or m.group("to"):
            notes.append(f"[FAIL] {tag} — write it as <Product>@<Owner>*<N|all> or =<K>")
            return False
        what, who = m.group("what"), m.group("who")
        want = None if (m.group("n") or "all") == "all" else int(m.group("n"))
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name = _idbd.name
        rid = self._squash(what) + "card"
        card0 = self._card_qty(udid, who, what)
        if m.group("keep") is not None:
            want = card0[0] - int(m.group("keep"))
            if want <= 0:
                notes.append(f"[ok] {tag} — {who}'s {what} is already at {card0[0]} "
                             f"({card0[1] or 'no line'})")
                return True
        self._list_view(udid)                  # rows swipe only in the list view
        els = self._focus_profile(udid, who, tag, notes)
        if els is None:
            return False
        row = next((e for e in self._order_rows(els) if name(e) == rid), None)
        if row is None:
            self._unfocus_profile(udid, tag, notes)
            if getattr(self, "_resume", None) and want is None:
                notes.append(f"[ok] {tag} — {who} has no {what} line: voided by the run "
                             f"this one resumes")
                return True
            notes.append(f"[FAIL] {tag} — {who} has no {what} line")
            return False
        els = self._row_action(udid, row, "VOID", lambda es: self._overlay(es, reason),
                               tag, notes)
        if els is None:
            return False
        els = self._overlay_taps(udid, els, "void", [self._REASON_TEXT[reason], "Apply"],
                                 reason, tag, notes)
        if els is None:
            return False
        units = 1
        els, ov = self._wait_els(udid, lambda es: self._overlay(es, "ApplyBtn"), 3.0)
        if ov is not None:                     # quantity > 1: which units
            units = self._units_set(udid, "ApplyBtn", want, tag, notes)
            if units is None:
                return False
            els = self._overlay_taps(udid, _idbd.describe_all(udid), "units", ["Next"],
                                     "ApplyBtn", tag, notes)
            if els is None:
                return False

        # The proof is the owner's card: its line 'x 6/2' became 'x 5/2', or left.
        # (The focused order list redraws -- a row missing for a moment is no proof.)
        self._unfocus_profile(udid, tag, notes)
        after = card0
        for _ in range(5):
            after = self._card_qty(udid, who, what)
            if after[0] < card0[0]:
                break
            time.sleep(2.5)
        if after[0] >= card0[0]:
            toast = self._toast(_idbd.describe_all(udid)) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — the void did not take: {who}'s card still reads "
                         f"{card0[1] or 'nothing'} for {what}" + (f" ({toast!r})" if toast else ""))
            return False
        notes.append(f"[ok] {tag} — voided {units} unit(s) of {who}'s {what} "
                     f"({self._pretty(reason).lower()}): {card0[1]} → "
                     f"{after[1] or 'the line left their card'}")
        return True

    _GRID_ICON, _LIST_ICON = "", ""     # the order's two view toggles, no ids

    def _list_view(self, udid: str) -> bool:
        """The order in its LIST view, where rows swipe (VOID / COMP / SPLIT). The grid
        view (ASSIGN / SPLIT, `assignOrSplitBtn`) does not swipe -- MEASURED 2026-10-29:
        three swipes on a grid row showed nothing. Toggle by the list icon."""
        name = _idbd.name
        for _ in range(3):
            els = _idbd.describe_all(udid)
            if not self._has(els, "assignOrSplitBtn"):
                return True
            icon = next((e for e in els if name(e) == self._LIST_ICON), None)
            if icon is None:
                return False
            _idbd.tap_el(udid, icon, els, scroll=False)
            self._wait_els(udid, lambda es: not self._has(es, "assignOrSplitBtn"), 4.0)
        return not self._has(_idbd.describe_all(udid), "assignOrSplitBtn")

    def _ocr_tap_name(self, udid: str, box, text: str) -> Tuple[bool, str]:
        """Tap *text* on an Overlay by OCR, also when Vision merged it with its
        neighbour (MEASURED 2026-10-29: 'Guest 2 Guest 3' as one word): the part's
        centre is placed by its share of the word's characters."""
        from automation.scenarios import screen_text
        words = screen_text.read_text(udid, region=box, accurate=True)
        for t, x, y, w, h in words:
            if self._text_matches(t, text):
                return _idbd.tap_point(udid, x + w / 2, y + h / 2)[0], f"{text!r} (OCR)"
        for t, x, y, w, h in words:
            at = t.find(text)
            if at >= 0 and len(t) > len(text):
                cx = x + w * (at + len(text) / 2) / len(t)
                return _idbd.tap_point(udid, cx, y + h / 2)[0], f"{text!r} inside {t!r} (OCR)"
        return False, f"{text!r} is not on the dialog (it reads: {[t for t, *_ in words][:12]})"

    def _w_grid(self, spec: str, mode: str, notes: List[str]) -> bool:
        """`@w_move:<Product>@<Owner> > Name` / `@w_gsplit:<Product>@<Owner> > Name, …`
        — after SEND, move the owner's line to another person (Assign) or share it
        with others (Split), on the order's grid view.

        Event screen (OrderSummary.js:578-623, 1522; Modal AssignSplitProductModal in
        move mode): show only the owner's lines (long-press, _focus_profile), the grid
        toggle (2nd icon, no id: '\\uec56'), tick the line, `assignOrSplitBtn` ->
        'Assign to or split among…' -- an Overlay listing EVERY profile, the owner too
        (MEASURED 2026-10-29). Assign wants exactly one other person (assignToUser
        'moveProduct'); Split at least two, the owner among them (multipleSplitProducts).
        Refused after Notify Payment. Done when the owner's card no longer lists the
        dish (move) or lists it as a share 'x n/m' (split), and the others' cards
        list it."""
        tag = f"@w_{'move' if mode == 'assign' else 'gsplit'}:{spec}"
        m = self._W_ON_RE.match(spec or "")
        people = [p.strip() for p in ((m.group("to") or "") if m else "").split(",") if p.strip()]
        if not m or not people or (mode == "assign" and len(people) != 1):
            notes.append(f"[FAIL] {tag} — write it as <Product>@<Owner> > Name"
                         + (", Name" if mode == "split" else " (one person)"))
            return False
        what, who = m.group("what"), m.group("who")
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name = _idbd.name
        pid = self._squash(what)
        on_card = lambda lines: [t for t in lines if self._CARD_LINE_RE.match(t)  # noqa: E731
                                 and self._squash(self._CARD_LINE_RE.match(t).group("what")) == pid]
        mine0 = on_card(self._owner_lines(udid, who, raw=True) or [])
        if getattr(self, "_resume", None):
            # A resumed segment re-runs this step; the cards say whether it was done.
            if mode == "assign" and not mine0 and on_card(
                    self._owner_lines(udid, people[0], raw=True) or []):
                notes.append(f"[ok] {tag} — {what} is already {people[0]}'s (moved by the "
                             f"run this one resumes)")
                return True
            if mode == "split" and any(re.search(r" x \d+/\d+$", t) for t in mine0):
                notes.append(f"[ok] {tag} — {who}'s {what} is already shared ({mine0}; by "
                             f"the run this one resumes)")
                return True
        if not mine0:
            notes.append(f"[FAIL] {tag} — {who}'s card lists no {what}")
            return False
        els = self._focus_profile(udid, who, tag, notes)
        if els is None:
            return False
        if not self._has(els, "assignOrSplitBtn"):
            grid = next((e for e in els if name(e) == self._GRID_ICON), None)
            if grid is None:
                notes.append(f"[FAIL] {tag} — no grid toggle on the order")
                return False
            _idbd.tap_el(udid, grid, els, scroll=False)
            els, _ = self._wait_els(udid, lambda es: self._has(es, "assignOrSplitBtn"), 6.0)
        row = next((e for e in self._order_rows(els) if name(e) == pid + "card"), None)
        if row is None:
            notes.append(f"[FAIL] {tag} — the grid shows no {what} line of {who}")
            return False
        els, row = self._row_into_view(udid, els, row)
        if row is None:
            notes.append(f"[FAIL] {tag} — could not bring {what} onto the screen")
            return False
        # The header reads Unselect while any line is ticked: clear it first, so the
        # tap below TICKS this line (MEASURED 2026-10-29: a leftover tick made the tap
        # un-tick it, and ASSIGN / SPLIT then opened nothing).
        if self._has(els, "unSelectAll"):
            _idbd.tap(udid, ["unSelectAll"], els)
            els, _ = self._wait_els(udid, lambda es: not self._has(es, "unSelectAll"), 4.0)
            row = self._same_row(els, row) or row
        _idbd.tap_el(udid, row, els, scroll=False)
        els, ticked = self._wait_els(udid, lambda es: self._has(es, "unSelectAll"), 4.0)
        if not ticked:
            notes.append(f"[FAIL] {tag} — tapped {who}'s {what} line but it did not get ticked")
            return False
        ok, how = _idbd.tap(udid, ["assignOrSplitBtn"])
        els, ov = self._wait_els(udid, lambda es: self._overlay(es, "assignProductsBtn"), 8.0)
        if ov is None:
            toast = self._toast(els) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — ASSIGN / SPLIT did not open its dialog"
                         + (f" ({toast!r})" if toast else f" ({how})"))
            return False
        time.sleep(0.8)
        box = self._box(els, "split")
        picks = people if mode == "assign" else [who] + [p for p in people if p != who]
        for person in picks:
            sel = self._squash(person)
            ok, how = self._ocr_tap_name(udid, box, person)
            els, done = self._wait_els(udid, lambda es: f"{sel}close" in name(
                self._overlay(es, "assignProductsBtn") or {}).split(), 5.0)
            if not ok or not done:
                notes.append(f"[FAIL] {tag} — {how}; {person} did not get selected")
                return False
        els = self._overlay_taps(udid, els, "split", ["Assign" if mode == "assign" else "Split"],
                                 "assignProductsBtn", tag, notes)
        if els is None:
            return False
        # Back to the list view and everyone's lines, then read the cards. Settle
        # first: toggled while the dialog was still closing, the view stayed on the
        # grid (MEASURED 2026-10-29) and the next Select All found nothing.
        time.sleep(1.5)
        if not self._list_view(udid):
            notes.append(f"    · {tag}: could not switch the order back to its list view")
        self._unfocus_profile(udid, tag, notes)

        def landed():
            now = on_card(self._owner_lines(udid, who, raw=True) or [])
            if mode == "assign":
                gone = len(now) < len(mine0)
                there = bool(on_card(self._owner_lines(udid, people[0], raw=True) or []))
                return gone and there, now
            shared = any(re.search(r" x \d+/\d+$", t) for t in now)
            return shared, now

        for _ in range(4):
            ok, now = landed()
            if ok:
                break
            time.sleep(3.0)
        if not ok:
            notes.append(f"[FAIL] {tag} — pressed {'Assign' if mode == 'assign' else 'Split'} "
                         f"but {who}'s card still reads {now or 'nothing'} for {what}")
            return False
        notes.append(f"[ok] {tag} — "
                     + (f"moved {who}'s {what} to {people[0]}" if mode == "assign" else
                        f"split {who}'s {what} with {', '.join(people)}: {who}'s card "
                        f"reads {now}"))
        return True

    _COMP_PCTS = (5, 10, 15, 25, 30, 40, 100)       # the dialog's chips (Modal:5885)

    def _w_comp(self, spec: str, notes: List[str], reason: str = "managerDiscretion") -> bool:
        """`@w_comp:<Product>@<Owner>*<N|all>:<pct>` — COMP N units (all = the whole
        line) of the owner's line by pct % (a chip: 5 10 15 25 30 40 100).

        As _comp_item -- swipe, COMP, a reason and a discount chip (both required, none
        preselected), Apply, all on an Overlay tapped by text -- then, for a line of
        quantity > 1, the 'which items' dialog (_units_set; Next is
        `assignProductsBtn`). COMP is refused before Serve and after Notify Payment
        (splitCall, Event/index.js:1750). Done when the owner's line shows its
        'COMP n%' mark (OrderSummary.js:844)."""
        tag = f"@w_comp:{spec}"
        head, _, pct_s = (spec or "").rpartition(":")
        m = self._W_ON_RE.match(head)
        if not m or m.group("to") or not pct_s.strip().isdigit() \
                or int(pct_s) not in self._COMP_PCTS:
            notes.append(f"[FAIL] {tag} — write it as <Product>@<Owner>*<N|all>:<pct> "
                         f"(pct one of {', '.join(map(str, self._COMP_PCTS))})")
            return False
        what, who, pct = m.group("what"), m.group("who"), int(pct_s)
        want = None if (m.group("n") or "all") == "all" else int(m.group("n"))
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, mark = _idbd.name, f"{pct}%"
        rid = self._squash(what) + "card"
        self._list_view(udid)                  # rows swipe only in the list view
        els = self._focus_profile(udid, who, tag, notes)
        if els is None:
            return False
        row = next((e for e in self._order_rows(els) if name(e) == rid), None)
        if row is None:
            notes.append(f"[FAIL] {tag} — {who} has no {what} line")
            self._unfocus_profile(udid, tag, notes)
            return False
        marks = sum(1 for e in els if name(e) == mark)
        price0 = self._row_money(els, row)
        # A resumed segment re-runs this step: the row already carrying its 'COMP n%'
        # mark was comped by the run this one resumes.
        if getattr(self, "_resume", None) and any(
                name(e) == mark for e in self._row_line(els, row)):
            self._unfocus_profile(udid, tag, notes)
            notes.append(f"[ok] {tag} — {who}'s {what} already shows 'COMP {mark}' (by the run "
                         f"this one resumes)")
            return True
        got = None
        for attempt in (1, 2):
            got = self._row_action(udid, row, "COMP", lambda es: self._overlay(es, "5Btn"),
                                   tag, notes)
            if got is not None:
                break
            # Serve posts asynchronously (see _comp_item), and right after SERVE the
            # dialog can fail to open with no message (MEASURED 2026-10-29, run
            # 040c74be; the same comp opened a minute later): retry once.
            if attempt == 1 and "already" not in (notes[-1] if notes else "").lower():
                notes[-1] = notes[-1].replace("[FAIL]", "[retry]")
                time.sleep(4.0)
                els = _idbd.describe_all(udid)
                row = self._same_row(els, row) or row
                continue
            done = self._done_before(notes, tag)
            self._unfocus_profile(udid, tag, notes)
            return done
        reason = self._comp_reason(got, reason) or reason
        els = self._comp_pick(udid, got, reason, pct, tag, notes)
        if els is None:
            return False
        def units_up(es):
            # The comp dialog's Apply is assignProductsBtn too: the units dialog is the
            # one whose ids carry a count.
            ov = self._overlay(es, "assignProductsBtn")
            return ov if ov is not None and self._UNITS_COUNT_RE.search(name(ov)) else None

        units = 1
        els, ov = self._wait_els(udid, units_up, 3.0)
        if ov is not None:                     # quantity > 1: which units
            units = self._units_set(udid, "assignProductsBtn", want, tag, notes)
            if units is None:
                return False
            els = self._overlay_taps(udid, _idbd.describe_all(udid), "units", ["Next"],
                                     "assignProductsBtn", tag, notes)
            if els is None:
                return False
        els, done = self._wait_els(udid, lambda es: sum(1 for e in es if name(e) == mark) > marks,
                                   12.0)
        if not done:
            toast = self._toast(els) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — pressed Apply but {who}'s {what} shows no "
                         f"'COMP {mark}'" + (f" ({toast!r})" if toast else ""))
            return False
        r2 = self._same_row(els, row)
        price1 = self._row_money(els, r2) if r2 is not None else None
        self._unfocus_profile(udid, tag, notes)
        notes.append(f"[ok] {tag} — comped {units} unit(s) of {who}'s {what} at {mark} "
                     f"({self._pretty(reason).lower()})"
                     + (f"; {price0:.2f} € → {price1:.2f} €" if price0 and price1 is not None
                        else ""))
        return True

    def _split_item(self, notes: List[str]) -> bool:
        """Swipe a row of the dish just assigned to everyone -> SPLIT -> one more
        profile -> Apply. AssignSplitProductModal hides the row's owner
        (Components/Modal/index.js:2276); it is an Overlay, so the profile and Apply
        are tapped by their text and the pick is confirmed by '<Name>close' joining
        its ids. The owner's line becomes '1/2' and the other half is a new line:
        one row more."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, tag = _idbd.name, "@split_item"
        els, rows = self._wait_els(udid, self._order_rows, 8.0)
        if not rows:
            notes.append(f"[FAIL] {tag} — the order has no item rows to split")
            return False
        target = getattr(self, "_assigned_all", "")
        pool = [x for x in rows if name(x) == target] or rows
        row = pool[0]
        dish = name(row)
        before = sum(1 for x in rows if name(x) == dish)
        def dialog_ids(es) -> List[str]:
            ov = self._overlay(es, "assignProductsBtn")
            ids = name(ov).split() if ov is not None else []
            return ids if any(t.endswith("select") and t not in self._NOT_PROFILES
                              for t in ids) else []

        els = self._row_action(udid, row, "SPLIT", dialog_ids, tag, notes)
        if els is None:
            return False
        ids = dialog_ids(els)
        people = [t[:-len("select")] for t in ids
                  if t.endswith("select") and t not in self._NOT_PROFILES]
        pick = next((p for p in people if f"{p}close" not in ids), None)
        if pick is None:
            notes.append(f"[FAIL] {tag} — the split dialog lists no one to share with "
                         f"(ids: {' '.join(ids)[:200]})")
            return False
        box = self._box(els, "split")
        ok, how = self._ocr_tap(udid, box, self._pretty(pick))
        if not ok:
            notes.append(f"[FAIL] {tag} — {how}")
            return False
        els, sel = self._wait_els(udid, lambda es: f"{pick}close" in dialog_ids(es), 5.0)
        if not sel:
            notes.append(f"[FAIL] {tag} — tapped {how} but {self._pretty(pick)} did not get "
                         f"selected")
            return False
        picked = [self._pretty(pick)]
        els = self._overlay_taps(udid, els, "split", ["Apply"], "assignProductsBtn", tag,
                                 notes)
        if els is None:
            return False
        els, done = self._wait_els(
            udid, lambda es: sum(1 for x in self._order_rows(es) if name(x) == dish) > before,
            12.0)
        got = sum(1 for x in self._order_rows(els) if name(x) == dish)
        if not done:
            toast = self._toast(els)
            notes.append(f"[FAIL] {tag} — applied the split but {self._pretty(dish[:-4])} "
                         f"still has {got} row(s) (was {before})"
                         + (f" ({toast!r})" if toast else ""))
            return False
        notes.append(f"[ok] {tag} — split one {self._pretty(dish[:-4])} with "
                     f"{', '.join(picked)}: {before} → {got} rows (two halves)")
        return True

    def _comp_item(self, notes: List[str], reason: str = "birthday", pct: int = 10) -> bool:
        """Swipe a served row -> COMP -> a reason -> a discount -> Apply.

        The app refuses COMP before the item is served and after Notify Payment
        (splitCall, Screens/Event/index.js:1750), and both a reason and a discount
        are required -- none is preselected for a new comp; the reason radio and the
        discount chip both TOGGLE, so each is tapped exactly once. The dialog is an
        Overlay (see _OVERLAY_BOX): its controls are tapped by their text ('10 %').
        Done when the row shows its maroon 'COMP 10%' mark (OrderSummary.js:844)."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, tag, mark = _idbd.name, "@comp_item", f"{pct}%"
        els, rows = self._wait_els(udid, self._order_rows, 8.0)
        if not rows:
            notes.append(f"[FAIL] {tag} — the order has no item rows to comp")
            return False
        marks = sum(1 for e in els if name(e) == mark)
        row = rows[0]
        dish = self._pretty(name(row)[:-4])
        price0 = self._row_price(els, row)
        got = None
        for attempt in (1, 2):
            got = self._row_action(udid, row, "COMP", lambda es: self._overlay(es, "5Btn"),
                                   tag, notes)
            if got is not None:
                break
            # Serve posts asynchronously: 'Can't apply comp before serve...' right
            # after SERVE can just mean the row has not re-rendered yet.
            if attempt == 1 and "before serve" in (notes[-1] if notes else "").lower():
                notes[-1] = notes[-1].replace("[FAIL]", "[retry]")
                time.sleep(4.0)
                els = _idbd.describe_all(udid)
                row = self._same_row(els, row) or row
                continue
            return False
        reason = self._comp_reason(got, reason) or reason
        els = self._comp_pick(udid, got, reason, pct, tag, notes)
        if els is None:
            return False
        # Quantity > 1 asks which units; that dialog's 'Next' is assignProductsBtn.
        els = self._units_dialog(udid, els, "assignProductsBtn", tag, notes)
        if els is None:
            return False
        els, done = self._wait_els(
            udid, lambda es: sum(1 for e in es if name(e) == mark) > marks, 10.0)
        if not done:
            toast = self._toast(els)
            notes.append(f"[FAIL] {tag} — pressed Apply but {dish} shows no 'COMP {mark}'"
                         + (f" ({toast!r})" if toast else ""))
            return False
        row = self._same_row(els, row) or row
        price1 = self._row_price(els, row)
        change = (f"; {name(price0)} → {name(price1)}"
                  if price0 is not None and price1 is not None else "")
        notes.append(f"[ok] {tag} — comped {dish}: {self._pretty(reason).lower()}, "
                     f"{mark} off{change}")
        return True

    def _notify_payment(self, notes: List[str]) -> bool:
        """NOTIFY PAYMENT -> 'Yes' on 'Are you sure to call « Notify Payment » ?'
        (Components/Modal/NotifyPaymentConfirmModal.js; Yes has no id, only its
        text). NOTIFY PAYMENT renders once every item is served."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, tag = _idbd.name, "@notify_payment"
        is_yes = lambda es: next((e for e in es if name(e) == "Yes"), None)  # noqa: E731
        # Notified = the payment tiles show in the cards (PaymentDetails.js:2049: only
        # once payment was requested) and the button reads REMIND PAYMENT. MEASURED
        # 2026-10-29: the payment was notified while the 'Yes' was never seen.
        tiles = ("epaymentBtn", "paidEpaymentBtn", "payForBtn", "tipBtn")
        notified = lambda es: any(name(e) in tiles for e in es)  # noqa: E731
        if notified(_idbd.describe_all(udid)):
            notes.append(f"[ok] {tag} — payment is already notified (the payment tiles show; "
                         f"the button reads REMIND PAYMENT)")
            return True
        for attempt in (1, 2):
            els, btn = self._wait_els(
                udid, lambda es: next((e for e in es if name(e) == "notifyPaymentBtn"), None),
                10.0 if attempt == 1 else 3.0)
            if btn is None:
                notes.append(f"[FAIL] {tag} — NOTIFY PAYMENT is not on screen (it shows "
                             f"once every item has been served)")
                return False
            ok, how = _idbd.tap_el(udid, btn, els)
            if not ok:
                notes.append(f"[FAIL] {tag} — could not tap NOTIFY PAYMENT ({how})")
                return False
            els, yes = self._wait_els(udid, is_yes, 8.0)
            if yes is not None:
                break
            # The dialog can be up with its buttons hidden from accessibility (the
            # Overlay trap, see _OVERLAY_BOX): then 'Yes' is only on the pixels.
            w, h = _idbd.app_size(els)
            ok, how = self._ocr_tap(udid, (0, 0, w, h), "Yes")
            if ok:
                time.sleep(2.0)
                if self._ocr_sees(udid, "Yes"):
                    notes.append(f"[FAIL] {tag} — tapped Yes ({how}) but the confirmation "
                                 f"stayed open")
                    return False
                notes.append(f"[ok] {tag} — tapped NOTIFY PAYMENT, then Yes ({how}, by its "
                             f"text); payment notified")
                return True
        else:
            els, done = self._wait_els(udid, notified, 8.0)
            if done:
                notes.append(f"[ok] {tag} — tapped NOTIFY PAYMENT; payment notified (the "
                             f"payment tiles show)")
                return True
            toast = self._toast(els) or self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — tapped NOTIFY PAYMENT twice but the 'Yes' "
                         f"confirmation never opened" + (f" ({toast!r})" if toast else ""))
            return False
        ok, how = _idbd.tap_el(udid, yes, els)
        els, gone = self._wait_els(udid, lambda es: is_yes(es) is None, 8.0)
        if not ok or not gone:
            notes.append(f"[FAIL] {tag} — the confirmation stayed open after Yes ({how})")
            return False
        notes.append(f"[ok] {tag} — tapped NOTIFY PAYMENT, then Yes; payment notified")
        return True

    def _waiter_close_table(self, r: ScenarioRunner, notes: List[str]) -> bool:
        """After the diner paid in the C-App: open THIS booking and close its table.

        This segment follows a consumer one, so the iPad sits on the bookings board,
        not on the order -- the old bare 'click closeTableBtn' had nothing to press.
        A booking paid in full can be completed by the app itself (measured on
        4954: COMPLETED, no Close Table), and then there is nothing to close."""
        tag = "@waiter_close_table"
        settled = self._already_settled()
        if "completed" in settled.lower():
            notes.append(f"[ok] {tag} — nothing to close: {settled} (the app closed the "
                         f"table itself once the bill was paid)")
            return True
        if "closeTableBtn" not in {_idbd.name(e) for e in _idbd.describe_all(
                getattr(self, "_cur_udid", "") or self._business_udid())}:
            if not self._open_reservation(r, notes, statuses=("payment", "serve", "inprogress"),
                                          what=tag):
                settled = self._already_settled()
                if "completed" in settled.lower():
                    notes.append(f"[ok] {tag} — nothing to close: {settled}")
                    return True
                return False
        return self._close_table(notes)

    def _close_table(self, notes: List[str], wait: float = 15.0) -> bool:
        """Close Table once the bill is settled, and confirm the order closed.

        closeTableBtn renders only when payment_completed (OrderSummary.js:1358),
        which the server confirms a few seconds AFTER Confirm Payment -- MEASURED on
        booking 4949: absent right after the E-Payment, there ~10s later. So wait
        for it, tap it, and confirm the screen went back to My Bookings."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        tag = "@close_table"
        find = lambda es: next((e for e in es if _idbd.name(e) == "closeTableBtn"), None)  # noqa: E731
        els, btn = self._wait_els(
            udid, lambda es: find(es) or (True if self._settled_screen(es) else None), wait)
        settled = self._settled_screen(els)
        if settled:
            # Pay for all settles the whole bill: the app shows the receipt and
            # completes the booking itself -- there is no Close Table to press
            # (measured: 4954 went to COMPLETED with no closeTableBtn).
            notes.append(f"[ok] {tag} — the bill is fully paid and the app closed the "
                         f"order itself ({'receipt shown' if settled == 'receipt' else 'back on My Bookings'})")
            return True
        if btn is True:
            btn = None
        # Close Table missing: CHECK THE PAYMENT before giving up (the user's rule).
        # MEASURED on booking 4954: the amount was entered (Due 0.00 €) but the
        # Confirm tap did not register, so Close Table never came. And Confirm only
        # used to be looked for on screen -- inside a FOLDED card it is not, so the
        # payment was never retried. Now: open the payer's card if it folded, and if
        # it still offers a payment (a method or Confirm Payment), pay by E-Payment
        # again and wait for Close Table again. Twice at most. If the card offers
        # nothing to pay, the payment did go through: just wait longer for Close
        # Table rather than paying twice.
        #
        # EXCEPT after a payment the app ACCEPTED (Confirm cleared the amount, see
        # _pay_business) that still did not settle. MEASURED 2026-10-19: when one
        # profile pays for others, staging IGNORES an amount equal to the bill -- the
        # 'businessSidePay' event the C-App logs (logs/metro_8084.log) shows
        # paidAmount 0 for all 13 such payments since 10-01, while every amount ABOVE
        # the bill settled at once. It looks like an unrounded float comparison:
        # 24.52+26.26+17.51+17.51 = 85.80000000000001 > 85.80 sent. Paying the same
        # amount again is ignored again, so: wait once more, then report it.
        accepted = getattr(self, "_payment_accepted", None)
        if btn is None and not settled and accepted:
            # At least ~45s after Confirm in all: a settle that went through once took
            # longer than 15s (4954, 2026-10-01).
            more = max(wait, 30.0)
            els, btn = self._wait_els(
                udid, lambda es: find(es) or (True if self._settled_screen(es) else None),
                more)
            settled = self._settled_screen(els)
            if btn is True:
                btn = None
            if btn is None and not settled:
                others = getattr(self, "_paid_for_others", None)
                notes.append(
                    f"[FAIL] {tag} — the app accepted the payment of {accepted:.2f} € "
                    f"(Confirm Payment cleared it), but the booking did not settle: Close "
                    f"Table never appeared in {wait + more:.0f}s."
                    + (f" One profile paid for {len(others)} others: staging ignores a "
                       f"payment equal to such a bill (server event: paidAmount 0; it "
                       f"settles only above the bill — likely an unrounded float sum, "
                       f"e.g. 85.80000000000001 > 85.80). Server bug, not the test; paying "
                       f"the same amount again would be ignored again." if others else
                       " Not paying the same amount again."))
                return False
        for attempt in (1, 2):
            if btn is not None or settled:
                break
            action, why = self._payment_pending(udid, els)
            if action == "pay":
                notes.append(f"    · {tag}: Close Table is not visible and the payment is "
                             f"not done ({why}) — paying by E-Payment again"
                             + (f" (attempt {attempt}/2)" if attempt > 1 else ""))
                self._pay_business("epay", notes)
            else:
                notes.append(f"    · {tag}: Close Table is not visible yet, but nothing is "
                             f"left to pay — waiting for it")
            els, btn = self._wait_els(
                udid, lambda es: find(es) or (True if self._settled_screen(es) else None),
                wait)
            settled = self._settled_screen(els)
            if btn is True:
                btn = None
        if settled:
            notes.append(f"[ok] {tag} — the bill is fully paid and the app closed the "
                         f"order itself ({'receipt shown' if settled == 'receipt' else 'back on My Bookings'})")
            return True
        if btn is None:
            toast = self._toast(els)
            notes.append(f"[FAIL] {tag} — Close Table did not appear within {wait:.0f}s "
                         f"(is the whole bill paid?)" + (f" ({toast!r})" if toast else ""))
            return False
        ok, how = _idbd.tap_el(udid, btn, els)
        els, gone = self._wait_els(
            udid, lambda es: not self._has(es, "closeTableBtn")
            and not self._order_rows(es), 15.0)
        if not ok or not gone:
            notes.append(f"[FAIL] {tag} — tapped Close Table ({how}) but the order is still "
                         f"open")
            return False
        notes.append(f"[ok] {tag} — tapped Close Table; the order closed")
        return True

    def _card_amount(self, els: List[dict], word: str) -> Optional[float]:
        """The amount on the payer card's '<word>' line ('Due', 'Outstanding'):
        either one text ('Due 0.00 €') or a label with its money on the same row."""
        name, frame = _idbd.name, _idbd.frame
        one = re.compile(rf"^\s*{word}\b[^0-9]*(\d+(?:[.,]\d{{1,2}})?)\s*€", re.I)
        for e in els:
            m = one.match(name(e) or "")
            if m:
                return float(m.group(1).replace(",", "."))
        for t in (e for e in els if (name(e) or "").strip().lower().rstrip(":") == word.lower()):
            ty = frame(t)[1] + frame(t)[3] / 2
            for e in els:
                m = self._MONEY_RE.match(name(e) or "")
                if m and abs(frame(e)[1] + frame(e)[3] / 2 - ty) < 12 and frame(e)[0] > frame(t)[0]:
                    return float(m.group(1).replace(",", "."))
        return None

    def _payment_pending(self, udid: str, els: List[dict]) -> Tuple[str, str]:
        """(what to do, why): ('confirm' | 'pay' | '', reason).

        STRICT, because re-paying a bill that did go through would charge it twice.
        (The '91.00 against a 90.56 bill' once read as such a double charge was a
        single 91.00 overpay -- the server event log shows the exact 90.56 payments
        before it recorded nothing; see _close_table.) The method buttons are NOT evidence:
        on 4954 the card still showed E-Payment/Cash/Voucher for seconds after a
        payment that had registered. So:
          * Confirm Payment still up -> the payment did not complete: 'pay' (the
            user's rule: do the E-Payment again, not Confirm alone);
          * Outstanding (else Due) above zero -> genuinely unpaid: 'pay';
          * anything else, unreadable included -> '' (wait, never pay on a guess).
        The card may have FOLDED, hiding all of it, so it is opened -- only when
        none of it shows, since tapping an open card folds it."""
        def decide(es) -> Tuple[str, str]:
            names = {_idbd.name(e) for e in es}
            due = self._card_amount(es, "Due")
            out = self._card_amount(es, "Outstanding")
            if "paymentConfirmBtn" in names:
                # The user's rule (2026-10-06): when Close Table is not visible, do
                # the E-Payment again -- not just Confirm. Pressing Confirm alone left
                # the run looping on "pressing Confirm Payment again" with no Close Table.
                return "pay", "Confirm Payment is still up"
            owed = out if out is not None else due
            if owed is not None and owed >= 0.005:
                return "pay", f"{owed:.2f} € still outstanding"
            return "", ""

        def visible(es) -> bool:
            names = {_idbd.name(e) for e in es}
            return ("paymentConfirmBtn" in names or self._card_amount(es, "Due") is not None
                    or self._card_amount(es, "Outstanding") is not None)

        if visible(els):
            return decide(els)
        cards = [e for e in els if "accordionCard" in _idbd.name(e)]
        want = getattr(self, "_payer_card", "")
        card = next((e for e in cards if want and want in _idbd.name(e).split()),
                    cards[0] if cards else None)
        if card is None:
            return "", ""
        _idbd.tap_el(udid, card, els)
        es, _ = self._wait_els(udid, lambda x: visible(x) or None, 4.0)
        return decide(es)

    _CARD_ID_RE = re.compile(r"(\S+accordionCard)\b")

    def _pay_for_all(self, notes: List[str], payer_name: str = "",
                     people: Optional[List[str]] = None) -> bool:
        """Open the first profile's card -> Pay For -> select every profile -> Apply.
        Payment itself (E-Payment -> total -> Confirm) is the next step, @pay:epay.

        Pay For is live only for the CURRENT payer -- disabled={... person._id ==
        currPayingUserId ? false : true} (Screens/Event/PaymentDetails.js:2572) -- and
        opening the card does not make its owner the payer; pressing one of the
        card's payment methods does (handleCurrPayingUser, Screens/Event/index.js:1474).
        So when Pay For is disabled: E-Payment, close the keypad, then Pay For.
        The dialog preselects the payer; Apply is `applyPayment` (Modal/index.js:5610)."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name, tag = _idbd.name, "@pay_for_all"
        find = lambda es, n: next((e for e in es if name(e) == n), None)  # noqa: E731

        # A card reads 'R RoopaDaccordionCard \uf10c' -- avatar initial, the id,
        # an icon glyph (measured on booking 4947) -- so match the id inside it.
        if payer_name:
            return self._pay_for_named(payer_name, people or [], notes)
        # `@w_pay_for:<Payer> > Name, Name`: THAT payer's card, only those people.
        want_card = self._squash(payer_name) + "accordionCard" if payer_name else ""
        if payer_name:
            tag = f"@w_pay_for:{payer_name} > {', '.join(people or [])}"

        def card(es):
            return next((e for e in es for m in [self._CARD_ID_RE.search(name(e))]
                         if m and (not want_card or m.group(1) == want_card)), None)

        els, c = self._wait_els(udid, card, 8.0)
        if c is None:
            notes.append(f"[FAIL] {tag} — no profile cards in the payment panel")
            return False
        self._payer_card = self._CARD_ID_RE.search(name(c)).group(1)
        payer = self._pretty(self._payer_card[:-len("accordionCard")])
        # Everyone on the order: one card per profile ('G Guest1accordionCard').
        owners = list(dict.fromkeys(
            self._pretty(m.group(1)[:-len("accordionCard")])
            for e in els for m in [self._CARD_ID_RE.search(name(e))] if m))
        els, pf = self._wait_els(udid, lambda es: find(es, "payForBtn"), 2.0)
        if pf is None:
            _idbd.tap_el(udid, c, els)                   # expand the card
            els, pf = self._wait_els(udid, lambda es: find(es, "payForBtn"), 8.0)
        if pf is None:
            notes.append(f"[FAIL] {tag} — opened {payer}'s card but it has no Pay For "
                         f"(is payment notified?)")
            return False

        def make_payer() -> bool:
            ok, _ = _idbd.tap(udid, ["epaymentBtn"])
            if not ok:
                return False
            es, pad = self._wait_els(udid, lambda x: find(x, "numberPadClose"), 6.0)
            if pad is not None:
                _idbd.tap_el(udid, pad, es, scroll=False)
                self._wait_els(udid, lambda x: find(x, "numberPadClose") is None, 5.0)
            notes.append(f"    · {tag}: Pay For did not open the dialog — tapped "
                         f"{payer}'s E-Payment to make them the payer, closed the keypad")
            return True

        def show_pay_for(es):
            """(els, payForBtn), re-opening the card if it folded. Only taps the card
            when Pay For is NOT shown -- tapping it while Pay For shows folds it."""
            es, btn = self._wait_els(udid, lambda x: find(x, "payForBtn"), 3.0)
            if btn is None:
                c2 = card(es)
                if c2 is not None:
                    _idbd.tap_el(udid, c2, es)
                    es, btn = self._wait_els(udid, lambda x: find(x, "payForBtn"), 5.0)
            return es, btn

        # The user's order: open the card -> Pay For -> select every profile -> Apply.
        # E-Payment belongs to the NEXT step (@pay:epay), after the Pay For.
        #
        # Pressing E-Payment FIRST (to make the profile the payer) broke this step:
        # closing its keypad folds the card and Pay For disappears ("3 tries; last:
        # no Pay For button on screen", 93s). It stays only as a FALLBACK for a build
        # where Pay For is dead until the profile is the payer (measured on 10-01).
        #   1. tap Pay For straight away
        #   2. not opened: E-Payment, close the keypad, re-open the card, Pay For
        #   3. still not: re-read the settled panel and tap Pay For once more
        last_how = "no Pay For button on screen"
        ap = None
        for attempt in (1, 2, 3):
            if attempt == 2:
                make_payer()
            if attempt > 1 or pf is None:
                els, pf = show_pay_for(els)
            if pf is None:
                last_how = f"{payer}'s card did not show Pay For"
            else:
                tapped, how = _idbd.tap_el(udid, pf, els)
                last_how = how if tapped else f"Pay For not tapped: {how}"
            els, ap = self._wait_els(udid, lambda es: find(es, "applyPayment"), 6.0)
            if ap is not None:
                if attempt > 1:
                    notes.append(f"    · {tag}: the Pay For dialog opened on try {attempt}")
                break
        if ap is None:
            notes.append(f"[FAIL] {tag} — tapped Pay For on {payer}'s card but the "
                         f"'For whom do you like to pay' dialog did not open "
                         f"(3 tries; last: {last_how})")
            return False
        if people:
            for person in people:
                sel = self._squash(person) + "select"
                prof = next((e for e in self._profiles(els) if name(e) == sel), None)
                if prof is None:
                    notes.append(f"[FAIL] {tag} — {person} is not offered (profiles: "
                                 f"{', '.join(self._pretty(name(e)[:-6]) for e in self._profiles(els))})")
                    return False
                if not self._picked(els, prof):
                    _idbd.tap_el(udid, prof, els)
                    els, ok = self._wait_els(udid, lambda es: self._picked(es, prof), 5.0)
                    if not ok:
                        notes.append(f"[FAIL] {tag} — tapped {person} but they did not get "
                                     f"selected")
                        return False
            ok, how = _idbd.tap(udid, ["applyPayment"], els)
            els, gone = self._wait_els(udid, lambda es: find(es, "applyPayment") is None, 10.0)
            if not ok or not gone:
                notes.append(f"[FAIL] {tag} — Apply did not close the Pay For dialog ({how})")
                return False
            self._paid_for_others = list(people)
            notes.append(f"[ok] {tag} — {payer} pays for {', '.join(people)}")
            return True
        els, picked = self._select_profiles(udid, els, "all", tag, notes)
        if picked is None:
            return False
        # "Pay for ALL": everyone on the order's payment panel must be covered.
        # MEASURED 2026-10-19 (run 518bffc6): only Guest 1 got selected, Roopa then
        # paid the whole 85.80 € for herself + Guest 1, staging settled THAT, and the
        # flow passed without testing pay-for-all at all.
        got = {p.replace(" ", "") for p in picked}
        missing = [o for o in owners if o.replace(" ", "") not in got]
        if missing:
            shown = {_idbd.name(e) for e in self._profiles(els)}
            off = [e for e in els if _idbd.name(e).endswith("select")
                   and " " not in _idbd.name(e) and _idbd.name(e) not in self._NOT_PROFILES
                   and _idbd.name(e) not in shown]
            notes.append(f"[FAIL] {tag} — the Pay For dialog let {payer} pay only for "
                         f"{', '.join(p for p in picked if p.replace(' ', '') != payer.replace(' ', '')) or 'nobody'}; "
                         f"{', '.join(missing)} could not be selected"
                         + (f" ({len(off)} more avatar(s) off-screen)" if off else
                            " (not listed in the dialog)"))
            return False
        ok, how = _idbd.tap(udid, ["applyPayment"], els)
        els, gone = self._wait_els(udid, lambda es: find(es, "applyPayment") is None, 10.0)
        if not ok or not gone:
            toast = self._toast(els)
            notes.append(f"[FAIL] {tag} — Apply did not close the Pay For dialog ({how})"
                         + (f" ({toast!r})" if toast else ""))
            return False
        others = [p for p in picked if p.replace(" ", "") != payer.replace(" ", "")]
        self._paid_for_others = others                  # see _close_table's report
        notes.append(f"[ok] {tag} — {payer} pays for {', '.join(others) or 'nobody else'}")
        return True

    def _open_card(self, udid: str, who: str, inside: str):
        """(els, card, control): *who*'s payment card opened until *inside* (an id in
        it: tipBtn, discountBtn, ...) shows within it; control None when it does not."""
        name, frame = _idbd.name, _idbd.frame

        def ctl(es):
            card, top, bottom = self._profile_region(es, who)
            if card is None:
                return None
            return next((e for e in es if name(e) == inside and top < frame(e)[1] < bottom), None)

        els = _idbd.describe_all(udid)
        card, _t, _b = self._profile_region(els, who)
        if card is not None and ctl(els) is None:
            els, card = self._panel_into_view(udid, els, card,
                                              lambda es: self._profile_region(es, who)[0])
            _idbd.tap_el(udid, card, els, scroll=False)
            els, _ = self._wait_els(udid, ctl, 6.0)
        c = ctl(els)
        if c is not None:
            els, c = self._panel_into_view(udid, els, c, ctl)
        return els, self._profile_region(els, who)[0], c

    def _pay_for_named(self, payer: str, people: List[str], notes: List[str]) -> bool:
        """`@w_pay_for:<Payer> > Name, Name` — on the payer's OWN card: Pay For
        (`payForBtn`), pick those people, Apply. Every card carries a payForBtn
        (PaymentDetails.js:2573), so the button is taken from the payer's card --
        MEASURED 2026-10-29: the first one on screen was another person's. Pay For is
        live only for the current payer: when it does not open, the card's E-Payment
        makes them the payer (keypad closed).

        The dialog ('For whom do you like to pay… ?') is an Overlay on the installed
        build: ONE element with every id ('NooluNagaselect NooluNagaclose
        Guest3select … applyPayment'), payer preselected. People are tapped by their
        shown name, confirmed by '<Name>close' joining the ids; Apply by its text."""
        tag = f"@w_pay_for:{payer} > {', '.join(people)}"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name = _idbd.name
        self._payer_card = self._squash(payer) + "accordionCard"

        def ids(es) -> List[str]:
            ov = self._overlay(es, "applyPayment")
            if ov is not None:
                return name(ov).split()
            return [name(e) for e in es] if self._has(es, "applyPayment") else []

        els = _idbd.describe_all(udid)
        for attempt in (1, 2):
            if ids(els):
                break
            els, card, btn = self._open_card(udid, payer, "payForBtn")
            if btn is None:
                notes.append(f"[FAIL] {tag} — {payer}'s card shows no Pay For")
                return False
            _idbd.tap_el(udid, btn, els, scroll=False)
            els, up = self._wait_els(udid, ids, 6.0)
            if up or attempt == 2:
                break
            _e, _c, epay = self._open_card(udid, payer, "epaymentBtn")
            if epay is None:
                _e, _c, epay = self._open_card(udid, payer, "paidEpaymentBtn")
            if epay is not None:
                _idbd.tap_el(udid, epay, _e, scroll=False)
                es, _ = self._wait_els(udid, lambda x: self._has(x, "numberPadClose"), 5.0)
                _idbd.tap(udid, ["numberPadClose"], es)
                time.sleep(1.0)
                notes.append(f"    · {tag}: made {payer} the payer (E-Payment, keypad closed)")
                els = _idbd.describe_all(udid)
        if not ids(els):
            notes.append(f"[FAIL] {tag} — Pay For on {payer}'s card did not open its dialog")
            return False
        time.sleep(0.8)
        box = self._box(els, "split")
        for person in people:
            sel = self._squash(person)
            if f"{sel}close" in ids(els):
                continue
            if f"{sel}select" not in ids(els):
                offered = [t[:-6] for t in ids(els) if t.endswith("select")]
                notes.append(f"[FAIL] {tag} — {person} is not offered ({', '.join(offered)})")
                return False
            el = next((e for e in els if name(e) == f"{sel}select"), None)
            ok, how = (_idbd.tap_el(udid, el, els) if el is not None
                       else self._ocr_tap_name(udid, box, person))
            els, done = self._wait_els(udid, lambda es: f"{sel}close" in ids(es), 5.0)
            if not done:
                notes.append(f"[FAIL] {tag} — {how}; {person} did not get selected")
                return False
        if self._has(els, "applyPayment"):
            _idbd.tap(udid, ["applyPayment"], els)
        else:
            self._ocr_tap(udid, box, "Apply")
        els, gone = self._wait_els(udid, lambda es: not ids(es), 10.0)
        if not gone:
            notes.append(f"[FAIL] {tag} — Apply did not close the Pay For dialog")
            return False
        self._paid_for_others = list(people)
        els, _ = self._wait_els(udid, lambda es: self._profile_region(es, payer)[0], 10.0)
        total = self._payer_card_total(els, self._profile_region(els, payer)[0])
        notes.append(f"[ok] {tag} — {payer} pays for {', '.join(people)}"
                     + (f"; their card's Total reads {total:.2f} €" if total is not None else ""))
        return True

    def _w_payer(self, who: str, notes: List[str]) -> bool:
        """`@w_payer:<Name>` — the next @pay:<method> pays on THIS person's card, opened
        (and brought on screen) so its methods show: a guest's card far down the panel
        stayed closed and @pay found no methods (MEASURED 2026-10-29, Guest 5)."""
        self._payer_card = self._squash(who) + "accordionCard"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        btn = None
        for inside in ("paidCashPaymentBtn", "cashPaymentBtn"):
            _els, _card, btn = self._open_card(udid, who, inside)
            if btn is not None:
                break
        notes.append(f"[ok] @w_payer:{who} — the next payment is {who}'s"
                     + ("" if btn is not None else " (their card shows no payment method)"))
        return True

    def _w_tip(self, spec: str, notes: List[str]) -> bool:
        """`@w_tip:<Name> <euros>` — a tip on that person's card: `tipBtn` (shown while
        they have none, PaymentDetails.js) opens the keypad -- digit keys
        `VirtualKeyboard-0..9`, the amount in `userAmountInput`, `userInputBtn`. A
        whole-euro amount: the decimal key has no id. Done when the card's Total rose
        by it."""
        tag = f"@w_tip:{spec}"
        who, _, amount = (spec or "").rpartition(" ")
        if not who or not amount.isdigit():
            notes.append(f"[FAIL] {tag} — write it as <Name> <whole euros>")
            return False
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        els, card, btn = self._open_card(udid, who, "tipBtn")
        if btn is None:
            notes.append(f"[FAIL] {tag} — {who}'s card shows no Tip (tipBtn: it shows only "
                         f"once payment is notified, while no tip is set)")
            return False
        total0 = self._payer_card_total(els, card)
        _idbd.tap_el(udid, btn, els, scroll=False)
        els, pad = self._wait_els(udid, lambda es: self._has(es, "userInputBtn"), 6.0)
        if not pad:
            notes.append(f"[FAIL] {tag} — Tip did not open the keypad")
            return False
        # The keys are named by their digit on the installed build ('1' … '9', '0',
        # 140x62 in the keypad, MEASURED 2026-10-29), `VirtualKeyboard-N` in the source.
        pad_top = next((_idbd.frame(e)[1] for e in els if _idbd.name(e) == "userAmountInput"), 0)
        for d in amount:
            key = next((e for e in els if _idbd.name(e) in (d, f"VirtualKeyboard-{d}")
                        and _idbd.frame(e)[1] > pad_top and _idbd.frame(e)[2] >= 60), None)
            if key is None:
                notes.append(f"[FAIL] {tag} — the keypad has no key {d!r}")
                return False
            _idbd.tap_el(udid, key, els, scroll=False)
            time.sleep(0.3)
        _idbd.tap(udid, ["userInputBtn"])
        want = (total0 or 0) + int(amount)

        def risen(es):
            t = self._payer_card_total(es, self._profile_region(es, who)[0])
            return t if t is not None and abs(t - want) < 0.015 else None

        els, now = self._wait_els(udid, risen, 10.0)
        if now is None:
            got = self._payer_card_total(els, self._profile_region(els, who)[0])
            notes.append(f"[FAIL] {tag} — tipped {amount} € but {who}'s Total reads {got} "
                         f"(was {total0})")
            return False
        notes.append(f"[ok] {tag} — {who}'s Total {total0:.2f} € + tip {amount} € = {now:.2f} €")
        return True

    def _w_discount(self, who: str, notes: List[str]) -> bool:
        """`@w_discount:<Name>` — the restaurant's coupon on that person's payment:
        `discountBtn` opens ApplyDiscountCoupon (Screens/Event/ApplyDiscountCoupon.js):
        this restaurant's business coupons valid today, tiles '{n}% OFFER' / '{n}€
        OFFER' with no ids -- the first is tapped by its text; applying closes the
        sheet. Staging 2026-10-29 has one: 'Coupon Festival', 5 % above 10 €. Done
        when the card's Total fell."""
        tag = f"@w_discount:{who}"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        els, card, btn = self._open_card(udid, who, "discountBtn")
        if btn is None:
            # MEASURED 2026-10-29: the installed B-App build has NO Discount on the payment
            # card (E-Payment, Cash, Food Voucher only) although the source has
            # `discountBtn`. An app/build gap, reported -- not a test fault and not faked.
            methods = self._open_card(udid, who, "epaymentBtn")[2] is not None
            if methods:
                notes.append(f"[skip] {tag} — this business-app build has no Discount on "
                             f"{who}'s payment card (only E-Payment / Cash / Food Voucher; the "
                             f"source has discountBtn). REPORTED: the discount cannot be tested "
                             f"on this build")
                return True
            notes.append(f"[FAIL] {tag} — {who}'s card shows no Discount (after payment is "
                         f"notified; locked once they paid)")
            return False
        total0 = self._payer_card_total(els, card)
        _idbd.tap_el(udid, btn, els, scroll=False)
        time.sleep(2.0)
        from automation.scenarios import screen_text
        w, h = _idbd.app_size(els)
        offers = [t for t in screen_text.read_text(udid, accurate=True) if "OFFER" in t[0].upper()]
        if not offers:
            notes.append(f"[FAIL] {tag} — the discount sheet lists no '% OFFER' coupon "
                         f"(none valid today for the business app?)")
            return False
        t = offers[0]
        _idbd.tap_point(udid, t[1] + t[3] / 2, t[2] + t[4] / 2)

        def fell(es):
            v = self._payer_card_total(es, self._profile_region(es, who)[0])
            return v if v is not None and total0 is not None and v < total0 - 0.005 else None

        els, now = self._wait_els(udid, fell, 12.0)
        if now is None:
            notes.append(f"[FAIL] {tag} — tapped {t[0]!r} but {who}'s Total did not fall "
                         f"(was {total0})")
            return False
        notes.append(f"[ok] {tag} — applied {t[0]!r}: {who}'s Total {total0:.2f} € → "
                     f"{now:.2f} € (−{(1 - now / total0) * 100:.1f} %)")
        return True

    def _send_if_any(self, r, notes: List[str]) -> bool:
        """`@send_if_any` — SEND what is not sent yet; nothing unsent passes. A line
        moved or split after SEND came back UNSENT in Phase 4 (SEND reappeared) and
        stayed sent in Phase 7 (MEASURED 2026-10-29, same booking), so a fixed
        'Select All + SEND' cannot be written for it."""
        tag = "@send_if_any"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        self._list_view(udid)
        _idbd.tap(udid, ["selectAll"])
        els, any_ = self._wait_els(udid, lambda es: self._has(es, "sendItemsBtn")
                                   or self._has(es, "unSelectAll"), 5.0)
        if self._has(els, "sendItemsBtn"):
            return FlowRunner._handle_special(self, r, "@send_to_kitchen", notes)
        if self._has(els, "unSelectAll"):
            _idbd.tap(udid, ["unSelectAll"], els)          # prepared lines: not ours
        notes.append(f"[ok] {tag} — nothing unsent: every line is with the kitchen already")
        return True

    # ── Invoices, review ─────────────────────────────────────────────────────
    _INVOICE_WORDS = ("VAT 3", "VAT 14", "VAT 17", "Total")
    _TOTAL_RE = re.compile(r"^total\b(?!\s*vat)", re.I)

    def _read_invoice(self, udid: str, box=None) -> Dict[str, str]:
        """What an open invoice shows, by OCR (its rows carry no ids): the VAT rows and
        the Total line, each with the text level with it. The invoice is longer than
        the screen (MEASURED 2026-10-29: the Pre-Order lines filled it, VAT and Total
        below), so it is read, dragged up, read again -- up to eight screens.

        'Total' is the GRAND total: the invoice prints a Total under its Pre-Order block
        and under its card payment too, then the overall one after Tip, then the VAT
        table, which has no Total (InvoiceModal, Components/Modal/index.js:3087/3109/
        3465). So the last Total above the first VAT row is kept."""
        from automation.scenarios import screen_text
        out: Dict[str, str] = {}
        vat_seen, last = False, None

        def row_of(hit) -> str:
            row = sorted((t for t in words if abs((t[2] + t[4] / 2) - (hit[2] + hit[4] / 2))
                          < 10 and t[1] > hit[1]), key=lambda t: t[1])
            return " ".join([hit[0]] + [t[0] for t in row])[:80]

        for page in range(8):
            words = screen_text.read_text(udid, region=box, accurate=True)
            if page == 0:
                out["_head"] = " ".join(t[0] for t in words[:6])[:80]
            vat_y = None
            for key in self._INVOICE_WORDS:
                if key == "Total" or key in out:
                    continue
                hit = next((t for t in words if t[0].upper().replace(" ", "").startswith(
                    key.upper().replace(" ", ""))), None)
                if hit is not None:
                    out[key] = row_of(hit)
                    vat_y = hit[2] if vat_y is None else min(vat_y, hit[2])
            if not vat_seen:
                totals = sorted((t for t in words if self._TOTAL_RE.match(t[0].strip())
                                 and (vat_y is None or t[2] < vat_y)), key=lambda t: t[2])
                if totals:
                    out["Total"] = row_of(totals[-1])
                vat_seen = vat_y is not None
            seen = [t[0] for t in words]
            if all(k in out for k in self._INVOICE_WORDS) or box is None or seen == last:
                break
            last = seen
            x0, y0, x1, y1 = box
            _idbd.swipe(udid, (x0 + x1) / 2, y0 + (y1 - y0) * 0.8, (x0 + x1) / 2,
                        y0 + (y1 - y0) * 0.25, 1.2)
            time.sleep(0.8)
        out.pop("_head", None) if "Total" in out else None
        return out

    def _close_invoice(self, udid: str) -> None:
        name = _idbd.name
        els = _idbd.describe_all(udid)
        btn = next((e for e in els if name(e).lower() in ("back", "close", "invoiceclose",
                                                          "closeinvoice", "printclose")), None)
        if btn is not None:
            _idbd.tap_el(udid, btn, els, scroll=False)
        else:
            w, h = _idbd.app_size(els)
            _idbd.swipe(udid, w / 2, h * 0.15, w / 2, h * 0.9, 0.3)
        time.sleep(1.0)

    def _w_invoice(self, notes: List[str]) -> bool:
        """`@w_invoice` — the waiter's EVENT invoice: grid view, its print icon (the
        grid's `addItemsBtn`), `eventInvoice`, Print Now (`assignProductsBtn`) opens
        InvoiceModal (Modal/index.js:2752; no ids, read by OCR). Needs the payment
        completed. Done when the invoice shows its VAT rows and its Total."""
        tag = "@w_invoice"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        name = _idbd.name
        els = _idbd.describe_all(udid)
        if not self._has(els, "assignOrSplitBtn"):
            grid = next((e for e in els if name(e) == self._GRID_ICON), None)
            if grid is not None:
                _idbd.tap_el(udid, grid, els, scroll=False)
                els, _ = self._wait_els(udid, lambda es: self._has(es, "assignOrSplitBtn"), 6.0)
        ok, how = _idbd.tap(udid, ["addItemsBtn"], els)      # the PRINT icon in grid view
        els, up = self._wait_els(udid, lambda es: self._has(es, "eventInvoice")
                                 or self._overlay(es, "eventInvoice"), 8.0)
        if not up:
            notes.append(f"[FAIL] {tag} — the print icon did not open the invoice choice "
                         f"({how})")
            return False
        if self._has(els, "eventInvoice"):
            _idbd.tap(udid, ["eventInvoice"], els)
        else:
            self._ocr_tap(udid, self._box(els, "units"), "Event Invoice")
        time.sleep(1.0)
        btns = [e for e in _idbd.describe_all(udid) if name(e) == "assignProductsBtn"]
        if btns:
            _idbd.tap_el(udid, btns[-1], None, scroll=False)
        else:
            w, h = _idbd.app_size(els)
            self._ocr_tap(udid, (0, h * 0.4, w, h), "Print Now")
        time.sleep(3.0)
        toast = self._toast(_idbd.describe_all(udid)) or self._screen_toast(udid)
        # The invoice sheet: centred, ~455 pt wide (MEASURED 2026-10-29 on the iPad).
        w, h = _idbd.app_size(_idbd.describe_all(udid))
        box = (w / 2 - 240, 40, w / 2 + 240, h - 30)
        seen = self._read_invoice(udid, box)
        if "Total" not in seen:
            notes.append(f"[FAIL] {tag} — the event invoice did not open or shows no Total"
                         + (f" ({toast!r})" if toast else "") + f"; read: {seen}")
            return False
        self._w_invoice_seen = seen
        # Closed by a tap beside it (its backdrop): it has no close control in the tree.
        _idbd.tap_point(udid, 150, h * 0.5)
        time.sleep(1.2)
        self._list_view(udid)
        notes.append(f"[ok] {tag} — event invoice: " + "; ".join(seen.values()))
        return True

    def _c_finished_card(self, udid: str, notes: List[str], tag: str) -> Optional[List[dict]]:
        """The Wallet's Finished tab with THIS run's event opened (View PDF, Rebook).

        MEASURED 2026-10-29: a Finished event is a `${restaurant}Card` header with its
        time ('13:55') below -- no `…finishedCard` id -- and a tap on its row opens it:
        `finishedCardclose`, `finishedRebook`, `finishedViewPdf` (CardData.js:2370)
        appear. The event is found by this run's booked time, scrolling the list."""
        name, frame = _idbd.name, _idbd.frame
        slot = getattr(self, "_booked_slot", "") or ""
        els = _idbd.describe_all(udid)
        if not self._has(els, "finishedBlock"):
            _idbd.tap(udid, ["walletTab", "Wallet"], els)
            els, _ = self._wait_els(udid, lambda es: self._has(es, "finishedBlock"), 10.0)
        _idbd.tap(udid, ["finishedBlock"])
        els, _ = self._wait_els(udid, lambda es: self._has(es, "walletFinishedSearchInput"), 8.0)
        w, h = _idbd.app_size(els)
        for _ in range(4):                     # from the TOP of the list
            _idbd.swipe(udid, w / 2, h * 0.3, w / 2, h * 0.85, 0.2)
            time.sleep(0.4)
        times = re.compile(r"^\d{1,2}:\d{2}$")

        def mine(es):
            return next((e for e in es if slot and name(e) == slot
                         and 0 < frame(e)[1] < h - 80), None)

        def opened(es):
            """View PDF / Rebook INSIDE this event's card: below its time, above the
            next event's (another event may be the open one)."""
            me = mine(es)
            if me is None:
                return False
            y0 = frame(me)[1]
            below = [frame(e)[1] for e in es if times.match(name(e) or "") and frame(e)[1] > y0]
            y1 = min(below) if below else 1e9
            return any(name(e) in ("finishedViewPdf", "finishedRebook")
                       and y0 < frame(e)[1] < y1 for e in es)

        when = None
        for _ in range(10):
            els = _idbd.describe_all(udid)
            when = mine(els)
            if when is not None:
                break
            _idbd.swipe(udid, w / 2, h * 0.75, w / 2, h * 0.35, 1.2)
            time.sleep(0.8)
        if when is None:
            notes.append(f"[FAIL] {tag} — the Finished tab lists no event at {slot or '?'}")
            return None
        if opened(els):
            return els                         # already open
        x, y, _w, hh = frame(when)
        for _ in range(2):                     # the row TOGGLES open / closed
            _idbd.tap_point(udid, x + 150, y + hh / 2)
            els, up = self._wait_els(udid, opened, 6.0)
            if up:
                break
        return els

    def _c_rating_prompt(self, udid: str, notes: List[str], tag: str) -> Optional[List[dict]]:
        """THIS run's event's rating prompt: Wallet → Upcoming → the event (unfolded by
        its time) → `${host}orderCardCompleted` ('Completed', UpcomingCard.js:4924) →
        'How was your experience …?' with `star1..star5` and `skipReviewCard`.
        MEASURED 2026-10-29: the paid event stays under Upcoming until it is rated or
        skipped; the Finished tab has no rating for it yet."""
        name = _idbd.name
        els = _idbd.describe_all(udid)
        if self._has(els, "star1"):
            return els
        if not self._has(els, "upcomingBlock"):
            _idbd.tap(udid, ["walletTab", "Wallet"], els)
            self._wait_els(udid, lambda es: self._has(es, "upcomingBlock"), 10.0)
        _idbd.tap(udid, ["upcomingBlock"])
        time.sleep(1.5)
        slot = getattr(self, "_booked_slot", "") or ""
        els = self._unfold_event(udid, slot, _norm(CONSUMER_NAME)) if slot else _idbd.describe_all(udid)
        btn = next((e for e in els if name(e).endswith("orderCardCompleted")), None)
        if btn is None:
            notes.append(f"[FAIL] {tag} — the {slot} event shows no 'Completed' (is it paid "
                         f"in full? already rated or skipped?)")
            return None
        _idbd.tap_el(udid, btn, els)
        els, up = self._wait_els(udid, lambda es: self._has(es, "star1"), 8.0)
        if not up:
            notes.append(f"[FAIL] {tag} — 'Completed' did not open the rating prompt")
            return None
        return els

    def _c_rate(self, stars: str, notes: List[str]) -> bool:
        """`@c_rate:<1-5>` — rate the restaurant: the prompt's star N opens the Review
        screen (Wallet/Review.js), a line of feedback is typed into its text box, and
        `submitReview` sends it. Done when the Review screen closed."""
        tag = f"@c_rate:{stars}"
        if stars not in ("1", "2", "3", "4", "5"):
            notes.append(f"[FAIL] {tag} — stars must be 1-5")
            return False
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        els = self._c_rating_prompt(udid, notes, tag)
        if els is None:
            return False
        _idbd.tap(udid, [f"star{stars}"], els)
        els, up = self._wait_els(udid, lambda es: self._has(es, "submitReview"), 10.0)
        if not up:
            notes.append(f"[FAIL] {tag} — star {stars} did not open the Review screen")
            return False
        box = next((e for e in els if e.get("type") in ("TextView", "TextField")
                    or _idbd.name(e) == "writeFeedback"), None)
        typed = ""
        if box is not None:
            _idbd.tap_el(udid, box, els)
            time.sleep(0.6)
            _idbd._idb(["ui", "text", "--udid", udid, "Great evening - automated full event"])
            _idbd.press_return(udid)
            typed = " with feedback"
        _idbd.tap(udid, ["submitReview"])
        els, gone = self._wait_els(udid, lambda es: not self._has(es, "submitReview"), 12.0)
        if not gone:
            notes.append(f"[FAIL] {tag} — Submit did not close the Review screen")
            return False
        notes.append(f"[ok] {tag} — rated {stars}★{typed} and submitted")
        return True

    def _c_skip_review(self, notes: List[str]) -> bool:
        """`@c_skip_review` — skip the rating: the event's 'Completed' prompt
        `skipReviewCard` (it posts skipReview). MEASURED 2026-10-29: right after, the
        Finished list still offered Skip -- it had not refreshed -- and minutes later
        it offered none, while View PDF still opened the invoice. Done when the prompt
        is gone."""
        tag = "@c_skip_review"
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        els = self._c_rating_prompt(udid, notes, tag)
        if els is None:
            return False
        _idbd.tap(udid, ["skipReviewCard"], els)
        els, gone = self._wait_els(udid, lambda es: not self._has(es, "skipReviewCard"), 8.0)
        if not gone:
            notes.append(f"[FAIL] {tag} — tapped Skip but the rating prompt stayed")
            return False
        notes.append(f"[ok] {tag} — skipped the rating")
        return True

    def _c_invoice(self, notes: List[str]) -> bool:
        """`@c_invoice` — the diner's invoice: Finished tab, the event, View PDF
        (`finishedViewPdf`, Wallet/CardData.js:2370) -> an in-app invoice built from the
        event (no ids; read by OCR) -> `back`. The app shows View PDF only while the
        rating was NOT skipped (`!data.skipReview`, CardData.js:2368) -- reported, not
        worked around. Done when the invoice shows its VAT rows and Total."""
        tag = "@c_invoice"
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        els = self._c_finished_card(udid, notes, tag)
        if els is None:
            return False
        if not self._has(els, "finishedViewPdf"):
            notes.append(f"[FAIL] {tag} — the finished event has no View PDF (the source "
                         f"hides it when `data.skipReview`, CardData.js:2368; on 2026-10-29 it "
                         f"still showed after a skip)")
            return False
        # View PDF sits low in the opened event, often below the screen (MEASURED
        # 2026-10-29: y 1123 on an 874-high phone): drag it into view, then tap.
        from automation.scenarios import screen_text
        w, h = _idbd.app_size(els)
        is_invoice = lambda: any(k in t[0].upper() for t in screen_text.read_text(udid)  # noqa: E731
                                 for k in ("RESERVATION", "VAT 3", "TOTAL"))
        for attempt in (1, 2):
            pdf = next((e for e in _idbd.describe_all(udid) if _idbd.name(e) == "finishedViewPdf"),
                       None)
            if pdf is None:
                break
            for _ in range(4):
                y = _idbd.frame(pdf)[1]
                if 100 < y < h - 100:
                    break
                _idbd.swipe(udid, w / 2, h * 0.75, w / 2, h * 0.3, 1.2)
                time.sleep(0.8)
                pdf = next((e for e in _idbd.describe_all(udid)
                            if _idbd.name(e) == "finishedViewPdf"), pdf)
            _idbd.tap_el(udid, pdf, None, scroll=False)
            time.sleep(2.5)
            if is_invoice():
                break
        # The whole screen as the box, so it scrolls down to the grand Total: the first
        # screen holds only the Pre-Order block's Total (MEASURED 2026-10-29: 168.27).
        seen = self._read_invoice(udid, (0, 60, w, h - 40))
        if "Total" not in seen:
            notes.append(f"[FAIL] {tag} — View PDF opened no readable invoice; read: {seen}")
            return False
        self._close_invoice(udid)
        notes.append(f"[ok] {tag} — the invoice: " + "; ".join(seen.values()))
        return True

    # ── C-App payment: pay for others, coupon, tip ──────────────────────────
    def _c_sheet_total(self, els: List[dict]) -> Optional[float]:
        return self._card_amount(els, "Total")

    def _c_pay_for(self, spec: str, notes: List[str]) -> bool:
        """`@c_pay_for:Name, Name` — on the C-App's 'Would you also like to pay for…'
        sheet (Components/Modal/index.js:4853), pick these people -- avatars
        `${username}pay`, a picked one gets a second element with the same id -- and
        Proceed (`proceedPayment`). It opens after Checkout when more than one person
        is unpaid. Done when the payment sheet (E-Payment) is up."""
        tag = f"@c_pay_for:{spec}"
        people = [p.strip() for p in (spec or "").split(",") if p.strip()]
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        name = _idbd.name
        pid = lambda e: self._squash(name(e)).lower()  # noqa: E731
        els, up = self._wait_els(udid, lambda es: self._has(es, "proceedPayment"), 12.0)
        if not up:
            notes.append(f"[FAIL] {tag} — the 'pay for others' sheet did not open (it shows "
                         f"when more than one person is still unpaid)")
            return False
        for person in people:
            want = self._squash(person).lower() + "pay"
            hits = [e for e in els if pid(e) == want]
            if not hits:
                shown = sorted({name(e)[:-3] for e in els if pid(e).endswith("pay")
                                and name(e) not in ("Mepay",)})
                notes.append(f"[FAIL] {tag} — {person} is not offered (offered: "
                             f"{', '.join(shown) or 'nobody'})")
                return False
            if len(hits) >= 2:
                continue                       # already picked
            _idbd.tap_el(udid, hits[0], els)
            els, ok = self._wait_els(udid, lambda es: sum(1 for e in es if pid(e) == want) >= 2
                                     or self._toast(es), 5.0)
            if self._toast(els) or sum(1 for e in els if pid(e) == want) < 2:
                notes.append(f"[FAIL] {tag} — {person} did not get picked"
                             + (f" ({self._toast(els)!r})" if self._toast(els) else ""))
                return False
        _idbd.tap(udid, ["proceedPayment"], els)
        els, sheet = self._wait_els(udid, lambda es: self._has(es, "ePayment")
                                    or self._has(es, "eApplyCoupons"), 15.0)
        if not sheet:
            notes.append(f"[FAIL] {tag} — Proceed did not open the payment sheet")
            return False
        self._paid_for_others = people
        total = self._c_sheet_total(els)
        notes.append(f"[ok] {tag} — paying for {', '.join(people)} too"
                     + (f"; the sheet's Total reads {total:.2f} €" if total is not None else ""))
        return True

    def _c_coupon(self, notes: List[str]) -> bool:
        """`@c_coupon` — Apply Coupons (`eApplyCoupons`, Modal/index.js:6458): the list
        of coupons this diner OWNS (ApplyCoupon/index.js, no code entry); a tap applies
        one. MEASURED 2026-10-29 on the installed build: an owned coupon reads '31.5%
        OFFER 31.5 31.5 - In 2 Days' (not `${price}Coupon`), and a diner who owns none
        sees 'NO COUPONS DATA FOUND' and `couponGetMore` -- 'Buy Coupons' with the
        restaurant's coupons for XP ('31.5% off … 0': free); the first is taken then.
        Done when the sheet's Total fell."""
        tag = "@c_coupon"
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        name = _idbd.name
        els = _idbd.describe_all(udid)
        total0 = getattr(self, "_c_total_before_coupon", None) or self._c_sheet_total(els)
        if not self._has(els, "couponClose"):
            ok, how = _idbd.tap(udid, ["eApplyCoupons"], els)
            if not ok:
                notes.append(f"[FAIL] {tag} — no Apply Coupons on the payment sheet ({how})")
                return False
        owned = lambda es: [e for e in es if "OFFER" in name(e).upper()  # noqa: E731
                            or (name(e).endswith("Coupon")
                                and name(e) not in ("eApplyCoupons", "applyCoupon"))]
        els, got = self._wait_els(udid, owned, 8.0)
        took = ""
        if not got and self._has(els, "couponGetMore"):
            _idbd.tap(udid, ["couponGetMore"], els)
            els, offers = self._wait_els(udid, lambda es: [e for e in es
                                                           if "% off" in name(e)], 8.0)
            if offers:
                took = name(offers[0]).split(" On ")[0]
                _idbd.tap_el(udid, offers[0], els)
                els, got = self._wait_els(udid, owned, 10.0)
        if not got:
            notes.append(f"[FAIL] {tag} — the diner owns no coupon for this event and none "
                         f"could be taken from Buy Coupons")
            return False
        coupon = name(got[0])[:40]
        _idbd.tap_el(udid, got[0], els)
        time.sleep(1.5)
        if self._has(_idbd.describe_all(udid), "couponClose"):
            _idbd.tap(udid, ["couponClose"])

        def fell(es):
            v = self._c_sheet_total(es)
            return v if v is not None and total0 is not None and v < total0 - 0.005 else None

        els, now = self._wait_els(udid, fell, 10.0)
        if now is None:
            notes.append(f"[FAIL] {tag} — tapped {coupon!r} but the Total did not fall "
                         f"(was {total0}, reads {self._c_sheet_total(els)})")
            return False
        self._c_coupon_pct = round((1 - now / total0) * 100, 1)
        notes.append(f"[ok] {tag} — " + (f"took {took!r} from Buy Coupons; " if took else "")
                     + f"applied {coupon!r}: Total {total0:.2f} € → {now:.2f} € "
                     f"(−{self._c_coupon_pct} %)")
        return True

    def _c_tip(self, amount: str, notes: List[str]) -> bool:
        """`@c_tip:<euros>` — the tip field `ePayTip` (an amount; the app strips commas,
        so a dot). Done when the Total rose by it."""
        tag = f"@c_tip:{amount}"
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()
        try:
            tip = float(amount)
        except ValueError:
            notes.append(f"[FAIL] {tag} — the tip must be a number (dot for decimals)")
            return False
        els = _idbd.describe_all(udid)
        total0 = self._c_sheet_total(els)
        ok, how = _idbd.fill(udid, "ePayTip", f"{tip:.2f}")
        if not ok:
            _idbd.tap(udid, ["ePayTip"], els)
            time.sleep(0.5)
            _idbd._idb(["ui", "text", "--udid", udid, f"{tip:.2f}"])
            _idbd.press_return(udid)
        want = (total0 or 0) + tip
        els, now = self._wait_els(udid, lambda es: (lambda v: v if v is not None and
                                  abs(v - want) < 0.015 else None)(self._c_sheet_total(es)), 8.0)
        if now is None:
            notes.append(f"[FAIL] {tag} — typed the tip but the Total reads "
                         f"{self._c_sheet_total(els)} (expected {want:.2f})")
            return False
        notes.append(f"[ok] {tag} — Total {total0:.2f} € + tip {tip:.2f} € = {now:.2f} €")
        return True

    def _filter_orders(self, r: ScenarioRunner, notes: List[str], status: str = "Reserved") -> bool:
        """Apply the My Orders status filter (Screens/Orders/index.js's `Filter`,
        rendered via Components/Modal/index.js). It is a magnus Overlay — its
        options (Reserved/In Progress/Order/Serve/Payment Notified/Payment
        Completed) and Apply carry no accessibility id, the same trap
        locator_map.md documents for VOID/COMP/SPLIT — so they are picked by
        OCR text with the same `_ocr_tap` those tokens already use. The 'N
        result(s) shown for « status »' line (Orders/index.js:425) renders
        OUTSIDE the overlay in the normal tree, so it proves the filter
        actually took effect, not just that the sheet closed."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        tag = f"@filter_orders:{status}"
        ok, how = self._ocr_tap(udid, None, status)
        if not ok:
            notes.append(f"[FAIL] {tag} — {how}")
            return False
        time.sleep(0.6)
        ok2, how2 = self._ocr_tap(udid, None, "Apply")
        if not ok2:
            notes.append(f"[FAIL] {tag} — picked {status!r} ({how}) but {how2}")
            return False

        def _narrowed(es) -> bool:
            return any("result" in _idbd.name(e).lower() and "shown" in _idbd.name(e).lower()
                       for e in es)

        els, done = self._wait_els(udid, _narrowed, 8.0)
        line = ""
        if done:
            line = next((_idbd.name(e) for e in els if "result" in _idbd.name(e).lower()
                        and "shown" in _idbd.name(e).lower()), "")
        else:
            # The info line can render without reaching the accessibility tree on
            # some builds (see idb_coords/_choose_card's 'content vs. screen' notes
            # elsewhere in this file) — OCR reads the pixels regardless, the same
            # fallback @void_item/@notify_payment rely on.
            for _ in range(6):
                if self._ocr_sees(udid, "result"):
                    done = True
                    break
                time.sleep(1.0)
        if not done:
            notes.append(f"[FAIL] {tag} — tapped {status!r} and Apply but the narrowed-list "
                         f"'result(s) shown for' line never appeared")
            return False
        notes.append(f"[ok] {tag} — {how}; {how2}" + (f"; {line!r}" if line else "; confirmed by OCR"))
        return True

    # BookingCard (Components/BookingCard/index.js:59-102) is ONE accessible element,
    # '<Name>card<Status>', so its ⋮ -- a TouchableOpacity with no id (styles.option:
    # padding 8, marginEnd 10) at the right of the card's top row -- is tapped at an
    # offset from the card's top-right corner. The touchable's frame includes the
    # card's margins (ml="md", my="lg"); the top row is a 16pt margin + a 13pt name.
    _CARD_MENU_OFFSETS = ((26.0, 36.0), (26.0, 26.0))
    _MENU_UP = ("resetTableBtn", "confirmEventBtn", "cancelEvent", "transferBtn")
    _ORDER_UP = ("selectAllItemsBtn", "addItemsBtn", "closeEventModal", "AssignTableBtn")

    @staticmethod
    def _assigned_table(notes: List[str]) -> str:
        """The table @assign_table picked earlier in this segment ('I2'), else ''."""
        for n in reversed(notes):
            m = re.search(r"@assign_table — assigned '([^']+)'", n)
            if m:
                t = re.sub(r"^tableChip", "", m.group(1))
                return "" if re.fullmatch(r"T\d+AssignAnyBtn", t) else t
        return ""

    @staticmethod
    def _same_table(seen: str, table: str) -> bool:
        """OCR reads table 'I16' as '116' (measured, see _open_from_panel)."""
        def key(s: str) -> str:
            s = re.sub(r"[^a-z0-9]", "", (s or "").lower())
            return s.translate(str.maketrans("il|o", "1110"))
        return bool(table) and key(seen) == key(table)

    def _row_menu_cards(self, els: List[dict], hour_lbl: str,
                        want: str = "inprogress") -> List[dict]:
        """This diner's cards with status *want* (In Progress by default) in the
        booked hour's row, left to right."""
        name, frame = _idbd.name, _idbd.frame
        hours = [(name(e), frame(e)[1] + frame(e)[3] / 2) for e in els
                 if re.fullmatch(r"\d{2}:00", name(e))]
        out = []
        for e in els:
            lbl = name(e)
            if "card" not in lbl.lower() or frame(e)[2] <= 60:
                continue
            who, status = self._split_card(lbl)
            if CONSUMER_NAME.lower() not in who.lower() or status != want:
                continue
            near = min(hours, key=lambda h: abs(h[1] - (frame(e)[1] + 20)), default=None)
            if near and near[0] == hour_lbl:
                out.append(e)
        return sorted(out, key=lambda e: frame(e)[0])

    def _bring_menu_into_view(self, udid: str, hour_lbl: str, k: int, dx: float, dy: float,
                              want: str = "inprogress") -> Tuple[Optional[dict], List[dict], str]:
        """Scroll the board until the k-th card's ⋮ point is on the board, left of the
        My Orders panel. Returns (card, els, why-not)."""
        frame = _idbd.frame
        last = None
        for _ in range(8):
            els = _idbd.describe_all(udid)
            cards = self._row_menu_cards(els, hour_lbl, want)
            if len(cards) <= k:
                return None, els, f"{len(cards)} '{want}' card(s) in the {hour_lbl} row"
            card = cards[k]
            w_app, h_app = _idbd.app_size(els)
            head = next((e for e in els if _idbd.name(e).startswith("My Orders")), None)
            # The hour row's scroll view is clipped well left of the My Orders title.
            # MEASURED (run 20d429b3, 12:00 row with 5 cards): the title at x≈823, the
            # row cut off at x≈737. With 'title - 30' the drag started at x≈752 --
            # outside the row, so nothing moved -- and a ⋮ in the clipped strip
            # counted as on the board.
            right = (frame(head)[0] - 90) if head else w_app * 0.60
            x, y, w, h = frame(card)
            px, py = x + w - dx, y + dy
            if 0.25 * h_app <= py <= 0.8 * h_app and 0 <= px <= right - 8:
                return card, els, ""
            if frame(card) == last:
                return None, els, f"the {hour_lbl} card did not move when the board was scrolled"
            last = frame(card)
            if not 0.25 * h_app <= py <= 0.8 * h_app:
                hour = next((e for e in els if _idbd.name(e) == hour_lbl), None)
                col = _idbd.centre(hour)[0] if hour else x / 2   # the hours column
                travel = min(max(abs(py - 0.5 * h_app), 80.0), 0.4 * h_app)
                start = 0.75 * h_app if py > 0.5 * h_app else 0.3 * h_app
                end = start - travel if py > 0.5 * h_app else start + travel
                _idbd.swipe(udid, col, start, col, end, max(1.0, travel / 200.0))
            else:                                   # off to the right: drag the row left
                row_y = y + h / 2
                travel = min(px - (right - 60), 0.6 * right)
                _idbd.swipe(udid, right - 40, row_y, right - 40 - travel, row_y,
                            max(1.0, travel / 200.0))
            time.sleep(0.8)
        return None, _idbd.describe_all(udid), "could not scroll the card's ⋮ onto the board"

    def _reset_table(self, notes: List[str]) -> bool:
        """`@reset_table` -- My Bookings: the booking's ⋮ menu → Reset table → Confirm →
        'Reset' on the app's Alert (Screens/Home/index.js `_resetTable`), on THIS run's
        booking after @assign_table put it In Progress.

        Finds the card with the board's 'Table' filter (tableBtn) on: only bookings
        holding a table are listed there (Screens/Home/index.js:1149-1152). With
        several of this diner's In Progress cards in the booked hour's row (shared
        account, earlier runs), the one showing the table @assign_table picked is
        ours; with no way to tell, it refuses rather than reset someone else's.

        Proves the reset: the success toast, AND this card leaving the Table view
        (a booking without a table is not listed there). Restores the 'All' filter.
        canResetTable (Util/canResetTable.js) disables the option on a booking with
        items, a kitchen order or a bill; the app then refuses -- reported, not retried."""
        tag = "@reset_table"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        m = re.match(r"^(\d{1,2})", getattr(self, "_booked_slot", "") or "")
        if not m:
            notes.append(f"[FAIL] {tag} — no booked slot known: book in an earlier segment")
            return False
        hour_lbl = f"{int(m.group(1)):02d}:00"
        if not self._waiter_board(notes):
            return False
        ok, how = _idbd.tap(udid, ["tableBtn"])
        if not ok:
            notes.append(f"[FAIL] {tag} — the board's Table filter (tableBtn): {how}")
            return False
        els, cards = self._wait_els(udid, lambda es: self._row_menu_cards(es, hour_lbl), 8.0)
        if not cards:
            notes.append(f"[FAIL] {tag} — no In Progress card for '{CONSUMER_NAME}' in the "
                         f"{hour_lbl} row of the Table view (was a table assigned?)")
            return False
        before = len(cards)
        table = self._assigned_table(notes)
        dx, dy = self._CARD_MENU_OFFSETS[0]
        k = 0
        if before > 1:
            if not table:
                notes.append(f"[FAIL] {tag} — {before} In Progress cards for "
                             f"'{CONSUMER_NAME}' in the {hour_lbl} row and the assigned table "
                             f"is unknown: refusing to reset a booking that may not be this run's")
                return False
            from automation.scenarios import screen_text
            k, seen = None, []
            for i in range(before):
                card, els, why = self._bring_menu_into_view(udid, hour_lbl, i, dx, dy)
                if card is None:
                    seen.append(f"#{i + 1}: {why}")
                    continue
                texts = [t for t, *_ in screen_text.read_text(udid, region=self._corners(card))]
                if any(self._same_table(t, table) for t in texts):
                    k = i
                    break
                seen.append(f"#{i + 1}: {' '.join(texts)[:40]!r}")
            if k is None:
                notes.append(f"[FAIL] {tag} — {before} In Progress cards for "
                             f"'{CONSUMER_NAME}' in the {hour_lbl} row, none showing table "
                             f"{table}: " + "; ".join(seen))
                return False
            notes.append(f"    · {before} In Progress cards in the {hour_lbl} row; "
                         f"#{k + 1} shows table {table} — this run's")
        menu_els, opened = [], False
        for dx, dy in self._CARD_MENU_OFFSETS:
            card, els, why = self._bring_menu_into_view(udid, hour_lbl, k, dx, dy)
            if card is None:
                notes.append(f"[FAIL] {tag} — {why}")
                return False
            x, y, w, _h = _idbd.frame(card)
            _idbd.tap_point(udid, x + w - dx, y + dy)
            menu_els, opened = self._wait_els(
                udid, lambda es: any(self._has(es, i) for i in self._MENU_UP + self._ORDER_UP), 6.0)
            if opened and any(self._has(menu_els, i) for i in self._MENU_UP):
                break
            if not opened and self._ocr_sees(udid, "Select an action"):
                break
            # The tap hit the card body: the booking opened. Back to the board.
            notes.append(f"    · the tap at ({x + w - dx:.0f}, {y + dy:.0f}) did not open the "
                         f"⋮ menu" + (" (it opened the booking)" if opened else ""))
            opened = False
            if not self._waiter_board(notes):
                return False
        else:
            notes.append(f"[FAIL] {tag} — the ⋮ menu of '{_idbd.name(card)}' never opened")
            return False
        for ident, text in (("resetTableBtn", "Reset table"), ("confirmEventBtn", "Confirm")):
            if self._has(menu_els, ident):
                ok, how = _idbd.tap(udid, [ident], menu_els, scroll=False)
            else:                                   # the Overlay hides its ids from idb
                ok, how = self._ocr_tap(udid, None, text)
            if not ok:
                notes.append(f"[FAIL] {tag} — {ident}: {how}")
                return False
            time.sleep(0.6)
        els, alert = self._wait_els(udid, lambda es: self._has(es, "Reset"), 6.0)
        if not alert:
            from automation.scenarios import screen_text
            said = next((t for t, *_ in screen_text.read_text(udid)
                         if "not allowed" in t.lower()), "")
            notes.append(f"[FAIL] {tag} — Confirm did not bring up the 'Reset table' alert"
                         + (f"; the app said {said!r}" if said else
                            " (Reset table is disabled when canResetTable is false)"))
            return False
        ok, how = _idbd.tap(udid, ["Reset"], els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — the alert's Reset button: {how}")
            return False
        if not self._see_text("Table has been reset", notes):
            return False
        els, gone = self._wait_els(
            udid, lambda es: len(self._row_menu_cards(es, hour_lbl)) < before, 10.0)
        left = len(self._row_menu_cards(els, hour_lbl))
        _idbd.tap(udid, ["allBtn"], scroll=False)
        if not gone:
            notes.append(f"[FAIL] {tag} — the toast said reset, but the {hour_lbl} row of the "
                         f"Table view still lists {left} of {before} In Progress card(s)")
            return False
        notes.append(f"[ok] {tag} — reset via ⋮ → Reset table → Confirm → Reset; the booking "
                     f"left the Table view ({before} → {left} In Progress card(s) at {hour_lbl})")
        return True

    @staticmethod
    def _corners(e: dict, pad: float = 0.0) -> Tuple[float, float, float, float]:
        """An element's frame as the (x0, y0, x1, y1) region screen_text.read_text
        takes -- NOT idb's (x, y, w, h): MEASURED (run 3f9f259f) a frame passed as the
        region cropped nothing and read ''."""
        x, y, w, h = _idbd.frame(e)
        return (max(0.0, x - pad), max(0.0, y - pad), x + w + pad, y + h + pad)

    def _header_table_text(self, udid: str, els: List[dict]) -> Tuple[Optional[dict], List[str]]:
        """The Event header's table button (modifyTable, Screens/Event/PaymentDetails.js
        :4913-4935) and the text read off its pixels: it is ONE accessible element,
        so the table name inside it is invisible to idb."""
        from automation.scenarios import screen_text
        btn = next((e for e in els if _idbd.name(e) == "modifyTable"), None)
        if btn is None:
            return None, []
        box = self._corners(btn, 6.0)
        texts = [t for t, *_ in screen_text.read_text(udid, region=box)]
        if not texts:
            texts = [t for t, *_ in screen_text.read_text(udid, region=box, accurate=True)]
        return btn, texts

    def _modify_table(self, notes: List[str]) -> bool:
        """`@modify_table` -- on THIS booking's Event screen (a table assigned, payment
        not yet notified: PaymentDetails.js:4913 only opens the sheet while
        reminder == false), tap the header's table (modifyTable), pick a free table
        OTHER than the current one on the 'Modify Table' sheet
        (Components/Modal/EventTableSelect, chips named after the tables, booked ones
        not offered), Confirm (AssignTableBtn / applyTableBtn), and prove the move:
        the sheet closes and the header now prints the new table."""
        tag = "@modify_table"
        udid = self._business_udid()
        name, frame = _idbd.name, _idbd.frame
        els, btn = self._wait_els(udid, lambda es: next(
            (e for e in es if name(e) == "modifyTable"), None), 8.0)
        if btn is None:
            notes.append(f"[FAIL] {tag} — the Event header's table button (modifyTable) is "
                         f"not on screen (is the booking open?)")
            return False
        old = self._assigned_table(notes)
        _btn, before = self._header_table_text(udid, els)
        if not old:
            old = next((t.strip() for t in before if re.fullmatch(r"[A-Za-z|]{1,2}\d{1,3}", t.strip())), "")
        ok, how = _idbd.tap(udid, ["modifyTable"], els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — modifyTable: {how}")
            return False

        def sheet(es):
            head = next((e for e in es if _norm(name(e)) == "modifytable"
                         and e.get("type") == "StaticText"), None)
            commit = next((e for e in es if name(e) in ("applyTableBtn", "AssignTableBtn")), None)
            if head is None or commit is None:
                return None
            chip_re = re.compile(r"(?:tableChip\w+|[A-Za-z]{1,2}\d{1,3})\Z")
            chips = [e for e in es if chip_re.fullmatch(name(e))
                     and frame(head)[1] < frame(e)[1] < frame(commit)[1] and frame(e)[2] > 0]
            return (commit, chips) if chips else None

        els, up = self._wait_els(udid, sheet, 12.0)
        if not up:
            notes.append(f"[FAIL] {tag} — tapped modifyTable but no 'Modify Table' sheet with "
                         f"free tables came up")
            return False
        commit, chips = up
        others = sorted((c for c in chips
                         if not self._same_table(re.sub(r"^tableChip", "", name(c)), old)),
                        key=lambda e: frame(e)[0])
        if not others:
            notes.append(f"[FAIL] {tag} — the sheet offers no free table other than "
                         f"{old or 'the current one'}: {[name(c) for c in chips]}")
            return False
        pick = others[0]
        new = re.sub(r"^tableChip", "", name(pick))
        ok, how = _idbd.tap_el(udid, pick, els, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — table chip {new}: {how}")
            return False
        time.sleep(0.6)
        ok, how = _idbd.tap_el(udid, commit, scroll=False)
        if not ok:
            notes.append(f"[FAIL] {tag} — Confirm: {how}")
            return False
        closed = False
        for _ in range(12):
            time.sleep(1.0)
            if not self._table_sheet_open():
                closed = True
                break
        if not closed:
            said = self._screen_toast(udid)
            notes.append(f"[FAIL] {tag} — picked {new} and tapped Confirm but the sheet stayed "
                         f"open" + (f"; the app said {said!r}" if said else ""))
            return False
        texts: List[str] = []
        for _ in range(5):
            els = _idbd.describe_all(udid)
            _btn, texts = self._header_table_text(udid, els)
            if any(self._same_table(t, new) for t in texts):
                notes.append(f"[ok] {tag} — moved from {old or '?'} to {new}; the Event header "
                             f"now reads {new}")
                return True
            time.sleep(1.5)
        notes.append(f"[FAIL] {tag} — picked {new} and the sheet closed, but the Event header "
                     f"reads {texts or 'nothing'} (was {before or old or '?'})")
        return False

    _PENDING = "confirmationpending"

    def _pending_action(self, tag: str, action: Tuple[str, str],
                        notes: List[str]) -> Tuple[str, int]:
        """My Bookings: THIS run's waiter-created appointment (still 'Confirmation
        Pending') → its ⋮ → *action* (id, OCR text) → Confirm (confirmEventBtn), on
        Components/Modal/EventAction/index.js. Returns (hour row label, the row's
        pending card count before), or ('', 0) after noting why it failed.

        The appointment is told from others by the booked slot: with several of the
        diner's pending cards in the hour's row (shared account), the one printing
        the slot ('12:20 - …') is ours; with no single match it refuses rather than
        act on someone else's."""
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        slot = getattr(self, "_booked_slot", "") or ""
        m = re.match(r"^(\d{1,2}):\d{2}$", slot)
        if not m:
            notes.append(f"[FAIL] {tag} — no booked slot known: create the appointment in "
                         f"this flow first")
            return "", 0
        hour_lbl = f"{int(m.group(1)):02d}:00"
        if not self._waiter_board(notes):
            return "", 0
        want = self._PENDING
        els, cards = self._wait_els(udid, lambda es: self._row_menu_cards(es, hour_lbl, want), 10.0)
        if not cards:
            notes.append(f"[FAIL] {tag} — no Confirmation Pending card for '{CONSUMER_NAME}' "
                         f"in the {hour_lbl} row (was the appointment saved?)")
            return "", 0
        before = len(cards)
        dx, dy = self._CARD_MENU_OFFSETS[0]
        k = 0
        if before > 1:
            from automation.scenarios import screen_text
            slot_re = re.compile(rf"(?<!\d){re.escape(slot)}(?!\d)")
            hits, seen = [], []
            for i in range(before):
                card, els, why = self._bring_menu_into_view(udid, hour_lbl, i, dx, dy, want)
                if card is None:
                    seen.append(f"#{i + 1}: {why}")
                    continue
                text = " ".join(t for t, *_ in screen_text.read_text(udid, region=self._corners(card)))
                if slot_re.search(text):
                    hits.append(i)
                seen.append(f"#{i + 1}: {text[:40]!r}")
            if len(hits) != 1:
                notes.append(f"[FAIL] {tag} — {before} Confirmation Pending cards for "
                             f"'{CONSUMER_NAME}' in the {hour_lbl} row, {len(hits)} showing "
                             f"{slot}: refusing to act on one that may not be this run's ("
                             + "; ".join(seen) + ")")
                return "", 0
            k = hits[0]
            notes.append(f"    · {before} pending cards in the {hour_lbl} row; #{k + 1} shows "
                         f"{slot} — this run's")
        menu_els, card = [], None
        for dx, dy in self._CARD_MENU_OFFSETS:
            card, els, why = self._bring_menu_into_view(udid, hour_lbl, k, dx, dy, want)
            if card is None:
                notes.append(f"[FAIL] {tag} — {why}")
                return "", 0
            x, y, w, _h = _idbd.frame(card)
            _idbd.tap_point(udid, x + w - dx, y + dy)
            menu_els, opened = self._wait_els(
                udid, lambda es: any(self._has(es, i) for i in self._MENU_UP), 6.0)
            if opened or self._ocr_sees(udid, "Select an action"):
                break
            notes.append(f"    · the tap at ({x + w - dx:.0f}, {y + dy:.0f}) did not open the ⋮ menu")
            if not self._waiter_board(notes):
                return "", 0
        else:
            notes.append(f"[FAIL] {tag} — the ⋮ menu of '{_idbd.name(card)}' never opened")
            return "", 0
        for ident, text in (action, ("confirmEventBtn", "Confirm")):
            if self._has(menu_els, ident):
                ok, how = _idbd.tap(udid, [ident], menu_els, scroll=False)
            else:                                   # the Overlay hides its ids from idb
                ok, how = self._ocr_tap(udid, None, text)
            if not ok:
                notes.append(f"[FAIL] {tag} — {ident}: {how}")
                return "", 0
            time.sleep(0.6)
        return hour_lbl, before

    def _cancel_event(self, notes: List[str]) -> bool:
        """`@cancel_event` -- My Bookings: THIS run's waiter-created appointment (still
        'Confirmation Pending') → its ⋮ → 'Cancel the event' (cancelEvent) → Confirm
        (confirmEventBtn), Components/Modal/EventAction/index.js. Cancel is enabled only
        for a business-created event with no kitchen order and no pre-order (:74-80);
        Screens/Home/index.js `_cancelAppointment` posts cancellEvent and toasts 'Event
        has been cancelled successfully!' (or 'Something went wrong!'). Proves it: the
        card leaving the row's pending cards AND a Cancelled card in that row printing
        this run's slot; 'Something went wrong!' fails it. Never cancels a card it
        cannot tell is this run's (_pending_action)."""
        tag = "@cancel_event"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        want = self._PENDING
        slot = getattr(self, "_booked_slot", "") or ""
        hour_lbl, before = self._pending_action(tag, ("cancelEvent", "Cancel the event"), notes)
        if not hour_lbl:
            return False
        # MEASURED (run 77458198): the 2.5s toast (Home/index.js:392-395) fell between
        # two iPad screen reads (3-5s each) and @see missed it, while the board already
        # showed the card Cancelled. _cancelAppointment replaces the bookings with the
        # server's answer (:805-810), so the lasting proof is the board, as in
        # _edit_event_time; the toast is noted when it is caught.
        said, left = "", before
        for _ in range(12):
            es = _idbd.describe_all(udid)
            texts = [_idbd.name(e) for e in es] + [e.get("AXLabel") or "" for e in es]
            said = next((t for t in texts if "cancelledsuccessfully" in _norm(t)
                         or "somethingwentwrong" in _norm(t)), said)
            if "somethingwentwrong" in _norm(said):
                break
            left = len(self._row_menu_cards(es, hour_lbl, want))
            if left < before:
                break
            time.sleep(1.0)
        if "somethingwentwrong" in _norm(said):
            notes.append(f"[FAIL] {tag} — Confirm was refused: the app said {said!r}")
            return False
        if left >= before:
            notes.append(f"[FAIL] {tag} — after Confirm the {hour_lbl} row still lists "
                         f"{left} of {before} Confirmation Pending card(s)"
                         + (f"; the app said {said!r}" if said else ""))
            return False
        # MEASURED (run fc6de124): the pending card was gone a read before the board
        # drew it again as Cancelled -- wait for it rather than read the row once.
        self._wait_els(udid, lambda es: self._row_menu_cards(es, hour_lbl, "cancelled"), 10.0)
        from automation.scenarios import screen_text
        slot_re = re.compile(rf"(?<!\d){re.escape(slot)}(?!\d)")
        dx, dy = self._CARD_MENU_OFFSETS[0]
        seen: List[str] = []
        for i in range(8):
            card, _els, why = self._bring_menu_into_view(udid, hour_lbl, i, dx, dy, "cancelled")
            if card is None:
                seen.append(why)
                break
            text = " ".join(t for t, *_ in screen_text.read_text(udid, region=self._corners(card)))
            if slot_re.search(text):
                notes.append(f"[ok] {tag} — cancelled via ⋮ → Cancel the event → Confirm; the "
                             f"{hour_lbl} row's pending cards went {before} → {left} and its "
                             f"Cancelled card reads {text[:30]!r}"
                             + (f"; the app said {said!r}" if said else ""))
                return True
            seen.append(repr(text[:30]))
        notes.append(f"[FAIL] {tag} — the pending card left the {hour_lbl} row, but no "
                     f"Cancelled card there prints {slot}: " + "; ".join(seen))
        return False

    _EDIT_CHIP_RE = re.compile(r"(\d{2}:\d{2})Btn")

    def _edit_event_time(self, notes: List[str]) -> bool:
        """`@edit_event_time` -- My Bookings: THIS run's waiter-created appointment
        (Confirmation Pending, no waiter yet: 'Edit the event' is enabled only then,
        EventAction/index.js:175) → ⋮ → editEvent → Confirm opens the Edit Appointment
        panel (Components/EditEventModal). It moves the appointment to the next time
        chip after the booked slot (`<HH:mm>Btn`, :869-901) and taps Update
        (saveEditBtn); saveEdit posts editReservation and toasts 'Reservation updated'
        (or 'Could not update reservation'). Proves the toast and the panel closing,
        then makes the NEW time this run's slot, so a later step (the consumer's
        invitation) is checked against it."""
        tag = "@edit_event_time"
        udid = getattr(self, "_cur_udid", "") or self._business_udid()
        old = getattr(self, "_booked_slot", "") or ""
        hour_lbl, _before = self._pending_action(tag, ("editEvent", "Edit the event"), notes)
        if not hour_lbl:
            return False

        def panel(es):
            if not self._has(es, "saveEditBtn"):
                return None
            return sorted({m.group(1) for e in es
                           for m in [self._EDIT_CHIP_RE.fullmatch(_idbd.name(e))] if m}) or None

        els, times = self._wait_els(udid, panel, 12.0)
        if not times:
            notes.append(f"[FAIL] {tag} — Confirm did not open the Edit Appointment panel "
                         f"with time chips (saveEditBtn, <HH:mm>Btn)")
            return False
        later = [t for t in times if t > old]
        if not later:
            notes.append(f"[FAIL] {tag} — the Edit Appointment panel offers no time after "
                         f"{old}: {times[:12]}")
            return False
        new = later[0]
        ok, how = _idbd.tap(udid, [f"{new}Btn"], els)
        if not ok:
            notes.append(f"[FAIL] {tag} — time chip {new}Btn: {how}")
            return False
        notes.append(f"    · picked {new}Btn ({how})")
        time.sleep(0.6)
        ok, how = _idbd.tap(udid, ["saveEditBtn"])
        if not ok:
            notes.append(f"[FAIL] {tag} — Update (saveEditBtn): {how}")
            return False
        notes.append(f"    · tapped Update ({how})")
        # handleSubmit refuses with a toast and keeps the panel open (no time, no
        # dining area, or the seat-capacity guard 'Sorry, at … has only N seats',
        # EditEventModal/index.js:638-683). MEASURED (run b5dd8ea4): the panel stayed
        # open and no 'Reservation updated' came -- so read what the app DID say.
        # MEASURED (run 8e8e2196): the panel closed but the ~2s toast fell between two
        # iPad screen reads (3-5s each). saveEdit closes the panel on success AND on
        # failure ('Could not update reservation'), so the lasting proof is the board:
        # this diner's pending card at the new hour now prints the new time.
        refusals = ("sorry", "couldnot", "please", "seats", "cannot", "wrong")
        said, closed = "", False
        for _ in range(12):
            es = _idbd.describe_all(udid)
            texts = [_idbd.name(e) for e in es] + [e.get("AXLabel") or "" for e in es]
            said = next((t for t in texts if "reservationupdated" in _norm(t)
                         or (len(t) > 12 and any(k in _norm(t) for k in refusals))), said)
            if not self._has(es, "saveEditBtn"):
                closed = True
                break
            if said and "reservationupdated" not in _norm(said):
                break                               # refused: the panel stays open
            time.sleep(1.0)
        if not closed or (said and "reservationupdated" not in _norm(said)):
            notes.append(f"[FAIL] {tag} — Update did not save the {new} time: "
                         + (f"the app said {said!r}" if said else
                            "the Edit Appointment panel stayed open, no message on screen"))
            return False
        from automation.scenarios import screen_text
        new_hour = f"{int(new.split(':')[0]):02d}:00"
        slot_re = re.compile(rf"(?<!\d){re.escape(new)}(?!\d)")
        dx, dy = self._CARD_MENU_OFFSETS[0]
        seen: List[str] = []
        for i in range(8):
            card, _els, why = self._bring_menu_into_view(udid, new_hour, i, dx, dy, self._PENDING)
            if card is None:
                seen.append(why)
                break
            text = " ".join(t for t, *_ in screen_text.read_text(udid, region=self._corners(card)))
            if slot_re.search(text):
                self._booked_slot = new
                notes.append(f"[ok] {tag} — moved the appointment from {old} to {new} via ⋮ → "
                             f"Edit the event → {new}Btn → Update; its card now reads "
                             f"{text[:30]!r}" + (f"; the app said {said!r}" if said else ""))
                return True
            seen.append(repr(text[:30]))
        notes.append(f"[FAIL] {tag} — the panel closed but no pending card in the {new_hour} "
                     f"row prints {new}: " + "; ".join(seen))
        return False

    def _wallet_invite_cancelled(self, notes: List[str]) -> bool:
        """`@wallet_invite_cancelled` -- C-App Wallet → Upcoming: THIS run's invitation
        (the booked slot) carries the 'Event Cancelled' band (vya-consumer
        Screens/Wallet/Upcoming.js:1108-1130, shown when el.canceled). The invitation
        is ONE accessible element, '<Restaurant>InviteCard' (:876-918), so its time and
        band are read off the pixels inside that element's frame. Another cancelled
        invitation on the shared account at a different time does not count."""
        tag = "@wallet_invite_cancelled"
        udid = self._consumer_udid()
        slot = getattr(self, "_booked_slot", "") or ""
        if not slot:
            notes.append(f"[FAIL] {tag} — this run booked no slot, so its invitation cannot "
                         f"be told from others on the shared account")
            return False
        from automation.scenarios import screen_text
        slot_re = re.compile(rf"(?<!\d){re.escape(slot)}(?!\d)")
        seen: List[str] = []
        for attempt in range(4):
            els = _idbd.describe_all(udid)
            _w, H = _idbd.app_size(els)
            cards = [e for e in els if _idbd.name(e).endswith(self._INVITE_SUFFIX)
                     and 0 <= _idbd.frame(e)[1] and _idbd.frame(e)[1] + _idbd.frame(e)[3] <= H]
            for c in cards:
                text = " ".join(t for t, *_ in screen_text.read_text(udid, region=self._corners(c)))
                if slot_re.search(text) and "cancelled" in text.lower():
                    notes.append(f"[ok] {tag} — the {slot} invitation reads 'Event Cancelled' "
                                 f"({text[:60]!r})")
                    return True
                seen.append(text[:50])
            if attempt % 2 == 0:                         # pull to refresh
                _idbd.swipe(udid, 200, H * 0.30, 200, H * 0.75, 0.6)
            time.sleep(2.0)
        notes.append(f"[FAIL] {tag} — no invitation for {slot} reads 'Event Cancelled'; "
                     f"invitations read: {seen[-6:] or 'none on screen'}")
        return False

    def _handle_special(self, r, step: str, notes: List[str]) -> bool:
        if (step in self._DONE_BY_PAYMENT
                and (getattr(self, "_booking_status", "") or "").startswith("payment")):
            notes.append(f"[skip] {step} — the booking is already at PAYMENT: its items "
                         f"were served and payment requested in an earlier attempt")
            return True
        if step == "@serve_items":
            udid = getattr(self, "_cur_udid", "") or self._business_udid()
            ok, how = _idbd.tap(udid, ["serveItemsBtn"])
            if ok:
                notes.append(f"[ok] serve items — tapped serveItemsBtn ({how})")
                return True
            # A resumed segment re-runs this step after the run it resumes served
            # everything: no SERVE, and NOTIFY PAYMENT (shown only once every item is
            # served, OrderSummary.js:1313) says so.
            els = _idbd.describe_all(udid)
            if getattr(self, "_resume", None) and self._has(els, "notifyPaymentBtn"):
                notes.append("[ok] serve items — nothing left to serve: every item is "
                             "served (NOTIFY PAYMENT shows; served by the run this one resumes)")
                return True
            # SEND instead of SERVE: the selection is lines not sent yet -- nothing is
            # prepared to serve. MEASURED 2026-10-29 (run a5fff25e): a split made after
            # SEND creates new, unsent lines; the fuzzy fallback then hunted SERVE 240s.
            if self._has(els, "sendItemsBtn"):
                if getattr(self, "_resume", None):
                    notes.append("[ok] serve items — nothing prepared is left to serve (the "
                                 "selection is unsent lines; served by the run this one "
                                 "resumes)")
                    return True
                notes.append("[FAIL] serve items — SEND shows, not SERVE: the selected lines "
                             "are not sent to the kitchen yet, so nothing is prepared to serve")
                return False
            ok, note, _ = self._smart_click(r, "click serveItemsBtn")
            notes.append(f"[{'ok' if ok else 'FAIL'}] serve items — {note}")
            return ok
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
            # After the kitchen marks the items Ready the booking reads SERVE (measured:
            # '4776 SERVE 17:05 - 18:05'); 'inprogress' alone skipped it and the step
            # timed out with the booking plainly listed. In Progress covers a partly
            # readied order, Payment Done a diner who already paid in the C-App.
            return self._open_reservation(r, notes, statuses=("serve", "inprogress", "payment"),
                                          what="@open_order")
        if step == "@to_checkout":
            return self._to_checkout(r, notes)
        if step == "@pay_stripe":
            return self._pay_stripe(r, notes)
        if step == "@consumer_home":
            return self._consumer_home(r, notes)
        if step == "@open_event":
            # THIS run's booking whatever stage it reached: a resumed waiter segment
            # finds it IN PROGRESS (its table assigned by the run it resumes). MEASURED
            # 2026-10-29 (run b86deb01): @open_reservation looked for RESERVED only,
            # read 5561 IN PROGRESS, and opened another diner's reserved card instead.
            return self._open_reservation(
                r, notes, statuses=("reserved", "confirmationpending", "inprogress",
                                    "serve", "payment"), what="@open_event")
        if step == "@assign_table":
            st = getattr(self, "_booking_status", "") or ""
            if st in ("inprogress", "serve", "payment"):
                notes.append(f"[ok] @assign_table — nothing to do: the booking is already "
                             f"{st.upper()}, so its table is assigned (a resumed run)")
                return True
            # Fast, verified idb path when the sheet is up; the full routine otherwise.
            return self._assign_table_idb(notes) or self._assign_table(r, notes)
        if step == "@ensure_order_items":
            return self._ensure_order_items(r, notes)
        if step == "@kitchen_ready":
            return self._kitchen_ready(r, notes)
        if step == "@select_all_items":
            return self._select_all_items(notes)
        if step == "@send_to_kitchen":
            return self._send_to_kitchen(notes)
        if step == "@order_later":
            return self._after_booking("orderLater", notes)
        if step == "@pre_order":
            return self._after_booking("preOrderBooking", notes)
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
        if step == "@decline_appointment":
            return self._decline_appointment(r, notes)
        if step.startswith("@wallet_event_shows:"):
            return self._wallet_event_shows(step.split(":", 1)[1].strip(), notes)
        if step == "@consumer_epay":
            return self._consumer_epay(notes)
        if step == "@consumer_checkout":
            return self._consumer_checkout(r, notes)
        if step == "@open_restaurant":
            return self._open_restaurant(r, notes)
        if step == "@leave_review":
            return self._leave_review(r, notes)
        if step.startswith("@duration:"):
            return self._set_duration(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@before:"):
            # `@before:HH:MM` — the run must start before this local time. MEASURED
            # 2026-10-29: the pre-order menu changes at 15:00 (menu 6a85354c… 06:00-15:00
            # allows Pre-Order; the 15:00-23:30 menu does not, and has no biryani, no
            # Pizza Margheritas), so a booking after 15:00 cannot pre-order the flow's
            # dishes ("That's not on the menu", run 0263c36a).
            limit = step.split(":", 1)[1].strip()
            now = datetime.now().strftime("%H:%M")
            if now >= limit:
                notes.append(f"[FAIL] {step} — it is {now}: start this flow before {limit}. "
                             f"Its booking must fall before 15:00, when the restaurant's "
                             f"pre-order menu changes (no biryani / Pizza Margheritas after).")
                return False
            notes.append(f"[ok] {step} — {now}, in time for the morning pre-order menu")
            return True
        if step.startswith("@c_add:"):
            return self._c_add_product(step.split(":", 1)[1], notes)
        if step == "@c_confirm_order":
            return self._c_confirm_order(notes)
        if step.startswith("@w_add:"):
            return self._w_add_item(r, step.split(":", 1)[1], notes)
        if step.startswith("@w_qty:"):
            return self._w_qty(step.split(":", 1)[1], notes)
        if step.startswith("@w_split:"):
            return self._w_split(step.split(":", 1)[1], notes)
        if step.startswith("@w_void:"):
            return self._w_void(step.split(":", 1)[1], notes)
        if step.startswith("@w_comp:"):
            return self._w_comp(step.split(":", 1)[1], notes)
        if step.startswith("@w_pay_for:"):
            payer, _, who = step.split(":", 1)[1].partition(">")
            return self._pay_for_all(notes, payer.strip(),
                                     [p.strip() for p in who.split(",") if p.strip()])
        if step.startswith("@w_payer:"):
            return self._w_payer(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@w_tip:"):
            return self._w_tip(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@w_discount:"):
            return self._w_discount(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@c_pay_for:"):
            return self._c_pay_for(step.split(":", 1)[1], notes)
        if step == "@c_coupon":
            return self._c_coupon(notes)
        if step == "@w_invoice":
            return self._w_invoice(notes)
        if step == "@send_if_any":
            return self._send_if_any(r, notes)
        if step == "@c_skip_review":
            return self._c_skip_review(notes)
        if step.startswith("@c_rate:"):
            return self._c_rate(step.split(":", 1)[1].strip(), notes)
        if step == "@c_invoice":
            return self._c_invoice(notes)
        if step.startswith("@c_tip:"):
            return self._c_tip(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@w_move:"):
            return self._w_grid(step.split(":", 1)[1], "assign", notes)
        if step.startswith("@w_gsplit:"):
            return self._w_grid(step.split(":", 1)[1], "split", notes)
        if step == "@invite_guests" or step.startswith("@invite_guests:"):
            # `@invite_guests:5` or `@invite_guests:5+Noolu Naga` (+ phone contacts)
            arg, *contacts = [a.strip() for a in step.partition(":")[2].split("+")]
            if arg and not arg.isdigit():
                notes.append(f"[FAIL] {step} — the guest count must be a number")
                return False
            return self._invite_guests(int(arg or 3), notes, [c for c in contacts if c])
        if step == "@void_item":
            return self._void_item(notes)
        if step == "@add_item_for_all":
            return self._add_item_for_all(r, notes)
        if step == "@split_item":
            return self._split_item(notes)
        if step == "@comp_item":
            return self._comp_item(notes)
        if step == "@notify_payment":
            return self._notify_payment(notes)
        if step == "@pay_for_all":
            return self._pay_for_all(notes)
        if step == "@waiter_close_table":
            return self._waiter_close_table(r, notes)
        if step == "@waiter_board":
            return self._waiter_board(notes)
        if step.startswith("@filter_orders:"):
            return self._filter_orders(r, notes, step.split(":", 1)[1].strip())
        if step == "@reset_table":
            return self._reset_table(notes)
        if step == "@cancel_event":
            return self._cancel_event(notes)
        if step == "@modify_table":
            return self._modify_table(notes)
        if step == "@edit_event_time":
            return self._edit_event_time(notes)
        if step == "@wallet_invite_cancelled":
            return self._wallet_invite_cancelled(notes)
        if step == "@close_table":
            return self._close_table(notes)
        if step.startswith("@wait_screen:"):
            return self._await_screen(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@see:"):
            return self._see_text(step.split(":", 1)[1].strip(), notes)
        if step.startswith("@pay:") and self._skip_if_settled(step, notes):
            return True
        if step.startswith("@pay:"):
            method = step.split(":", 1)[1]
            if method in self._PAY_BUTTONS:          # the Business app's payment section
                return self._pay_business(method, notes)
            return self._pay(r, method, notes)
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
    _PLAIN_STEP_TOKENS = {
        # The duration chips became a slider (consumer Reservation.js, 2026-10):
        # 'Not Sure' is now the last stop of the bar, not a button with that text.
        "click Checkout": "@consumer_checkout",
        # The card can be below Home's first screen (lazy list): scroll to find it
        # instead of letting the Appium lookup hang 240s (2026-10-27).
        "click NylaiKitchen2": "@open_restaurant",
        "select Not Sure": "@duration:not_sure",
        "select 1 hr": "@duration:1",
        "select 2 hrs": "@duration:2",
        "select 2 hr": "@duration:2",
        "select 3 hrs": "@duration:3",
        "select 3 hr": "@duration:3",
        "accept the appointment": "@accept_appointment",
        # Select All is 'selectAll' on the iPad and 'selectAllItemsBtn' on a phone,
        # and on either it is REPLACED by 'Unselect' once anything is selected --
        # as it is right after the waiter adds items. The plain click then hunted
        # a control that could not appear and hung the segment for 240s.
        "click selectAllItemsBtn": "@select_all_items",
        "click selectAll": "@select_all_items",
        # A tap on SEND was reported as done without checking the order went.
        "click sendItemsBtn": "@send_to_kitchen",
        # After booking: the 1 hr dialog's buttons, or the Wallet card for 'Not Sure'.
        "click orderLater": "@order_later",
        "click serveItemsBtn": "@serve_items",
        "click preOrderBooking": "@pre_order",
    }

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
        # The simulator this step acts on. (Used below by the LogBox branch, which
        # referenced an undefined `udid` -- a NameError swallowed by its except, so
        # that retry never ran.)
        udid = (getattr(self, "_cur_udid", "") or self._consumer_udid())
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
            # FIRST: idb, VERIFIED. One screen read (~1s), then describe-point must
            # confirm the element itself is under the tap point before tapping --
            # an element scrolled out of its scroll view still reports a frame, and
            # a blind tap there lands on whatever is drawn at those pixels (measured:
            # a time chip's frame that was really the Save button). Unique name
            # only; anything idb cannot confirm falls through to the paths below.
            # ~2s a tap against ~12-17s for Appium's find + click on this tree.
            # Not for a checkbox: a dispatched tap is not a toggled box, and only
            # the Appium path below confirms the state (see ScenarioRunner._tap_step).
            # wait=3: if the previous step's screen is still rendering, tap the
            # moment this target appears -- instead of every step sleeping a fixed
            # 0.6s after its own tap whether the next screen needed it or not.
            _ok_fast, _how = (False, "checkbox") if r._looks_like_checkbox(ident) \
                else _idbd.tap(udid, self._id_candidates(ident), wait=3.0)
            if _ok_fast:
                return True, f"tapped {ident} ({_how})", False
            if ident in self._SETTLE_IDS:
                settled = self._already_settled()
                if settled:
                    return True, (f"not needed — {settled}; the app closed the "
                                  f"booking itself"), False
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
                                    and 0 <= e["cx"] <= sw and 0 <= e["cy"] <= sh \
                                    and _idbd.hittable(udid, self._raw_el(e)):
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
                        # Same rule as the fast path: only a point describe-point
                        # confirms. A blind centre tap here opened the Wallet tab
                        # (drawn over the bottom of a restaurant card) instead of
                        # the restaurant.
                        if not _idbd.hittable(udid, self._raw_el(e)):
                            break
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
            # idb first: tap, clear, type, read back EXACTLY, close the keyboard.
            # ~5s against ~44s for Appium's click + clear + send_keys + read-back.
            _ok_fill, _got = _idbd.fill(udid, _field, _val)
            if _ok_fill:
                return True, f'typed "{_val}" into "{_field}" (idb, verified)', False
            # Bounded: MEASURED 2026-10-17 (flow6, run cf6a0379) the idb fill of
            # mobileInputBtn did not verify, and this unbounded Appium fallback then
            # sat until the 240s step watchdog killed the segment.
            #
            # A TIMEOUT is not a miss: the abandoned call keeps running (Appium queues
            # it), so falling through to run_one would type into the field a second
            # time while the first fill can still clear/send_keys it (review
            # 2026-10-19). ~44s is a normal Appium fill here, hence 90s.
            def _appium_fill() -> bool:
                try:
                    return bool(_fill_field(r.d, _field, _val))
                except Exception:
                    return False                     # a real miss: the resolver may find it
            _late, _t_af = object(), time.time()
            _res = _bounded_call(_appium_fill, 90.0, _late)
            if _res is _late:
                return False, (f'could not type "{_val}" into "{_field}": the idb fill did not '
                               f'verify and Appium did not answer in {time.time() - _t_af:.0f}s '
                               f'(WDA busy?) — not typing a second time'), False
            if _res:
                time.sleep(0.3)
                return True, f'typed "{_val}" into "{_field}" (direct id)', False
        ms = re.match(r'^\s*(?:scroll|swipe)\s+(up|down|left|right)\s*$', step, re.I)
        if ms:
            # Same gesture as XCUITest's `mobile: swipe` on the app (finger travels
            # that way, from the middle), in ~0.7s instead of ~49s through the
            # resolver's per-step Appium checks.
            if _idbd.swipe_screen(udid, ms.group(1).lower()):
                time.sleep(0.6)
                return True, f"swiped {ms.group(1).lower()} (idb)", False
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
        self._cur_role = role                      # see _consumer_udid
        # Segment 1 keeps the setup lines it showed while the devices got ready.
        notes: List[str] = list(getattr(self, "_setup_notes", []))
        self._setup_notes = []
        started = time.time()
        self._seg_started_at = started             # see _own_panel_card
        self._metro_marks = self._metro_log_sizes()  # see _collect_evidence
        status = "PASS"
        flaky_steps = 0
        # WHERE + evidence: the first step that fails wins — we screenshot the screen at THAT
        # step (not the end of the segment) and remember which step it was, so the report can
        # say exactly where it broke and show that step's screen.
        fail_shot = None
        fail_step = None
        total_steps = len(seg["steps"])
        try:
            _t_seg = time.time()
            # Live Steps: session start and sign-in take 20-30s on a role switch,
            # and the segment used to appear only after both.
            self._persist(seg, "running", notes + [f"▶ starting the {role} session"], 0.0)
            r = self._session_for(role)
            notes.append(f"[ok] session ready on {getattr(self, '_cur_udid', '')[:8]} "
                         f"({time.time() - _t_seg:.1f}s)")
            if role in ("waiter", "kitchen"):
                _t_login, _n_login = time.time(), len(notes)
                self._persist(seg, "running", notes + [f"▶ signing in as {role}"],
                              time.time() - started)
                _acct_ok = self._ensure_business_account(r, role, notes)
                self._stamp_duration(notes, _n_login, time.time() - _t_login)
                if not _acct_ok:
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
                    if _step.startswith("?"):
                        return self._optional_step(r, _step[1:].strip(), notes), False
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
                r._healthy_at = None     # only THIS step's own crash check may be reused
                _watch = self._start_step_watchdog(step, seg)
                _n_before, _t_step = len(notes), time.time()
                _ex = _fut.ThreadPoolExecutor(max_workers=1)
                try:
                    ok, step_flaky = self._await_step(_ex.submit(_exec_step), seg, step,
                                                      notes, _n_before, started)
                    self._stamp_duration(notes, _n_before, time.time() - _t_step)
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
                if not ok and getattr(self, "_too_early", None):
                    # Not a failure: the app will not open the booking yet. Stop here
                    # and say when it can carry on (Resume from failure picks it up).
                    status = "STOPPED"
                    notes.append(f"[stopped] {step} — waiting for the booking's window")
                    break
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
                # A step that went through ScenarioRunner.run_one has JUST checked
                # (its own app_crash turns an ok step into a failure); reading the
                # whole screen again ~1s later found nothing new and cost a read per
                # step. Only skip when that check is fresh and was healthy.
                _fresh = (time.time() - (getattr(r, "_healthy_at", None) or 0)) < 1.5
                crash = None if _fresh else r.app_crash()   # read ONCE — a second call can race a Metro
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

    LIVE_REFRESH = 2.0

    def _await_step(self, future, seg, step: str, notes: List[str], shown: int,
                    started: float):
        """The step's result, within STEP_TIMEOUT (raises TimeoutError, as
        future.result did).

        Meanwhile, whatever the step has noted so far is shown in Live Steps. A long
        step (@add_all_products: 19s, one line per item) otherwise showed only
        'running: <step>' until it returned. `shown` is len(notes) before the step
        started: counting after submit misses a line the step writes at once."""
        import concurrent.futures as _fut
        deadline = time.time() + STEP_TIMEOUT
        while True:
            try:
                return future.result(timeout=max(0.05, min(self.LIVE_REFRESH,
                                                           deadline - time.time())))
            except _fut.TimeoutError:
                if time.time() >= deadline:
                    raise
            if len(notes) != shown:
                shown = len(notes)
                try:
                    self._persist(seg, "running", list(notes) + [f"▶ {step}"],
                                  time.time() - started)
                except Exception:                # display only; never fail the step
                    logger.debug("could not show step progress", exc_info=True)

    @staticmethod
    def _stamp_duration(notes: List[str], start: int, secs: float) -> None:
        """Append how long the step took to its own result line ('[ok] … (2.1s)'),
        so a report shows where a run's time went, step by step."""
        for i in range(len(notes) - 1, start - 1, -1):
            if notes[i].startswith(("[ok]", "[flaky]", "[FAIL]", "[warn]", "[skip]")):
                notes[i] = f"{notes[i]} ({secs:.1f}s)"
                return

    @staticmethod
    def _metro_log_for(bundle: str) -> str:
        """The Metro log of the bundler serving this app ('' if there is none)."""
        try:
            from automation.projects.builder import AppBuilder, metro_log_path
            path = metro_log_path(AppBuilder.metro_port_for(bundle))
            return path if os.path.isfile(path) else ""
        except Exception:
            return ""

    def _metro_log_sizes(self) -> Dict[str, int]:
        """Each app's Metro log size now — where this segment's lines will start."""
        marks: Dict[str, int] = {}
        for bundle in (getattr(self, "consumer_bundle", ""), getattr(self, "business_bundle", "")):
            path = self._metro_log_for(bundle or "")
            if path:
                try:
                    marks[path] = os.path.getsize(path)
                except OSError:
                    pass
        return marks

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
        bundle = (self.consumer_bundle if role in _CONSUMER_ROLES else self.business_bundle) or ""
        proc = bundle.rsplit(".", 1)[-1] if bundle else "Vya"

        try:
            # The failing app's OWN Metro log (each app runs its bundler on its own
            # port), from where it stood when this segment began — so only what the
            # app logged during the failure. MEASURED 2026-10-27: the newest log of
            # any app, tail-read with no time bound, attached an old "JavaScript heap
            # out of memory" line to three unrelated failures, and the AI Agent
            # diagnosed a Metro crash that never happened.
            path = os.getenv("METRO_LOG_PATH") or self._metro_log_for(bundle)
            if not path:
                from automation.projects.builder import METRO_LOG_DIR
                logs = sorted(glob.glob(os.path.join(METRO_LOG_DIR, "metro_*.log")),
                              key=os.path.getmtime, reverse=True)
                path = logs[0] if logs else ""
            since = (getattr(self, "_metro_marks", None) or {}).get(path)
            for line in read_js_console(path, max_lines=12, since=since):
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
        udid = getattr(self, "_cur_udid", "") or self._consumer_udid()

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
                # Wait BEFORE the first sample. It only matters for steps that run
                # past the window (30s), and sampling at t=0 put a full idb screen
                # read in parallel with every step's own first read -- on a step
                # that now takes 2-5s in total.
                #
                # And start late: notes are only written for steps that reach the
                # window, and the verdict needs >=2 samples, so the first read is
                # taken 12s before the window (3 samples by 30s). Sampling from 6s
                # spent a whole-screen read every 6s on steps that finish in 10-20s
                # and never report anything, competing with the step's own reads.
                first = max(6.0, self._watchdog_window() - 12.0)
                if w["stop"].wait(first):
                    return
                while True:
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
                    if w["stop"].wait(6.0):
                        return

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
        side = "consumer" if seg["role"] in _CONSUMER_ROLES else "business"
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

    def _timeline(self, event: str) -> None:
        """Append to the run's Execution Timeline. Only the remote-agent path wrote
        it, so every cross-app run showed "No timeline data available"."""
        import json as _json
        try:
            with SessionLocal() as db:
                run = db.query(TestRun).filter_by(id=self.run_id).first()
                if run is None:
                    return
                try:
                    events = _json.loads(run.timeline) if run.timeline else []
                except ValueError:
                    events = []
                # Local clock: the person reading it is on this Mac.
                events.append({"time": datetime.now().strftime("%H:%M:%S"), "event": event})
                run.timeline = _json.dumps(events)
                db.commit()
        except Exception:                        # display only; never fail a run
            logger.debug("could not write timeline event", exc_info=True)

    # -- Live Steps before the first step -------------------------------------

    def _carry_over(self) -> None:
        """Record the segments a resumed run skips as PASS, with where they passed."""
        start = getattr(self, "_start_at", 0)
        if not start:
            return
        src = (self._resume.get("from_run") or "")[:8]
        carried = self._resume.get("carried") or {}
        for seg in self.flow["segments"][:start]:
            prev = carried.get(str(seg["num"])) or {}
            self._persist(seg, "PASS",
                          [f"[ok] carried over — passed in run {src}; not run again "
                           f"(resumed from segment {self.flow['segments'][start]['num']})"]
                          + list(prev.get("reasons") or []),
                          float(prev.get("launch_time") or 0.0))
        slot = getattr(self, "_booked_slot", "")
        self._timeline(f"Resumed from segment {self.flow['segments'][start]['num']} "
                       f"of run {src}" + (f" (booked slot {slot})" if slot else ""))

    def _queue_segments(self) -> None:
        """Write every segment as 'queued' up front, so Live Steps shows the whole
        flow from the first poll instead of growing one row at a time."""
        for seg in self.flow["segments"]:
            self._persist(seg, "queued", [], 0.0)

    def _setup_stage(self, stage: str) -> None:
        """Close the previous setup stage (with its duration) and show this one
        as running on segment 1."""
        if not hasattr(self, "_setup_notes"):
            return
        prev = getattr(self, "_setup_stage_at", None)
        if prev is not None:
            self._setup_notes.append(f"· setup: {prev[0]} ({time.time() - prev[1]:.1f}s)")
        self._setup_stage_at = (stage, time.time())
        self._show_setup()

    def _setup_log(self, message: str) -> None:
        line = (message.strip().splitlines() or [""])[0][:160]
        if line:
            self._setup_notes.append(f"· {line}")
            self._show_setup()

    def _setup_done(self) -> None:
        if getattr(self, "_setup_stage_at", None) is not None:
            self._setup_stage(":done")          # closes the last stage
            self._setup_stage_at = None

    def _show_setup(self) -> None:
        stage = self._setup_stage_at
        if not self.flow["segments"] or stage is None or stage[0] == ":done":
            return
        try:
            first = self.flow["segments"][min(getattr(self, "_start_at", 0),
                                              len(self.flow["segments"]) - 1)]
            self._persist(first, "running",
                          self._setup_notes + [f"▶ setup: {stage[0]}"],
                          time.time() - stage[1])
        except Exception:                        # progress display must never fail a run
            logger.debug("could not show setup progress", exc_info=True)

    def _preflight(self) -> None:
        """Bring up everything a run needs BEFORE any segment: the target sims booted
        and the Appium server listening. Previously a run just assumed both were up and
        every segment died with 'Connection refused (127.0.0.1:4723)' when they weren't."""
        import os
        import shutil
        import subprocess
        import urllib.request
        self._setup_stage("booting the simulators and Appium")
        # 1. Boot each distinct sim this flow uses (consumer + waiter + kitchen).
        udids = {u for u in (self.devices.get("consumer"), self.devices.get("waiter"),
                             self.devices.get("kitchen"), DEFAULT_CONSUMER_UDID) if u}
        # The participant's iPhone only when this flow has a participant segment:
        # every other flow leaves it shut down (a third idle sim costs the rig).
        if any(seg["role"] == "participant"
               for seg in (getattr(self, "flow", None) or {}).get("segments", [])):
            udids.add(self._participant_udid())
        # Ask once what is already up. Every run used to `simctl boot` every sim
        # (each refused with "already booted", but simctl is slow under load) and
        # relaunch Simulator.app -- 10-18s of setup on runs where nothing was down.
        try:
            _booted = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"],
                                     capture_output=True, text=True, timeout=30).stdout
        except Exception:
            _booted = ""
        _down = [u for u in udids if u not in _booted]
        for udid in _down:
            try:
                subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True,
                               text=True, timeout=60)
            except Exception as e:
                logger.warning("preflight boot %s: %s", udid, e)
        # A crashed launchd_sim ("Unable to boot … launchd_sim may have crashed") is
        # cleared by launching Simulator.app, which reinitialises the subsystem.
        if _down:
            try:
                subprocess.run(["open", "-a", "Simulator"], capture_output=True, timeout=15)
            except Exception:
                pass
        for udid in _down:
            for _ in range(20):
                out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"],
                                     capture_output=True, text=True).stdout
                if udid in out:
                    break
                subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True)
                time.sleep(1.5)
        from automation.projects import simulators as _sims
        for udid in udids:
            _sims.quiet_background_daemons(udid)

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
        for c_role in (r_ for r_ in _CONSUMER_ROLES if r_ in roles):
            required.append(_deploy.AppRequirement(
                role=c_role,
                bundle_id=self.consumer_bundle,
                device_id=self._role_udid(c_role)))
        if roles - set(_CONSUMER_ROLES):
            required.append(_deploy.AppRequirement(
                role="business",
                bundle_id=self.business_bundle,
                device_id=self._business_udid()))
        self._setup_stage("checking the apps are installed")
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
            self._setup_stage("starting the device sessions")
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
        self._setup_stage("starting the device sessions")
        self._prewarm_sessions()

    def _prewarm_sessions(self) -> None:
        """Create (and thus build WDA for) every device this flow uses, in parallel."""
        import concurrent.futures as _fut
        targets = {}   # udid -> (bundle, wda_port, needs_metro)
        for seg in self.flow["segments"]:
            if seg["role"] in _CONSUMER_ROLES:
                # needs_metro for the consumer too: this prewarm launches and
                # attaches to the app, so the Metro location must be set BEFORE it
                # or the session holds a "No bundle URL present" instance.
                targets[self._role_udid(seg["role"])] = (
                    self.consumer_bundle,
                    _PARTICIPANT_WDA_PORT if seg["role"] == "participant" else 8100, True)
            else:  # waiter + kitchen share the business device
                targets[self._business_udid()] = (self.business_bundle, 8101, True)

        def _warm(udid, bundle, wda, needs_metro):
            try:
                if needs_metro and bundle == self.consumer_bundle:
                    if udid not in self._con_metro_ready and ensure_app_metro(udid, bundle):
                        self._con_metro_ready.add(udid)
                elif needs_metro and not self._biz_metro_ready:
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
                    apply_flow_speed_settings(d)
                    d.activate_app(bundle)
                    self._wait_app_ready(d, bundle, udid=udid)
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
        _t_run = time.time()
        try:
            self._timeline("Run started")
            self._queue_segments()
            self._carry_over()
            self._setup_stage("reserving the simulators")
            if not acquire_devices(
                    self.run_id, self._flow_udids(), self._cancel,
                    on_wait=lambda owner, udid: self.on_event({
                        "type": "log",
                        "message": f"waiting for simulator {udid[:8]} — run {owner[:8]} "
                                   f"is using it (one run per device)"})):
                if not self.cancelled:
                    raise RuntimeError(
                        f"simulator still busy with another run after "
                        f"{DEVICE_WAIT_TIMEOUT // 60} min — stop that run and retry")
                for seg in self.flow["segments"]:
                    self._persist(seg, "SKIPPED",
                                  ["[skipped] not run — the run was stopped while "
                                   "waiting for its simulator."], 0.0)
                return
            self._preflight()
            self._setup_done()
            self._timeline(f"Devices ready ({time.time() - _t_run:.0f}s)")
            segments = self.flow["segments"]
            for i, seg in enumerate(segments):
                if i < self._start_at:
                    continue                     # passed in the run this one resumes
                if self.cancelled:
                    for skipped in segments[i:]:
                        self._persist(skipped, "SKIPPED",
                                      ["[skipped] not run — the run was stopped by the user."],
                                      0.0)
                    break
                self._timeline(f"Segment {seg['num']}: {seg['name']}")
                _t_seg = time.time()
                _passed = self._run_segment(seg)
                self._timeline(f"Segment {seg['num']} "
                               f"{'passed' if _passed else 'stopped' if self.cancelled else 'Failed'}"
                               f" ({time.time() - _t_seg:.0f}s)")
                if _passed:
                    continue
                if getattr(self, "_too_early", None):
                    for skipped in segments[i + 1:]:
                        self._persist(skipped, "SKIPPED",
                                      [f"[skipped] waiting — {self._too_early}"], 0.0)
                    self._timeline(f"Stopped: {self._too_early}")
                    break
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
            release_devices(self.run_id)
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
                        if (row.status or "").lower() == "queued":
                            # Written up front for Live Steps, and the run ended
                            # before this segment started: it never ran.
                            row.status = "SKIPPED"
                            if row.consumer_status == "queued":
                                row.consumer_status = "SKIPPED"
                            if row.business_status == "queued":
                                row.business_status = "SKIPPED"
                            row.reasons = ["[skipped] not run — the run ended before "
                                           "this segment started."]
                            continue
                        if (row.status or "").lower() == "running":
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
                    ran = [r for r in rows if r.status != "SKIPPED"]
                    # A crash, or a run that never produced ANY segment result, is a
                    # FAILURE — never report a false 'passed' just because no row said FAIL.
                    if self.cancelled:
                        # The user stopped this run. It is neither a pass nor a
                        # failure, and it must NOT be reported as one: this block
                        # used to overwrite the 'stopped' that the Stop button had
                        # just written, so a stopped run reappeared minutes later
                        # as passed/failed and looked like it had never stopped.
                        run.status = "stopped"
                    elif getattr(self, "_too_early", None):
                        # Waiting for the booking's 30-minute window: stopped, with
                        # the time to come back -- resumable, not a failure.
                        run.status = "stopped"
                        if hasattr(run, "error_message"):
                            run.error_message = f"Waiting: {self._too_early}"
                    # Segment rows now exist from the start (queued), so 'no rows'
                    # became 'no segment actually ran'.
                    elif crashed is not None or not ran:
                        run.status = "failed"
                    else:
                        run.status = "failed" if any(r.status == "FAIL" for r in rows) else "passed"
                    run.job_state = run.status
                    try:
                        import json as _json
                        _events = _json.loads(run.timeline) if run.timeline else []
                    except ValueError:
                        _events = []
                    _events.append({"time": datetime.now().strftime("%H:%M:%S"),
                                    # 'Failed' capitalised: the timeline paints it red.
                                    "event": {"failed": "Run Failed"}.get(
                                        run.status, f"Run {run.status}")
                                             + f" ({time.time() - _t_run:.0f}s)"})
                    run.timeline = _json.dumps(_events)
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
                    elif not ran and hasattr(run, "error_message"):
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


_SLOT_RE = re.compile(r"slot '(\d{1,2}:\d{2})'")
_TICKET_RE = re.compile(r"booking ticket (\d{3,6})")


def resume_plan(flow: Dict[str, Any], rows: List[Any]) -> Dict[str, Any]:
    """Where to resume a run: the first segment that did not PASS.

    `rows` are the old run's ScenarioResult rows. Returns {'start_at', 'booked_slot',
    'carried'}; raises ValueError when there is nothing to resume."""
    by_num = {str(r.scenario_num): r for r in rows}
    segs = flow["segments"]
    start = next((i for i, seg in enumerate(segs)
                  if (getattr(by_num.get(str(seg["num"])), "status", "") or "") != "PASS"), None)
    if start is None:
        raise ValueError("Every segment of this run passed — there is nothing to resume.")
    slot = ticket = ""
    for seg in segs[:start]:
        for line in getattr(by_num.get(str(seg["num"])), "reasons", None) or []:
            m = _SLOT_RE.search(str(line))
            if m:
                slot = m.group(1)
            m = _TICKET_RE.search(str(line))
            if m:
                ticket = m.group(1)
    carried = {str(seg["num"]): {"reasons": list(by_num[str(seg["num"])].reasons or []),
                                 "launch_time": by_num[str(seg["num"])].launch_time}
               for seg in segs[:start]}
    return {"start_at": start, "booked_slot": slot, "booked_ticket": ticket,
            "carried": carried}


def start_flow_run(flow_id: str, env: str = "prod",
                   business_device: str = "tablet",
                   resume: Optional[Dict[str, Any]] = None) -> str:
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
        # Exclude the consumer device THIS run uses (not just the default one):
        # sharing it brings back the WDA "Session does not exist" collisions. The
        # participant's iPhone likewise -- it is booted, so it would be picked first.
        phone_udid = cfgmod.local_udid_for(
            "iPhone", DEFAULT_BUSINESS_PHONE_UDID,
            exclude=[devices.get("consumer") or DEFAULT_CONSUMER_UDID,
                     devices.get("participant") or DEFAULT_PARTICIPANT_UDID]) \
            or DEFAULT_BUSINESS_PHONE_UDID
        devices["waiter"] = phone_udid
        devices["kitchen"] = phone_udid
        # Make sure that dedicated iPhone sim is booted (it's normally shut down).
        try:
            import subprocess as _sp
            _sp.run(["xcrun", "simctl", "boot", phone_udid],
                    capture_output=True, text=True, timeout=60)
        except Exception:
            pass  # already booted -> simctl returns non-zero; the session create will surface real issues
        from automation.projects import simulators as _sims
        _sims.quiet_background_daemons(phone_udid)
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
    runner = FlowRunner(run_id, flow, devices, credentials, env=env, resume=resume)
    threading.Thread(target=runner.run, name=f"flow-{flow_id}-{env}-{run_id[:8]}",
                     daemon=True).start()
    return run_id
