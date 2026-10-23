"""The Vyapy test-suite coverage catalog + the app's core workflow (spine).

This is the single source of truth for the ~150-scenario coverage matrix and the
logical build order. The /workflow endpoints turn it into a visual coverage map.

Status per scenario:
  auto    — automatable UI/calc/nav flow (build it as a variation of the spine)
  manual  — cannot be reliably automated (gestures / no testIDs) → never false-green
"""

from __future__ import annotations
from typing import Any, Dict, List

# ── The SPINE: the core CROSS-APP happy path, in two lanes with handoffs ─────
# 'app' places the node in the Consumer or Business lane; handoff edges (below)
# are where one app's action shows up in the other — the whole point of the
# platform. Each node depends on the previous in its lane.
CONSUMER_FLOW: List[Dict[str, str]] = [
    {"id": "c_login",    "label": "Login (consumer)", "app": "consumer"},
    {"id": "c_book",     "label": "Book event",       "app": "consumer"},
    {"id": "c_preorder", "label": "Pre-order items",  "app": "consumer"},
    {"id": "c_wallet",   "label": "Wallet: event",    "app": "consumer"},
    {"id": "c_pay",      "label": "Pay (C-App)",      "app": "consumer"},
    {"id": "c_review",   "label": "Review",           "app": "consumer"},
]
BUSINESS_FLOW: List[Dict[str, str]] = [
    {"id": "b_login",    "label": "Login (waiter)",   "app": "business"},
    {"id": "b_bookings", "label": "My Bookings",      "app": "business"},
    {"id": "b_assign",   "label": "Assign table",     "app": "business"},
    {"id": "b_add",      "label": "Add items",        "app": "business"},
    {"id": "b_send",     "label": "Send to kitchen",  "app": "business"},
    {"id": "k_login",    "label": "Login (kitchen)",  "app": "business"},
    {"id": "k_ready",    "label": "Kitchen: ready",   "app": "business"},
    {"id": "b_serve",    "label": "Serve items",      "app": "business"},
    {"id": "b_notify",   "label": "Notify payment",   "app": "business"},
    {"id": "b_close",    "label": "Close table",      "app": "business"},
]
# The whole spine (both lanes), kept for the "built" status lookup.
SPINE: List[Dict[str, str]] = CONSUMER_FLOW + BUSINESS_FLOW

# CROSS-APP HANDOFFS — where one app's action appears in the other.
CROSS_APP_EDGES: List[Dict[str, str]] = [
    {"source": "c_book",     "target": "b_bookings", "label": "booking appears on waiter's My Bookings"},
    {"source": "c_preorder", "target": "b_add",      "label": "pre-ordered items appear in the order"},
    {"source": "b_notify",   "target": "c_pay",      "label": "payment requested → consumer pays"},
    {"source": "c_pay",      "target": "b_close",    "label": "consumer paid → waiter closes table"},
    {"source": "b_close",    "target": "c_review",   "label": "event complete → review prompt"},
]

# ── The INTERCONNECTED graph (with BRANCHES) — the agent reads this ──────────
# Beyond the linear lanes, real flows branch: after Notify Payment you can pay in
# the C-App OR the B-App (cash/voucher/e-pay); booking can pre-order OR order-later.
# Extra nodes for the branch endpoints (the linear nodes above are reused):
BRANCH_NODES: List[Dict[str, str]] = [
    {"id": "c_order_later", "label": "Order later",      "app": "consumer"},
    {"id": "b_pay_cash",    "label": "Pay: Cash (B-App)","app": "business"},
    {"id": "b_pay_voucher", "label": "Pay: Voucher (B-App)","app": "business"},
    {"id": "b_pay_epay",    "label": "Pay: E-pay (B-App)","app": "business"},
]

# Full directed edge set. kind: flow (same app), handoff (cross-app), branch (a choice).
WF_EDGES: List[Dict[str, str]] = [
    # consumer lane
    {"source": "c_login", "target": "c_book", "kind": "flow"},
    {"source": "c_book", "target": "c_preorder", "kind": "branch", "label": "pre-order now"},
    {"source": "c_book", "target": "c_order_later", "kind": "branch", "label": "order later"},
    {"source": "c_preorder", "target": "c_wallet", "kind": "flow"},
    {"source": "c_order_later", "target": "c_wallet", "kind": "flow"},
    {"source": "c_wallet", "target": "c_pay", "kind": "flow"},
    {"source": "c_pay", "target": "c_review", "kind": "flow"},
    # business lane
    {"source": "b_login", "target": "b_bookings", "kind": "flow"},
    {"source": "b_bookings", "target": "b_assign", "kind": "flow"},
    {"source": "b_assign", "target": "b_add", "kind": "flow"},
    {"source": "b_add", "target": "b_send", "kind": "flow"},
    {"source": "b_send", "target": "k_ready", "kind": "flow"},   # kitchen device readies
    {"source": "k_login", "target": "k_ready", "kind": "flow"},
    {"source": "k_ready", "target": "b_serve", "kind": "flow"},
    {"source": "b_serve", "target": "b_notify", "kind": "flow"},
    # PAYMENT BRANCH — pay in C-App OR in B-App (3 methods)
    {"source": "b_notify", "target": "c_pay", "kind": "branch", "label": "pay in Consumer App"},
    {"source": "b_notify", "target": "b_pay_cash", "kind": "branch", "label": "pay in B-App (cash)"},
    {"source": "b_notify", "target": "b_pay_voucher", "kind": "branch", "label": "pay in B-App (voucher)"},
    {"source": "b_notify", "target": "b_pay_epay", "kind": "branch", "label": "pay in B-App (e-pay)"},
    {"source": "c_pay", "target": "b_close", "kind": "flow"},
    {"source": "b_pay_cash", "target": "b_close", "kind": "flow"},
    {"source": "b_pay_voucher", "target": "b_close", "kind": "flow"},
    {"source": "b_pay_epay", "target": "b_close", "kind": "flow"},
    {"source": "b_close", "target": "c_review", "kind": "handoff", "label": "event complete → review"},
    # cross-app handoffs
    {"source": "c_book", "target": "b_bookings", "kind": "handoff", "label": "booking → waiter"},
    {"source": "c_preorder", "target": "b_add", "kind": "handoff", "label": "pre-order → order"},
]


def all_nodes() -> List[Dict[str, str]]:
    return CONSUMER_FLOW + BUSINESS_FLOW + BRANCH_NODES


def plan_path(goal_id: str) -> List[str]:
    """The agent's brain: BFS the graph to find HOW to reach a goal node.

    Returns the ordered node ids from an entry point to the goal — i.e. exactly
    which building-block flows to run, in order, to recreate that scenario.
    This is what lets the platform read the workflow to auto-compose a scenario.
    """
    from collections import deque
    adj: Dict[str, List[str]] = {}
    for e in WF_EDGES:
        adj.setdefault(e["source"], []).append(e["target"])
    targets = {e["target"] for e in WF_EDGES}
    entries = [n["id"] for n in all_nodes() if n["id"] not in targets]  # roots
    for start in entries:
        seen = {start}
        q = deque([[start]])
        while q:
            path = q.popleft()
            if path[-1] == goal_id:
                return path
            for nxt in adj.get(path[-1], []):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(path + [nxt])
    return []

# ── The full coverage matrix (category -> [(scenario, status)]) ──────────────
# 'auto' = automatable; 'manual' = blocked / cannot be done reliably.
CATALOG: Dict[str, List[Dict[str, str]]] = {
    "Event Booking": [
        {"name": "Book an Event", "status": "auto"},
        {"name": "Book with 1 Guest", "status": "auto"},
        {"name": "Book with 1 Participant", "status": "auto"},
        {"name": "Participant Accepts the Event", "status": "auto"},
        {"name": "Participant Declines the Event", "status": "auto"},
        {"name": "Book with 1 Participant and 1 Guest", "status": "auto"},
        {"name": "Book for 2 Hours → Add items after serve", "status": "auto"},
        {"name": "Book for 3 Hours → Order from menu 2 times", "status": "auto"},
        {"name": "Book with Outdoor", "status": "auto"},
        {"name": "Book with Indoor", "status": "auto"},
        {"name": "Book with Multiple Users + Filter in Wallet", "status": "auto"},
        {"name": "Add Image and Verify Host Notification", "status": "auto"},
    ],
    "Ordering": [
        {"name": "Add items after serve", "status": "auto"},
        {"name": "Add Items w/ Modifiers, Variants & Special Instr (4 users)", "status": "auto"},
        {"name": "Editing items in B-App", "status": "auto"},
        {"name": "Making 1 Person as No Show", "status": "auto"},
        {"name": "Split before serve", "status": "auto"},
        {"name": "Split after serve", "status": "auto"},
        {"name": "Assign split items to another person (verify)", "status": "auto"},
        {"name": "Assign item from one to another", "status": "auto"},
        {"name": "Giving tip", "status": "auto"},
        {"name": "Split / Mixed Payment (3 users + 1 + 1 No-show)", "status": "auto"},
    ],
    "Contacts & Wallet": [
        {"name": "Create a New Contact from Reservation", "status": "auto"},
        {"name": "Create a New Contact from Wallet", "status": "auto"},
        {"name": "Add a Guest through Wallet after event", "status": "auto"},
        {"name": "Add a Participant through Wallet after event", "status": "auto"},
    ],
    "Preorder": [
        {"name": "Add more items from cart during event", "status": "auto"},
        {"name": "Add/reduce items in cart & menu", "status": "auto"},
        {"name": "Edit item from cart (verify correctness)", "status": "auto"},
        {"name": "Both host and invitee preorder", "status": "auto"},
        {"name": "Host preorders first, invitee doesn't (+/- spcl instr)", "status": "auto"},
        {"name": "Host doesn't preorder, participant first (diff options)", "status": "auto"},
        {"name": "Invitee preorders first → host later", "status": "auto"},
        {"name": "Add coupon, remove, re-add, verify total", "status": "auto"},
    ],
    "Status Verification": [
        {"name": "C-App Wallet statuses (No-Show/Preorder/Cancelled/Completed/Payment Req)", "status": "auto"},
        {"name": "B-App Home/Order statuses displayed correctly", "status": "auto"},
        {"name": "Event declination from B-App", "status": "auto"},
        {"name": "Payment done status via C-App payment", "status": "auto"},
        {"name": "No-show status for users", "status": "auto"},
    ],
    "Adding Guests from B-App": [
        {"name": "Add Items in B-App with 1 Guest", "status": "auto"},
        {"name": "Add Items in B-App with 2+ Guests", "status": "auto"},
    ],
    "Payments (C-App)": [
        {"name": "E-payment + decimal tip", "status": "auto"},
        {"name": "Host paying for all + decimal tip", "status": "auto"},
        {"name": "Cash (via B-App) + decimal tip", "status": "auto"},
        {"name": "Food voucher (via B-App) + decimal tip", "status": "auto"},
        {"name": "Remind payment from B-App → re-checkout in C-App", "status": "auto"},
        {"name": "Participant pays for others (whom to pay) + recheckout", "status": "auto"},
        {"name": "Recheckout + decimal tip", "status": "auto"},
    ],
    "Payments-1 (B-App + PDF)": [
        {"name": "Cash + decimal tip (host pays others) + PDF", "status": "auto"},
        {"name": "Food voucher + decimal tip + PDF", "status": "auto"},
        {"name": "E-payment + decimal tip + PDF", "status": "auto"},
        {"name": "Split using all 3 methods + PDF", "status": "auto"},
    ],
    "Payments-2": [
        {"name": "Participant pays for others (large amount)", "status": "auto"},
        {"name": "Guest pays for others", "status": "auto"},
        {"name": "Split with 4 users (1 self + 1 for all)", "status": "auto"},
    ],
    "Manage Events (B-App)": [
        {"name": "User Accepts Invitation", "status": "auto"},
        {"name": "User Declines Event", "status": "auto"},
        {"name": "Create event from B-App + invite from wallet", "status": "auto"},
        {"name": "Cancel event from B-App", "status": "auto"},
        {"name": "Cancel from B-App when invitee present", "status": "auto"},
        {"name": "Cancel from B-App (event created in C-App)", "status": "auto"},
        {"name": "Transfer event waiter→waiter (verify reflection)", "status": "auto"},
    ],
    "Filter Events (B-App)": [
        {"name": "Filter Events by Status", "status": "auto"},
        {"name": "Filter Events by Table", "status": "auto"},
        {"name": "Status filter in Events popup (home)", "status": "auto"},
        {"name": "Table filter in Events popup (home)", "status": "auto"},
        {"name": "Table main filter → Events table filter", "status": "auto"},
        {"name": "Table main filter → Events status filter", "status": "auto"},
        {"name": "Pickup main filter (home)", "status": "auto"},
    ],
    "Filter (C-App)": [
        {"name": "Filter by Top rated (restaurants + products)", "status": "auto"},
        {"name": "Filter by Discount (restaurants + products)", "status": "auto"},
    ],
    "Event Cancellations": [
        {"name": "Host cancels (invitee accepted + guest) Me-Only + reason", "status": "auto"},
        {"name": "Change host during cancellation, verify add user/guest", "status": "auto"},
        {"name": "Host cancels (invitee declined + 2-3 guests)", "status": "auto"},
        {"name": "Participant cancels event", "status": "auto"},
        {"name": "Both host and participant cancel", "status": "auto"},
        {"name": "Host cancels after both preorder, before 15 min, Me-Only", "status": "auto"},
        {"name": "Only host preorders & cancels + guest, before 15 min", "status": "auto"},
        {"name": "Host cancels after both preorder, After 15 min, Me-Only", "status": "auto"},
        {"name": "Only host preorders & cancels, After 15 min", "status": "auto"},
        {"name": "Host cancels after both preorder, before 15 min, Cancel-all", "status": "auto"},
        {"name": "Host cancels after both preorder, After 15 min, Cancel-all", "status": "auto"},
        {"name": "Pickup cancellation", "status": "auto"},
        {"name": "Cancel after table assign - host (On Hold)", "status": "auto"},
        {"name": "Cancel after table assign - user", "status": "auto"},
    ],
    "PDF Generation": [
        {"name": "Individual payment per user", "status": "auto"},
        {"name": "PDF after event (individual)", "status": "auto"},
        {"name": "PDF for entire event", "status": "auto"},
    ],
    "Modify Event": [
        {"name": "Modify event type (re-modify check)", "status": "auto"},
        {"name": "Modify event including invitees", "status": "auto"},
        {"name": "Modify image + message + invite (verify reflection)", "status": "auto"},
        {"name": "Subtract number of persons (click −)", "status": "auto"},
    ],
    "Subscriptions & Ratings": [
        {"name": "Subscribe from more-info (check home + all screens)", "status": "auto"},
        {"name": "Unsubscribe (check targeted ads)", "status": "auto"},
        {"name": "Like a restaurant", "status": "auto"},
        {"name": "Like a product (verify reflection)", "status": "auto"},
        {"name": "Dislike a restaurant", "status": "auto"},
        {"name": "Dislike a product (verify reflection)", "status": "auto"},
    ],
    "Blocked Users": [
        {"name": "Block invited person (verify blocked list + host waiting)", "status": "auto"},
        {"name": "Unblock from blocked list (menu)", "status": "auto"},
        {"name": "Unblock from contact list", "status": "auto"},
    ],
    "C-App Payment-2": [
        {"name": "Coupon application", "status": "auto"},
        {"name": "Split payment between B-App & C-App", "status": "auto"},
    ],
    "Complimentary Items": [
        {"name": "Complimentary 1 item", "status": "auto"},
        {"name": "Complimentary 2 items when 5 present", "status": "auto"},
    ],
    "Void Items": [
        {"name": "Void before sending to kitchen", "status": "auto"},
        {"name": "Void after sending to kitchen", "status": "auto"},
        {"name": "Void 1 item", "status": "auto"},
        {"name": "Void 2-4 items when only 4 (over-void error)", "status": "auto"},
    ],
    "Combined Payment": [
        {"name": "Invitee pays 1 guest (C-App) + rest B-App (coupon+items)", "status": "auto"},
        {"name": "Invitee pays self (C-App) + rest B-App (coupon+no-show)", "status": "auto"},
        {"name": "Invitee pays 1 participant (C-App) + rest B-App (coupon+large)", "status": "auto"},
        {"name": "Host pays 1 guest (C-App) + rest B-App (tip, split)", "status": "auto"},
        {"name": "Host pays self (C-App) + rest B-App (tip, split assign)", "status": "auto"},
        {"name": "Host pays 1 participant (C-App) + rest B-App (tip, modify qty)", "status": "auto"},
    ],
    "Splitting (gestures) — BLOCKED": [
        {"name": "Split via swipe (host/guest, participants, all)", "status": "manual"},
        {"name": "Split via toggle (host/guest, participants, all)", "status": "manual"},
        {"name": "Split via modal before assigning", "status": "manual"},
        {"name": "Split already-split items (error message)", "status": "manual"},
    ],
    "Location — BLOCKED": [
        {"name": "Search / Add / Use current location", "status": "manual"},
    ],
    "Notifications": [
        {"name": "Verify all notifications in Consumer App", "status": "auto"},
    ],
    "History & QR — BLOCKED": [
        {"name": "Filter history cards by dates", "status": "auto"},
        {"name": "Book via QR (table available / not) — Cannot be done", "status": "manual"},
        {"name": "Payment when Due/Outstanding exists — Cannot be done", "status": "manual"},
        {"name": "Search items from Home (C-App) — Cannot be done", "status": "manual"},
    ],
    "Reviews — BLOCKED": [
        {"name": "Reviews Breakfast/Lunch/Dinner, Edit, Delete (no testIDs)", "status": "manual"},
    ],
}


def summary() -> Dict[str, Any]:
    """Totals for the coverage matrix."""
    total = auto = manual = 0
    for items in CATALOG.values():
        for it in items:
            total += 1
            if it["status"] == "manual":
                manual += 1
            else:
                auto += 1
    return {"total": total, "automatable": auto, "manual_blocked": manual,
            "categories": len(CATALOG)}
