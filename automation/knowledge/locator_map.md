# Grounded Locator Map — Vya Consumer + Business apps

Real element identifiers extracted from the app source (not guessed). In React Native,
Appium matches `testID` and `accessibilityLabel` (both surface as accessibility id / `~name`).
Template patterns use `<Var>` with the exact JS shown. Keep this in sync with the source; it
is the grounding the flows + AI drafter should resolve against instead of guessing.

Source roots:
- BUSINESS = `repos/1519bec5-…/App` (vya-business.git)
- CONSUMER = `repos/bd34a47c-…/App` (vya-consumer.git)

---

## BUSINESS / WAITER / KITCHEN

### Login (shared by waiter + kitchen — role resolved server-side)  SignIn.js
`emailValue`, `passwordValue`, `signInBtn`, `clickCheckBox` (T&C, login disabled until checked),
`forgotPassword`. (All matched by **accessibilityLabel** — InputField maps accessName→label only.)

### My Bookings (Home/index.js)
`allBtn`, `tableBtn`, `pickupBtn` (filter tabs), `qrScaner`, `addNewEvent`.
Date selector: accessibilityLabel = the date string value (dynamic).

### Booking card (BookingCard/index.js)
- id/label pattern: `` `${username.replace(/\s+/g,'')}${status.replace(/\s+/g,'')}Card` `` →
  `<UsernameNoSpaces><StatusNoSpaces>Card` (e.g. `RoopaDReservedCard`). Statuses: Reserved, Serve,
  In Progress, Order, Completed, Cancelled, Expired.
- onPress → navigates to `EventInformations` ONLY when `now(UTC) >= from_time − 30min`; else a toast.
- Card is **disabled** (untappable) when expired/left/cancelled/no-show/business-created.
- "⋮" menu button: **NO id** → `handleEventActionClicked` (opens EventAction modal).

### Order Summary / EventInformations (Event/OrderSummary.js)
`selectAllItemsBtn` (Select All), `unSelectItemsBtn`, `sendItemsBtn` (SEND to kitchen),
`serveItemsBtn` (Serve), `notifyPaymentBtn` (Notify Payment), `closeTableBtn` (Close Table),
`addItemsBtn` (ADD items), `closePickupBtn`.
Per-product radio: `` `${product.name}card`.replace(/\s+/g,'') `` → `<ProductNameNoSpaces>card`.
Table-assign chip (opens the table modal): **NO id** — visible text = table name — onPress `onModifyTableClick`.
Use the `modifyTable` id on the modal path instead.

### Select-a-table / Modify-Table modal (Modal/EventTableSelect/index.js)
- Table buttons: `` `T${idx}AssignAnyBtn` `` → **`T0AssignAnyBtn`, `T1AssignAnyBtn`, …** (idx = position
  in the filtered list of UNBOOKED tables; visible text = table name). **`T0AssignAnyBtn` = first free table.**
- Confirm button: **`AssignTableBtn`**.
- Auto-shows when a reservation with a room loads; also reachable via `modifyTable`.

### Kitchen (Orders/KitchenOrder.js)  — tabs in Home/kitchen.js: `kitchenAllBtn`/`kitchenTableBtn`/`kitchenPickupBtn`
- Order card: `inProgressOrderCard` / `completedOrderCard` / `orderCard` (by state).
- Per-item SELECT (Ready is disabled until items selected): tap the item — id `` `itemName-${name.replace(/\s+/g,'')}` ``
  (testID) or accessibilityLabel `<ProductNameNoSpaces>`; both fire `Select(order,item)`.
- `orderReadyBtn` (Ready — mark selected prepared), `orderCloseBtn` (Close Order — replaces Ready once all served).

### Payment (Event/PaymentDetails.js)
Method: `epaymentBtn`, `cashPaymentBtn`, `foodVoucherBtn`, `discountBtn`, `tipBtn`, `payForBtn`.
Amount pad: `paymentAmountInput` (testID) → `userInputBtn` (confirm amount); `numberPadDismiss`/`numberPadClose`.
Confirm: `paymentConfirmBtn`. Already-paid view: `paidEpaymentBtn`/`paidCashPaymentBtn`/`paidVoucherBtn`.
Per-diner accordion: `` `${username}accordionCard`.replace(/\s+/g,'') ``. Voucher code: `inputVoucher` (ApplyFoodVoucher.js).
`remindPayment`, `addGuestBtn`.

> **Overlay trap (measured 2026-10-01):** the VOID/COMP reason dialog, the swipe-SPLIT dialog
> and the "which units" dialog are magnus `Overlay`s — accessibility (idb AND Appium) sees ONE
> screen-sized element labelled with all the ids below joined by spaces. Their ids cannot be
> tapped; use the `@void_item` / `@comp_item` / `@split_item` handlers (they tap by OCR text).

### Order row swipe: VOID / COMP / SPLIT (Event/OrderSummary.js:757, RNGH `Swipeable`)
Swipe a row LEFT (start on its price, not its radio). The three buttons have **NO id**, only
their text `VOID` / `COMP` / `SPLIT`; while a row is closed they sit 10000pt off-screen.
Rows are `<ProductNameNoSpaces>card` and are NOT unique (one dish for four people = four rows).
- VOID: only before the item is prepared. COMP: only after Serve and before Notify Payment.
  SPLIT: not on a comped or already-split line.
- VOID / COMP reason dialog (Modal/index.js:5742): **nothing preselected**. Void reasons
  `entryError`, `customerChangedMind`, `itemUnavailable`, `duplicateOrder`,
  `allergyDietaryConcern`, `managerOverride`; comp reasons `birthday`, `managerDiscretion`,
  `serviceRecovery`; comp discount chips `5Btn` `10Btn` `15Btn` `25Btn` `30Btn` `40Btn` `100Btn`
  (comp needs a reason AND a discount). Notes `voidOtherReasonInput` / `compOtherReasonInput`.
  Apply = **`assignProductsBtn`**. Quantity > 1 adds a units dialog: `ApplyBtn` (void) /
  `assignProductsBtn` (comp).
- SPLIT dialog (AssignSplitProductModal): the row's owner is hidden; profiles
  `` `${name}select` `` (spaces stripped: `RoopaDselect`, `Guest1select`), selected ones show
  `` `${name}close` ``; Apply = `assignProductsBtn`.

### ADD NEW ITEM → Assign / Split (Event/AddNewItem.js, Modal/index.js:1423)
`addItemsBtn` opens it; products `` `${name}Item` `` (spaces stripped); a dish with options
needs `applyOptionBtn`; **`assignToBtn`** (ASSIGN / SPLIT) opens "Assign to or split among…":
profiles `<Name>select` / `<Name>close`, `selectAll`, `addNewGuest`, and TWO buttons both
`assignProductsBtn` — the LEFT is Assign (≥1 selected, one of the dish per person), the RIGHT is
Split (≥2). Assign/Split is the commit; there is no separate add button.

### Notify Payment / Pay For (OrderSummary.js:1312, PaymentDetails.js)
`notifyPaymentBtn` (also REMIND PAYMENT) → confirm dialog "Yes" / "No" (**no ids**, text only).
Profile card: `<Name>accordionCard` (on the name text). **`payForBtn` is disabled until that
profile is the current payer** — pressing its `epaymentBtn` (then `numberPadClose`) makes it so.
Pay For dialog: payer preselected, profiles `<Name>select` / `<Name>close`, Apply = `applyPayment`.
Amount pad (tablet): `userAmountInput` (display), digit keys `VirtualKeyboard-0..9`, `userInputBtn`.

### Booking action "⋮" modal (Modal/EventAction/index.js)
`cancelEvent`, `transferBtn`, `` `${username}Btn` ``, `confirmEventBtn`.

### Add-new-event modal (AddNewEventModal/index.js) — waiter-created bookings
`addNewEvent` opens it. `firstName`, `lastName`, dining `anyBtn`/`indoorBtn`/`outdoorBtn`,
`leftDateBtn`/`rightDateBtn`, `mobileInputBtn`, `saveBtn`, `closeEventModal`.

---

## CONSUMER / DINER

### Login (Login/Login.js)
`loginEmail`, `loginPassword`, `signIn`, `signUp`, `forgetLoginPassword`.

### Restaurant card (Cards/Block/StoreBlock.js)
Card image id = `<StoreNameNoSpaces>` (`store.name.replace(/\s+/g,'')`) → navigates to StoreReservation.
`<StoreName>Fav` / `<StoreName>Sub` / `<StoreName>Unsub`.

### Reservation (StoreView/Reservation.js)
Dining area radios: `Any` / `Indoor` / `Outdoor`. Date: `previousDate` / `tommorowDate`.
Time-slot chips: accessibilityLabel = the slot string, e.g. `"18:30"` (`_handleTime(el)`).
Book button: **`bookAppoitment`** (note the misspelling). Dynamic: `modifyReservation` when editing,
`browseMenu` on the menu tab. `MessageInput`, `addImageButton`.
**Duration** is a DRAG slider (`CustomSlider`) — **no tappable id**; `durationIndexChange(0..3)` →
'1 hr'/'2 hr'/'3 hr'/'Not Sure'. Can't select by id — needs a swipe/drag or leave default.
**Persons steppers** (Components/CustomCounter.js): `counterMinus` / `counterPlus` — the adult
and child steppers share these labels (adult = the LEFT pair). The adult +/− does not count by
itself: it opens **My Contacts** (Components/Contacts): `guestAdd` (each tap adds an unnamed
"Guest N", chip `Guest Ncancel`), `newContactAdd`, `contactSearch`, phone contacts by name,
**`inviteUsers`** (Invite → closes; Persons = 1 + invitees). The Guest row renders only when the
phone has ≥1 contact with a number and the app can read contacts (iOS: no explicit request).
After BOOK NOW only a **1 hr** booking shows the pre-order dialog; Not Sure / 2 / 3 hrs go to Wallet.

### Booking-Confirmed modal (Components/Modal/index.js)
`preOrderBooking` (YES, PRE-ORDER → menu), `orderLater` (ORDER LATER → wallet),
`preOrderYes`/`preOrderLater` (secondary prompt), `later` (abandon confirm).
`appointmentId` — transparent Text whose value = the new booking id (grab it in tests).

### Menu + product (StoreView/Products.js, Product.js)
Menu tile: testID `` `product-item-${productId}` `` (label `<name> product`). Category tab:
`` `category-tab-${categoryId}` ``. Containers: `products-list`, `category-list`, `cartImage`.
Product detail: `<ProductName>Add` (ADD/start qty), `<ProductName>Submit` (commit to cart),
`<ProductName>SplIns`, `<ProductName>Close`.
Steppers (CustomCounter.js): `counterMinus`/`counterPlus`; per-product `<name>Dec`/`<name>Inc`.

### Cart / checkout (Screens/Cart/index.js)
`cartCheckout` (active button lives in Payment/PreOrderStripe.js:110 — the inline one is commented out),
`cartAddItems`, `applyCoupon`, `toggleBtn`, `<ProductName>finishedCard`.

### Payment (Components/Modal/index.js payment sheet, Payment/*.js)
Card: `credit-card-input` (whole widget — Stripe subfields have NO per-field id), `cardPay` (submit), `paymentClose`.
E-payment/split: `ePayment`, `eCash`, `eFoodVoucher`, `eApplyCoupons`, `ePayTip`, `ePaymentConfirm`,
`payTotal`, `proceedPayment`, `Mepay`. Add method: `addNewPayment`/`addNewCreditCard`/`paymentConfirm`.

### Wallet (Screens/Wallet/index.js)
`walletBackBtn`, `walletSearchIcon`. Booking card: `<RestaurantNameNoSpaces>Card`.
`finishedRebook`, `finishedViewPdf`, `finishedCardclose`.

---

## Elements with NO id (must use visible text or are un-targetable)
BookingCard "⋮" menu; OrderSummary table-assign chip (use `modifyTable`/`T{idx}AssignAnyBtn` instead);
Kitchen "Print Ticket"; consumer **duration slider** (drag-only); individual Stripe card subfields
(only `credit-card-input`). Consumer book id is misspelled **`bookAppoitment`**.
