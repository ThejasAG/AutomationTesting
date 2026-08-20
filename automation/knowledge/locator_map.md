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
