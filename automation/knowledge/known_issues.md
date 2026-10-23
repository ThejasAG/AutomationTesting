# Known issues — read before investigating a failure

What is already understood about the rig, staging and the apps. A failure that
matches one of these is reported as what it is; it is not re-investigated and not
"fixed" in the automation. Add to this file (via a person) when a new one is proven.

## Server / app bugs (report, never work around)

- **Pay-for-all exact E-Payment is ignored by staging** (flow_guests_void_comp_split,
  segment 4). When one profile pays for the others with an E-Payment exactly equal
  to the bill, the server answers 2xx and records nothing (`paidAmount 0,
  payment_completed false` in the C-App's `businessSidePay` log, logs/metro_8084.log).
  Overpaying settles; single-profile exact payments settle. Likely an unrounded float
  sum (24.52+26.26+17.51+17.51 = 85.80000000000001 > 85.80). The flow fails with the
  server-bug message on purpose. Do NOT overpay, re-pay or tap Confirm again.
- A greyed-out **Confirm** on the B-App payment card takes taps silently (no
  accessibility state). Confirm is enabled only when the stored amount ≥ Σ finalTotal;
  check the amount label, not the tap result. "Due" is change owed back.

## Staging environment

- **vya.xorstack.com flaps 502.** Bookings then never complete (the app stays on the
  reservation form) and every live flow fails at `bookAppoitment`. INFRA:
  `preflight --wait 15`, retry once, else report.
- **Shared accounts.** Other people use the same consumer ("Roopa D") and waiter
  ("emp2 A") accounts at NylaiKitchen2. Everyone's earliest bookable slot is the same,
  so same-slot bookings by others are normal; someone else has paid a booking a flow
  was about to pay. A booking with contents the run did not create (wrong party size,
  pre-orders it did not make) is someone else's. The engine picks its own booking by
  ticket (`_booked_ticket`) and the updatedAt window (`_own_panel_card`); keep it so.
- **The booking card opens only within 30 minutes of the booking time**
  (BookingCard.onPress; earlier it shows a toast). Flows book a slot 5–28 min ahead.
  A run stopped with "Waiting: …" hit this window — not a failure.

## The rig

- **Huge accessibility tree.** Appium `find_elements` / `page_source` / MATCHES
  predicates can hang for minutes. Read the screen with idb (`describe-all`, ~1–2 s)
  and tap by coordinate, or use a targeted ACCESSIBILITY_ID lookup. Every Appium call
  in the engine is time-limited (`_bounded_call`); a timed-out call is never followed
  by another on the same session.
- **idb coordinates are content-space.** In scroll views an element can sit at
  y≈2400 on an 834-tall screen; open it with Appium `.click()` (WDA scrolls it in).
- **iPad rotation.** The landscape business app: idb describe-all reports landscape
  coordinates, idb tap takes portrait device coordinates; the direction is measured in
  `scenarios/idb_coords.py`. Route new idb taps through `idb_coords.to_device`.
- **WDA / testmanagerd overload.** When idb describe-all takes 20 s+ and Appium calls
  take a minute, the simulator's test daemons are overloaded: `recover wda`, then
  `preflight`. macOS `lsdiagnose` hogging the CPU makes it worse (nothing to do but wait).
- **Red full-screen box** ("console.error: ViewPropTypes…") = the app's dev JS was
  switched off (`RCT_enableDev=0`). A settings problem, not an app crash; device and
  app settings are changed only by a person.
- **Sessions die after 60 s idle** unless `newCommandTimeout` is set (it is: 1200 s
  in `_options()`). Do not remove it.
- **Per-step timeout** uses a manual ThreadPoolExecutor with `shutdown(wait=False)`;
  never `with ThreadPoolExecutor()` (its exit blocks on the hung thread).

## The apps (real names that look wrong)

- The restaurant is named **NylaiKitchen2** — a bare "kitchen" match mis-tags
  consumer scenarios. Match the kitchen flow by its ids (`orderReadyBtn`,
  `itemName-<name>`), not the word.
- The book button id is misspelled **`bookAppoitment`**. It is real; keep it.
- Card labels are `<UsernameNoSpaces><StatusNoSpaces>Card`; the C-App Checkout is
  `<user>finishedCard` (`<user>consumerCheckout` has no onPress); paid in full shows
  `<user>orderCardCompleted`.
- Overlay dialogs (VOID/COMP reason, swipe-SPLIT, units) are magnus Overlays: idb
  sees one screen-sized element, so their controls are tapped at OCR positions.

## Flow preconditions

- **flow_kitchen_demo** needs an order already waiting in the kitchen queue. In a
  Run all there usually is none: "no queued order has items" is an unmet
  precondition, not a bug.

## Unit tests that fail on purpose today

- `tests/test_consumer_checkout.py::test_taps_checkout_of_the_booked_event_not_another`
  and `::test_refuses_to_guess_between_bookings_without_a_booked_time` — written for
  the label-based checkout before the "open each event" rework; the owner decides.
- `tests/test_fresh_machine_readiness.py::test_the_stale_repo_database_is_gone` —
  a stray empty `test_automation_new.db` in the repo root; a person deletes it.
