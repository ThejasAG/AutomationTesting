# Full-event scenario — app map (read from source 2026-10-29)

What the 12-phase full-event scenario needs from both apps: screens, REAL ids, the
rules that gate each action, and the client-side money formulas. Read from source
(consumer `repos/ca3da713…`, business `repos/b2233d0c…`, the current checkouts) on
2026-10-29; nothing here was run yet. Verify an id on the device before relying on
it. `A/` = the app's `App/` folder. Corrections to `locator_map.md` are marked ⚠.

## Consumer app (iPhone; host = consumer role, participant = participant role)

### Inviting a real participant (reservation form)
- Adult `counterPlus`/`counterMinus` open **My Contacts** (Reservation.js:1309-1378).
- The sheet lists the PHONE's address book (contacts with a number). `guestAdd` (unnamed
  "Guest N") and `newContactAdd` render only when ≥1 such contact exists.
- `newContactAdd` → New Contact modal: `firstName`, `lastName`, `mobileCont`
  (≥10 digits, country default +91, `countryPick`), `saveContact`, `cancelContact`.
  Save writes the contact into the device address book; it is NOT selected — tap its row.
- Contact row: testID `${given}${family}`(spaces stripped)`+'Invite'` e.g.
  `AnnaSmithInvite`; label `"${given} ${family}"`. Chip `${given||family}cancel`.
- `contactSearch` searches the address book. Own number → toast; duplicate → toast.
- `inviteUsers` ("Invite") is disabled until a chip changed. Before booking nothing is
  sent; `bookAppoitment` posts the invitees' numbers. The server decides registered or not.

### Invitee accepts (participant phone)
- `walletTab` → Upcoming (`upcomingBlock`). Invite card `${hostUsername}InviteCard`,
  inline `${hostUsername}Accept` / `${hostUsername}Decline` (NOT space-stripped).
  Card body → `eventAccept` / `eventDecline` (Modal/index.js:9379).
- After accept, only a **1 hr** booking prompts pre-order (`preOrderBooking`/`orderLater`).
  Otherwise pre-order from the Wallet card `${hostUsername}preOrderCard` (spaces stripped;
  text "Pre-order" before the table is assigned, "Menu" after).

### Menu / product with options (StoreView/Products.js, Cards/Block/RestaurantProduct.js)
- ⚠ `product-item-${id}` / `<name> product` are NOT in the rendered tree.
- Tile: name button `${name}`(stripped) = +1; `${name}Inc` / `${name}Dec` (stripped).
  Going 0→1 opens the options sheet when the product has features/modifiers.
- Options sheet (BottomSheet/BottomUpdate.js): rows `${optName}(stripped)Btn ${price}`
  (literal space, number; variants use `price`, modifiers `additionalPrice`, e.g.
  `LargeBtn 2.5`, `SmallBtn 0`). Variants: one per group, required groups must be set.
  Modifiers: multi-select up to the group max; compulsory groups need min..max.
  Qty in the sheet: `counterMinus`/`counterPlus` (1..stock). Confirm: `confirmProduct`
  ("Add to cart ∙ X €"), a no-op until every required/compulsory group is satisfied.
- `cartImage` ("Checkout (n) ∙ total €") opens the cart.
- Product data: `price` (excl. VAT), `vat` (%), `specialPrice`/`special_price_to`,
  `features[g].items[{name, price}]`, `modifiersData[g].modifiers[{name,
  additionalPrice (GROSS), taxRate}]`. VAT brackets in the system: 3, 14, 17.

### Ordering during the event (table assigned, `visited_restaurant`)
- Wallet card `${host}preOrderCard` ("Menu") → menu → add → `cartImage` → Cart:
  **CONFIRM ORDER** (text only, no id) → "Order Confirmed" `orderConfirmedMenu`.
  No payment at this point (no `cartCheckout`). `cartAddItems` = add more.

### Payment (host pays for others)
- `${host}finishedCard` on the Upcoming card ("Checkout"/"Finished"): enabled only when
  the waiter notified payment (`order.reminder`) and total > 0.
- Pay-for sheet: avatars `${username}pay` (yourself `${username}payYou`; NOT stripped;
  a selected one gets a 2nd element with the same id), `Mepay`, `payTotal`, `Clear`,
  `proceedPayment`. Only unpaid people whose items are all sent are listed.
- Payment sheet: `eApplyCoupons` → LIST of coupons the user OWNS (no code entry):
  `${coupon.price}Coupon` (a % coupon may read `undefinedCoupon`), `couponClose`,
  `couponGetMore` (buy coupons). Tap applies at once, tap again removes.
- Tip: `ePayTip` TextInput, an absolute € amount. ⚠ the app strips commas — type `5.50`.
- `ePayment` (A/Payment/PaymentStripe.js:104) opens the Stripe sheet directly; the
  amount charged = (total + tip). `eCash` / `eFoodVoucher` + `ePaymentConfirm`
  ("Notify Waiter") are the cash / voucher requests.

### Review and invoice
- After payment: "Rate & Earn!" `star1..star5`, `skipReviewCard` ("Skip"), `reviewBack`.
  Review screen (Wallet/Review.js): `star1..5`, aspect chips (text), `writeFeedback`,
  `submitReview`. Finished tab skip: `finishedSkipReview`.
- Invoice: Finished tab card → `finishedViewPdf` ("View PDF") → in-app modal built from
  the card data (no ids; `back`). Fields: Pre-order / On Premises lines, Split, Paid by,
  Total, Discount, EC-Card / Cash / Food Voucher, Balance, Tip, VAT table 3/14/17.
- ⚠ **`finishedViewPdf` renders only when `!data.skipReview`** (CardData.js:2368): a
  diner who SKIPPED the rating has no View PDF. The scenario's "participant skips the
  rating, then opens the invoice" cannot pass as written — report it (app behaviour).

### Login / logout
- `loginEmail`, `loginPassword`, `signIn`. Logout: `menuTab` → `menuLogout`.

## Business app (iPad renders `Components/` + `Screens/`, not the Mobile* tree)

⚠ Corrections: iPad select-all is `selectAll`/`unSelectAll` (text); kitchen items are
`${name}Btn` (stripped), not `itemName-`; `inputVoucher`, `addGuestBtn`,
`remindPayment`, `paymentAmountInput` are phone-only/not found; the `paid*Btn` ids are
the payment tiles on GUEST cards (host/participant cards use `epaymentBtn`,
`cashPaymentBtn`, `foodVoucherBtn`, `discountBtn`).

### ADD NEW ITEM (Event/AddNewItem.js, SelectProductOptions.js, AddNewItemSummary.js)
- `addItemsBtn` (disabled after Notify Payment). Search `addNewItemInput`.
- Product `${name}Item` (stripped). A product with `features`/`modifiersData` opens the
  options pane: radios `${opt}Btn ${price}` (as on the consumer), `applyOptionBtn`.
  The same product + same options again = qty +1; other options = a new line.
- Summary line qty: per-variant `Plus`/`Minus` in the "Custom Selections" panel.
  ⚠ the summary's `${name}Increase/Decrease/Remove` only OPEN that panel.
- `assignToBtn` → AssignProductModal (a real modal; ids reachable): `${user}select` /
  `${user}close` (Guests `Guest1select`), `selectAll`, `addNewGuest`, TWO
  `assignProductsBtn`: LEFT = Assign (≥1), RIGHT = Split (≥2).
- Semantics are SERVER-side: Assign sends the lines + ids (`count:1`); Split sends
  `count = selected`. "Assign qty 5 to 2 guests = 5 each or 5 total": measure on device.

### Order page (OrderSummary left pane, PaymentDetails right pane)
- Rows `${name}card` (radios), text "name x qty" ("/N" when split). No stepper here.
- Before SEND only, in the profile accordion (tap `${user}accordionCard`):
  `${name}Increase`, `${name}Decrease` (count ≥ 2), `${name}Remove` (count < 2).
- `sendItemsBtn`, `serveItemsBtn` (selected prepared rows; a partial serve is fine).
- Swipe a row LEFT → VOID / COMP / SPLIT (text only, no ids):
  - VOID: not prepared, not paid, not assigned (unsent is OK).
  - COMP: served, before Notify Payment, not paid, not pre-order, not already comped.
  - SPLIT: not paid, not comped, not already split (served is OK). Swipe-split shares
    the WHOLE line: qty N among k chosen + owner → N/(k+1) each.
  - Reason dialog (Overlay; tap by text): void `entryError` … ; comp `birthday`,
    `managerDiscretion`, `serviceRecovery`; chips 5/10/15/25/30/40/100 %; Apply.
  - Units dialog (qty > 1; Overlay): counter `incrementQuantity`/`decrementQuantity`,
    1..N, then "Next" (`ApplyBtn` void / `assignProductsBtn` comp).
- Assign/Split AFTER send: long-press `${user}accordionCard` → grid toggle (2nd icon,
  no id) → tick rows → `assignOrSplitBtn` → dialog: Assign = exactly 1 other profile
  (moves the line); Split = ≥2 incl. the owner. Refused after Notify Payment.
  ⚠ in grid mode `addItemsBtn` is the PRINT icon.
- `notifyPaymentBtn`: every line sent AND served. Confirm "Yes" (no id).

### Kitchen (Home/kitchen.js, KitchenCards)
- Item radios `${name}Btn` (stripped, not unique across orders) → `orderReadyBtn`;
  `orderCloseBtn` once all are ready. No select-all.

### Payment (PaymentDetails.js) — expand ONE card at a time (ids repeat per card)
- Methods (host/participant): `epaymentBtn`, `cashPaymentBtn`, `foodVoucherBtn`,
  `discountBtn`; guests: `paidEpaymentBtn`, `paidCashPaymentBtn`, `paidVoucherBtn`,
  `paidDiscountBtn`. Keypad: `VirtualKeyboard-0..9`, display `userAmountInput`,
  `userInputBtn`, `numberPadClose`. Methods can be mixed; amounts add.
- `payForBtn` (current payer only) → `${user}select` → `applyPayment`.
- `tipBtn`: keypad, an absolute € amount.
- `discountBtn` = a COUPON sheet (restaurant coupons for the business app, valid
  today; tiles "{n}% OFFER" / "{n}€ OFFER", no ids; tap applies). Locked once paid.
- Food voucher sheet: counter + "Input" (no ids); rule `userTotal > voucherTotal`.
- `paymentConfirmBtn`: enabled when entered ≥ total (+ paid-for + tip). Overpay = Due.

### Invoices and closing
- Grid-mode print icon (`addItemsBtn`) → `eventInvoice` / `individualInvoice` +
  `${user}select` → "Print Now" (`assignProductsBtn`) → InvoiceModal (text only).
  Event invoice needs payment_completed.
- `closeTableBtn` once payment_completed.

## MEASURED on the simulators (2026-10-29, building flow_full_event)

These override the source reading above where they differ (the installed builds
are not the checked-out source).

- C-App menu renders only the tiles near the viewport (4 of 41 in the tree); every
  section header is there. Use the menu's own SEARCH: drag the list ~450pt, then the
  sticky bar's glyph `` (x347 y45) opens a TextField 'Search for an item'. Its
  results are the same tiles. The clear X is ``.
- A C-App tile is three elements: `<pid>Dec`, its count (a bare number), `<pid>Inc`.
  The floating `cartImage` bar (y≈778-826) covers the bottom tiles; a '+' tapped under
  it opens the Cart.
- The C-App DRINKS tiles do not render (header only), so drinks are added by the waiter.
  The B-App lists "Coca-Cola® Light 1.0l", not "Coca-Cola® 1.0lh".
- `idb ui text` mangles non-ASCII: "Coca-Cola® Light 1.0l" → "Coca-Colaight 1.0l".
  Type only the ASCII head of a name ("Coca-Cola"), then pick the product by its id.
- My Contacts: `contactSearch` is a container (`contactSearch `). Tap it, then
  `idb ui text`. A contact row is a Button named by the contact ("Noolu Naga"), not
  `NooluNagaInvite`. The chip is `Noolucancel`.
- The participant (Noolu Naga, +91 9553477008, username NooluNaga) is in the host
  phone's Contacts (`xcrun simctl addmedia <udid> noolu.vcf`).
- A diner who ACCEPTED an invitation sees events folded (`NylaiKitchen2Card`
  Buttons). Unfold one to see its time; its Pre-order button is
  `${host}preOrderCard` (RoopaDpreOrderCard).
- B-App options pane (installed build): rows are `<opt>CheckBtn` (largeCheckBtn,
  vegCheckBtn) at x≈807. The pane slides in, so a tap during it is lost. The ticked
  state shows only in the pixels: a dark dot 44-48 pt in from the row's left edge.
- B-App order rows: `<pid>card` plus a price beside them (x≈691). No quantity in the
  tree. Assigning a dish to someone who already has that line MERGES it (the price
  rises, no new row). A long order renders only its top rows.
- Money check, Phase 1: host €168.27 and participant €108.50 matched a by-hand
  calculation exactly. unit = round2(price×(1+vat/100)); line = round2(unit×qty)
  + modifiers (gross) × qty + variant price (untaxed) × qty.

## Money (client side; the business app's finalTotal is computed by the SERVER)
- Unit gross = round2(price × (1 + vat/100)); modifiers add their GROSS
  `additionalPrice` (VAT = their `taxRate`, backed out); variant prices are untaxed.
- Line = round2(unit × qty) + round2(Σ mods) × qty + Σ variant prices.
- VAT row b = round2(gross_b × b/(100+b)); Excl = gross − VAT.
- Split share = line / (k+1). Comp n % → the line's compWithVAT (server).
- Consumer coupon: % → total × (1 − pct/100); fixed → total − price; only if base ≥
  `offerAbove`. Tip added on top. Invoice total = Σ brackets + paid-for + tip.
