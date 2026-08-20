# Vya Consumer app — curated facts (AUTHORITATIVE)

These are human-verified domain facts. They OVERRIDE anything the model would guess
and override inferences from code. When a fact here conflicts with the ticket wording,
follow the fact.

<!-- Machine-parsed allowlist of REAL, referenceable elements/sections. Only names on
     this line count toward step validation. Keep it to things that actually exist. -->
REAL_ELEMENTS: Home, Wallet, Coupons, Reservation, Menu, Cart, XP, Book, Cancel, Checkout

## Sections / navigation (real names)
- There is NO section called "Events". A booking is made from a restaurant:
  Home → open a restaurant → book. To "book an event" use the recorded booking flow.
- There is NO section called "Points". Points/XP are displayed inside the **Coupons**
  section (reachable from the Home XP balance). Rewards/coupons live there too.
- Real primary areas: Home, Wallet, Coupons, Reservation, Menu, Cart.

## Terminology
- "Points" has been renamed to **XP**. The FIXED/expected state is: **XP is visible**
  and the old **"Points" label is NOT visible**. Never assert "Points is visible" as a
  pass condition — that is the OLD bug. Assert `verify XP is visible` and
  `verify Points is not visible`.

## XP mechanic (amounts + preconditions)
- **Pre-booking an event earns +20 XP.**
- **Cancelling an event deducts −30 XP.**
- Therefore, to test the cancel deduction you MUST first pre-book an event (which grants
  +20 XP), then cancel it (−30 XP). Assert the change by BALANCE DELTA: read the XP
  balance in the Coupons section before and after, and verify the difference (−30 on
  cancel, +20 on pre-book) — do NOT hardcode a single absolute number.
- The XP balance is shown in the Coupons section; navigating from the Home XP balance
  opens the Coupons section.
