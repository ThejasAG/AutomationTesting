# Automation Roadmap — build ONE logical step at a time

**Principle:** don't automate 150 scenarios at once. Build a **dependency chain** —
each scenario reuses the building blocks below it. Verify each block live before
building the next. Later categories become small *variations* of the core chain.

Legend: ✅ done · 🔎 verifying · ⛔ blocked on input · ⬜ not started · ❌ can't automate

---

## LAYER 0 — Foundations (build + verify FIRST; everything reuses these)
| # | Block | Status |
|---|-------|--------|
| 0.1 | Consumer: **Book an event** (1 person, 1 hr) | ✅ built, ~works |
| 0.2 | Business: **Login as waiter** (email→pass→checkbox→SignIn→verify My Bookings) | 🔎 testIDs confirmed, ⛔ needs valid creds |
| 0.3 | Business: **Login as kitchen** | ⛔ needs creds |
| 0.4 | Consumer: **Login** (if sim logs out) | ⬜ needs creds |

## LAYER 1 — The core cross-app happy path (the "spine")
*Build in this exact order; each step needs the one above it to have run.*
| # | Step | Reuses | Status |
|---|------|--------|--------|
| 1.1 | Business: Book event (waiter) | 0.2 | 🔎 pending 0.2 |
| 1.2 | Business: Assign table | 1.1 | ⬜ |
| 1.3 | Business: Add item to order | 1.2 | ⬜ |
| 1.4 | Business: Send to kitchen | 1.3 | ⬜ |
| 1.5 | Business: Kitchen marks ready | 0.3, 1.4 | ⬜ |
| 1.6 | Business: Serve items | 1.5 | ⬜ |
| 1.7 | Business: Notify payment → pay (cash) → close table | 1.6 | ⬜ |

➡️ **When Layer 1 runs end-to-end, we have the full flow.** Everything below is a variation.

## LAYER 2 — Booking variations (each = Layer-0/1 with one change)
- Book with 1 Guest · with 1 Participant · with Guest+Participant
- Book Indoor · Outdoor · 2 Hours · 3 Hours
- Participant Accepts / Declines
- Modify Event (type, invitees, image, persons −)

## LAYER 3 — Ordering variations (reuse 1.2–1.4)
- Add items with Modifiers / Variants / Special Instructions
- Edit items · Assign item from one person to another
- Split before serve · Split after serve · Give tip

## LAYER 4 — Preorder (Consumer, reuses 0.1)
- Add more items from cart during event · add/reduce/edit in cart
- Host preorders / Invitee preorders (various orders)
- Coupon add + remove + verify total

## LAYER 5 — Payments (reuse the full spine 1.1–1.7)
- Cash / E-payment / Food voucher (+ decimal tip) · check PDF
- Host pays all · Participant/Guest pays for others · Split / mixed payment
- Remind payment + re-checkout · Combined B-App + C-App

## LAYER 6 — Cancellations & status (reuse 0.1 / 1.1)
- Cancel from B-App / C-App · before/after 15 min · Me-only / Cancel-all
- Cancel after table assigned · Pickup cancellation
- Status verification (No-Show, Preorder, Cancelled, Completed, Payment Requested)

## LAYER 7 — Contacts, subscriptions, blocked users, filters, PDFs, complimentary, void
- Create contact (from reservation / wallet) · add guest/participant via wallet
- Subscribe/like/dislike restaurant & product
- Block / unblock user · Filter events (status/table/pickup) · PDF generation
- Complimentary items · Void items (before/after send, over-void error)

---

## ❌ Cannot automate (mark MANUAL — never false-green)
Per your own tags:
- Splitting via **swipe/toggle/modal gestures** (21–24)
- **QR-code** booking · **Location** search/add/current
- **Reviews** (accessibility-ID gaps) · **Search from Home** in C-App
- Payment when Due/Outstanding amount exists

---

## How we proceed (one step at a time)
1. Unblock **0.2 Business waiter login** (need valid creds) → verify it lands on My Bookings.
2. Then **1.1 → 1.7** in order, verifying each live (dump the real screen, fix testIDs, confirm) before the next.
3. Once the spine (Layer 1) is green, Layers 2–7 are quick variations — draft with AI, tune, run.
4. Each finished block is saved + reusable as a ticket **Setup scenario**.

**We are here:** waiting on the waiter credentials to verify 0.2, then we walk the spine 1.1 → 1.7.
