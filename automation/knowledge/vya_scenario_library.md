# Vya scenario library (extracted from Vya-agentic-Bot, verified against the real flow)

> 70 end-to-end Consumer↔Business scenarios with the app's REAL element IDs (same IDs the
> platform's cross_app_flows use). Extracted from `ios_scenarios.py`. The bot's iOS *driver*
> is broken, but these scenario definitions are the salvage value — port them into platform flows.

**Real-flow reconciliation (from live-verified runs):**
- Consumer default books with **Any · 1 Hr** (EB1/EB2 test the Indoor/Outdoor variants on purpose).
- Waiter is **table-first**: select table → add item → assign to guest → send to kitchen.
- Close-out: serve → notify payment → **confirm payment → close** (no 'remind' step).
- XP: **pre-book = +20 XP, cancel = −30 XP**; XP shows in the **Coupons** section (no 'Points'/'Events' section).

## EB — Event Booking (6)

### EB1 — Book Event 1 Guest Indoor
- **Consumer:** tap(wait) NylaiKitchen → tap Indoor → tap Not Sure → tap(wait) chip → tap counterPlus → tap guestAdd → tap inviteUsers → tap(wait) bookAppoitment → tap(opt) orderLater → tap walletTab → tap homeTab
- **Business:** tap(wait) Orders → tap(wait) ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn

### EB2 — Book Event Outdoor - Invitee Accepts
- **Consumer:** tap(wait) NylaiKitchen → tap Outdoor → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap orderLater → tap walletTab → tap(wait) RoopaDInviteCard → tap eventAccept → tap(wait) orderLater → tap orderLater → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap homeTab

### EB3 — Book Event 1 Participant + 1 Guest
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap guestAdd → tap(wait) NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap eventAccept → tap(wait) orderLater → tap orderLater → tap walletTab → tap NylaiKitchenCard → tap walletTab → tap(wait) RoopaDInviteCard → tap NylaiKitchenCard

### EB4 — Book Event - Invitee Declines
- **Consumer:** tap homeTab → tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap orderLater → tap walletTab → tap(wait) RoopaDInviteCard → tap eventDecline → tap walletTab → tap NylaiKitchenCard

### EB5 — Book Event Multiple Invitees + Wallet Filter
- **Consumer:** tap homeTab → tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap AritroInvite → tap SnehaInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap orderLater → tap walletTab → tap(wait) RoopaDInviteCard → tap eventDecline → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap eventAccept → tap orderLater → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap allFilterInvites → tap allUserFilterReject → tap allUserFilterApply → tap clearFilterInvites → tap allFilterInvites → tap allUserFilterAccept → tap allUserFilterApply → tap clearFilterInvites → tap eventAccept → tap orderLater → tap walletTab → tap walletTab → tap NylaiKitchenCard

### EB6 — Pre-Order Bill Validation
- **Consumer:** tap(wait) NylaiKitchen → tap Indoor → tap Not Sure → tap(wait) chip → tap counterPlus → tap guestAdd → tap inviteUsers → tap(wait) bookAppoitment → tap(opt) orderLater → tap(wait) walletTab → tap(wait) Pre-Order → tap applyCoupon → tap(opt) cartImage → tap(opt) cartCheckout → tap(opt) Get More → tap(opt) getCoupons
- **Business:** tap(wait) Orders → tap(wait) ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn

## BME — Business-Managed Events (waiter creates) (5)

### BME1 — B-App Event - User Declines
- **Consumer:** tap walletTab → tap(wait) Nylai KitchenInviteCard → tap eventDecline
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn

### BME2 — B-App Event - User Accepts & Preorders
- **Consumer:** tap walletTab → tap(wait) Nylai KitchenInviteCard → tap eventAccept → tap preOrderBooking → tap starterscategory → tap MuttonSeekhKebabInc → tap 2pcsProduct → tap chutneyProduct → tap confirmProduct → tap(wait) cheesy-comfort-platescategory → tap cheesy-comfort-platescategory → tap Cheese-StuffedGarlicBreadInc → tap 6pccsProduct → tap MarinaraDipProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn

### BME3 — B-App Event - Invite User from Wallet
- **Consumer:** tap walletTab → tap(wait) Nylai KitchenInviteCard → tap eventAccept → tap orderLater → tap NylaiKitchenCard → tap(wait) NooluInvite → tap inviteUsers
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn

### BME4 — B-App Event - Cancel with Invitee
- **Consumer:** tap walletTab → tap(wait) RoopaDInviteCard → tap eventAccept → tap(wait) orderLater → tap orderLater
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn → tap(wait) cancelEvent → tap confirmEventBtn

### BME5 — B-App Event Cancel - No Invitees
- **Consumer:** tap walletTab → tap(wait) Nylai KitchenInviteCard → tap eventAccept → tap orderLater
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn → tap(wait) cancelEvent → tap confirmEventBtn

## CW — Contacts & Wallet (6)

### CW1 — Creating New Contact from Reservation
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap newContactAdd → tap saveContact → tap back → tap back → tap chip-container → tap(wait) bookAppoitment

### CW2 — Adding Guest from Wallet
- **Consumer:** tap(wait) NylaiKitchen → tap Not Sure → tap(wait) chip → tap Book Now → tap(wait) orderLater → tap(wait) NylaiKitchenCard → tap(wait) Invite → tap guestAdd → tap inviteUsers → tap homeTab

### CW3 — Adding User from Wallet
- **Consumer:** tap NylaiKitchen → tap(wait) bookAppoitment → tap orderLater → tap NooluInvite → tap inviteUsers → tap homeTab

### CW4 — Creating New Contact from Wallet
- **Consumer:** tap(wait) bookAppoitment → tap(wait) orderLater → tap(wait) Invite → tap(wait) newContactAdd → tap saveContact → tap back → tap homeTab

### CW5 — B-App Adding Items with 1 Guest
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap Payment Payment → tap(wait) addGuestBtn → tap addGuestBtn → tap Overview Overview → tap addItemsBtn → tap PaneerTikkaItem → tap regularBtn → tap extracheeseBtn → tap applyOptionBtn → tap Pasta & PizzaBtn → tap PizzaQuattroStagioniItem → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap PaneerTikka2regularextracheese PaneerTikka → tap PizzaQuattroStagioni2 PizzaQuattroStagioni → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number6 → tap Number0 → tap Number8 → tap userInputBtn → tap Guest1select → tap Apply → tap tipBtn → tap Number4 → tap userInputBtn → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### CW6 — B-App Adding Items with 2+ Guests
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap guestAdd → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) Payment Payment → tap Payment Payment → tap(wait) addGuestBtn → tap addGuestBtn → tap Guest2Card → tap(wait) addGuestBtn → tap addGuestBtn → tap Guest3Card → tap(wait) addGuestBtn → tap addGuestBtn → tap Guest4Card → tap Overview Overview → tap In ProgressOrderCard → tap addItemsBtn → tap Pasta & PizzaBtn → tap PizzaRucolaeParmigianoItem → tap FreshbellpepperBtn → tap applyOptionBtn → tap Pasta & PizzaBtn → tap PizzaRucolaeParmigianoItem → tap addNewCustomSelection → tap FreshchampignonsBtn → tap applyOptionBtn → tap Pasta & PizzaBtn → tap PizzaRucolaeParmigianoItem → tap addNewCustomSelection → tap CookedhamBtn → tap applyOptionBtn → tap PizzaQuattroStagioniItem → tap applyOptionBtn → tap assignToBtn → tap addNewGuest → tap(wait) selectAll → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap PizzaRucolaeParmigiano6Freshchampignons PizzaRucolaeParmigiano → tap PizzaRucolaeParmigiano6Freshbellpepper PizzaRucolaeParmigiano → tap PizzaRucolaeParmigiano6Cookedham PizzaRucolaeParmigiano → tap PizzaQuattroStagioni6 PizzaQuattroStagioni → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number5 → tap Number0 → tap Number0 → tap userInputBtn → tap Guest2select → tap Guest4select → tap Guest5select → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap Guest3Card → tap paidCashPaymentBtn → tap Number3 → tap Number0 → tap Number0 → tap userInputBtn → tap Guest5select → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

## PO — Pre-Order (7)

### PO1 — Adding More Items from Cart
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) preOrderBooking → tap preOrderBooking → tap Tagliatelle al Salmone → tap Chicken65 → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaRucolaeParmigiano → tap FreshbellpepperProduct → tap confirmProduct

### PO2 — Add and Reduce Items Cart/Menu
- **Consumer:** tap walletTab → tap NylaiKitchenCard → tap preOrderBooking → tap starterscategory → tap MuttonSeekhKebabInc → tap 2pcsProduct → tap confirmProduct → tap cartImage → tap counterMinus → tap addMore → tap confirmProduct

### PO3 — Edit Item from Cart
- **Consumer:** tap walletTab → tap NylaiKitchenCard → tap preOrderBooking → tap starterscategory → tap MuttonSeekhKebabInc → tap 2pcsProduct → tap confirmProduct → tap cartImage → tap cartImage → tap editItemBtn → tap 4pcsProduct → tap confirmProduct

### PO4 — Host Preorders First Invitee Skips
- **Consumer:** tap homeTab → tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap RoopaInvite → tap inviteUsers → tap(wait) bookAppoitment → tap chip-container → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) Chicken65Inc → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap Chicken65Inc → tap addNewCustomSelection → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) NooluNagaInviteCard → tap NooluNagaInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap(wait) HaraBharaKebabInc → tap HaraBharaKebab → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap(wait) Macaroni&CheeseBakeItem → tap cheesy-comfort-platescategory → tap Macaroni&CheeseBakeItem → tap regularBtn → tap AddTruffleOilBtn → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap Macaroni&CheeseBake2regularAddTruffleOil Macaroni&CheeseBake → tap Chicken651addmayomorecrispyy → tap Chicken651addmayolessoil → tap HaraBharaKebab14pcs → tap PizzaProsciuttoeFunghi1Spicysalami → tap PizzaProsciuttoeFunghi2Freshbellpeppermorecheese → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number2 → tap Number0 → tap userInputBtn → tap RoopaDselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PO5 — Both Host and Invitee Preorder
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap VegManchurianInc → tap gravyProduct → tap extrasauceProduct → tap confirmProduct → tap VegManchurianInc → tap addNewCustomSelection → tap gravyProduct → tap extrasauceProduct → tap confirmProduct → tap cheesy-comfort-platescategory → tap FourCheeseLasagnaInc → tap SliceProduct → tap confirmProduct → tap PizzaQuattroStagioniInc → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap(wait) HaraBharaKebabInc → tap HaraBharaKebabInc → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap(wait) Macaroni&CheeseBakeItem → tap Cheesy & Comfort PlatesBtn → tap Macaroni&CheeseBakeItem → tap regularBtn → tap AddTruffleOilBtn → tap applyOptionBtn → tap(wait) selectAll → tap assignToBtn → tap selectAll → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap Macaroni&CheeseBake2regularAddTruffleOil Macaroni&CheeseBake → tap VegManchurian1gravyextrasauceSpicyy → tap VegManchurian1gravyextrasauceMoresauce → tap VegManchurian1gravyextrasauceCrispyy → tap FourCheeseLasagna1SliceGluten-FreePasta → tap FourCheeseLasagna1AddMushroom → tap PizzaQuattroStagioni1Extrasauce → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap RoopaDCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number2 → tap Number0 → tap userInputBtn → tap NooluNagaselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap NooluNagaCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PO6 — Participant Preorders First Host Adds Later
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap(wait) HaraBharaKebabInc → tap HaraBharaKebabInc → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) NylaiKitchenCard → tap NylaiKitchenCard → tap(wait) starterscategory → tap starterscategory → tap MuttonSeekhKebab → tap 2pcsProduct → tap chutneyProduct → tap confirmProduct → tap cartImage → tap applyCoupon → tap 6% OFFER → tap cartCheckout → tap pickUpOrderConfirm
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap(wait) Macaroni&CheeseBakeItem → tap Cheesy & Comfort PlatesBtn → tap Macaroni&CheeseBakeItem → tap regularBtn → tap AddTruffleOilBtn → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap MuttonSeekhKebab12pcschutney MuttonSeekhKebab → tap Macaroni&CheeseBake2regularAddTruffleOil Macaroni&CheeseBake → tap HaraBharaKebab14pcs HaraBharaKebab → tap ThaiGreenCurrywithJasmineRice1vegchickentofuextrap ThaiGreenCurrywithJasmineRice → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number2 → tap Number0 → tap userInputBtn → tap RoopaDselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap NooluNagaCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PO7 — Coupon Verify Total
- **Consumer:** tap(wait) starterscategory → tap MuttonSeekhKebabInc → tap 2pcsProduct → tap chutneyProduct → tap confirmProduct → tap applyCoupon → tap 6% OFFER → tap cartCheckout
- **Business:** tap(wait) Orders → tap(wait) ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap(wait) Cheesy & Comfort PlatesBtn → tap Cheesy & Comfort PlatesBtn → tap Macaroni&CheeseBakeItem → tap regularBtn → tap AddTruffleOilBtn → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap assignProductsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number2 → tap Number0 → tap userInputBtn → tap RoopaDselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) closeTableBtn → tap Overview Overview → tap closeTableBtn

## O — Ordering (modifiers/variants/split) (5)

### O1 — Adding Items with Modifiers & Variants
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap guestAdd → tap guestAdd → tap NooluInvite → tap AritroInvite → tap inviteUsers → tap(wait) chip-container → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap orderLater → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap vegBtn → tap addspicyBtn → tap applyOptionBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap addNewCustomSelection → tap tofuBtn → tap extrapeanutsBtn → tap applyOptionBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap addNewCustomSelection → tap chickenBtn → tap extrasauceBtn → tap applyOptionBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap addNewCustomSelection → tap chickenBtn → tap addmayoBtn → tap applyOptionBtn → tap assignToBtn → tap RoopaDselect → tap Guest1select → tap Guest2select → tap(wait) assignProductsBtn → tap assignProductsBtns → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton

### O2 — Split Before Serve
- **Business:** tap Orders → tap ServeOrderCard → tap(wait) addItemsBtn → tap addItemsBtn → tap PizzaRucolaeParmigianoItem → tap FreshchampignonsBtn → tap applyOptionBtn → tap PizzaRucolaeParmigianoItem → tap addNewCustomSelection → tap CookedhamBtn → tap applyOptionBtn → tap assignToBtn → tap NooluNagaselect → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap ThaiGreenCurrywithJasmineRice1vegaddspicy ThaiGreenCurrywithJasmineRice → tap ThaiGreenCurrywithJasmineRice1tofuextrapeanuts ThaiGreenCurrywithJasmineRice → tap ThaiGreenCurrywithJasmineRice1chickenaddmayoOnions ThaiGreenCurrywithJasmineRice → tap ThaiGreenCurrywithJasmineRice1chickenextrasauceMor ThaiGreenCurrywithJasmineRice → tap PizzaRucolaeParmigiano1Freshchampignons PizzaRucolaeParmigiano → tap PizzaRucolaeParmigiano1Cookedham PizzaRucolaeParmigiano → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard

### O3 — Trying to Split Splitted Items
- **Business:** tap Assign/Split Assign/Split → tap ThaiGreenCurrywithJasmineRicecard → tap sendItemsBtn → tap RoopaDselect → tap Guest1select → tap assignProductsBtns → tap closeModal

### O4 — Assigning Item from 1 to Another
- **Business:** tap arrowBtn → tap NooluNagaselect → tap PizzaRucolaeParmigianocard → tap sendItemsBtn → tap RoopaDselect → tap assignProductsBtns → tap closeModal → tap assignProductsBtns → tap selectAllItemsBtn → tap(wait) selectAllItemsBtn → tap serveItemsBtn → tap(wait) serveItemsBtn

### O5 — Split After Serve with Payment
- **Business:** tap Assign/Split Assign/Split → tap NooluNagaselect → tap PizzaRucolaeParmigianocard → tap sendItemsBtn → tap NooluNagaselect → tap Guest2select → tap Guest1select → tap assignProductsBtns → tap closeModal → tap Overview Overview → tap notifyPaymentBtn → tap Payment Payment → tap RoopaDCard → tap cashPaymentBtn → tap Number7 → tap Number0 → tap userInputBtn → tap Guest1select → tap Guest2select → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number1 → tap Number0 → tap userInputBtn → tap tipBtn → tap Number1 → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap Overview Overview

## EC — Event Cancellation (12)

### EC1 — Host Cancels - Invitee Accepted
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap orderLater → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap eventAccept → tap orderLater → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap cancelBookingMe → tap NooluNagaassign → tap confirmCancelBook → tap optionOne → tap optionSubmit

### EC2 — Adding User After Host Change
- **Consumer:** tap walletTab → tap(wait) NylaiKitchenCard → tap NylaiKitchenCard

### EC3 — Host Cancels - Invitee Declined
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap orderLater → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap eventDecline → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap optionOne → tap optionSubmit

### EC4 — Participant Cancels Event
- **Consumer:** tap homeTab → tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap orderLater → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap eventAccept → tap orderLater → tap walletTab → tap NylaiKitchenCard → tap cancelBookingMe → tap confirmCancelBook

### EC5 — Both Host and Participant Cancel
- **Consumer:** tap walletTab → tap(wait) NylaiKitchenCard → tap NylaiKitchenCard → tap optionOne → tap optionSubmit → tap BOOKING CANCELLED → tap homeTab

### EC6 — Host Cancels After Preorder - Me Only
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) Chicken65Inc → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap HaraBharaKebabInc → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap cancelBookingMe → tap NooluNagaassign → tap confirmCancelBook → tap optionOne → tap optionSubmit → tap BOOKING CANCELLED → tap homeTab

### EC7 — Host Preorders with Guest, Cancels Me Only
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap guestAdd → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) Chicken65Inc → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap optionOne → tap optionSubmit → tap BOOKING CANCELLED → tap homeTab

### EC8 — Host Cancels for All After Preorder
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) Chicken65Inc → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap HaraBharaKebabInc → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap homeTab

### EC9 — Host Cancels for All Before 15 min
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap HaraBharaKebabInc → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap homeTab

### EC10 — Host Cancels Me Only Before 15 min
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) Chicken65Inc → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap preOrderBooking → tap(wait) starterscategory → tap starterscategory → tap HaraBharaKebabInc → tap 4pcsProduct → tap confirmProduct → tap ThaiGreenCurrywithJasmineRiceInc → tap vegProduct → tap extrapeanutsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap walletTab → tap NylaiKitchenCard → tap cancelBookingMe → tap NooluNagaassign → tap confirmCancelBook → tap optionOne → tap optionSubmit → tap BOOKING CANCELLED → tap homeTab

### EC11 — Host with Guest Preorders, Cancels Me Only
- **Consumer:** tap NylaiKitchen → tap counterPlus → tap guestAdd → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) preOrderBooking → tap preOrderBooking → tap(wait) Chicken65Inc → tap Chicken65Inc → tap addmayoProduct → tap confirmProduct → tap pasta-pizzacategory → tap PizzaProsciuttoeFunghiInc → tap FreshbellpepperProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap optionOne → tap optionSubmit → tap BOOKING CANCELLED → tap homeTab

### EC12 — Pickup Cancellation
- **Consumer:** tap NylaiKitchen → tap chip-container → tap(wait) bookAppoitment → tap preOrderBooking → tap starterscategory → tap MuttonSeekhKebabInc → tap 2pcsProduct → tap confirmProduct → tap cartImage → tap cartCheckout → tap pickUpOrderConfirm → tap walletTab → tap NylaiKitchenCard → tap cancelBookingMe → tap confirmCancelBook

## PAY — Payments (7)

### PAY1 — Payment by Cash
- **Consumer:** tap walletTab
- **Business:** tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap userInputBtn → tap RoopaDselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn

### PAY2 — Host Pays for Others
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap Any → tap counterPlus → tap guestAdd → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T8 → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap Pasta & PizzaBtn → tap PizzaRucolaeParmigianoItem → tap FreshbellpepperBtn → tap applyOptionBtn → tap PizzaRucolaeParmigianoItem → tap addNewCustomSelection → tap FreshchampignonsBtn → tap applyOptionBtn → tap PizzaRucolaeParmigianoItem → tap addNewCustomSelection → tap CookedhamBtn → tap applyOptionBtn → tap PizzaQuattroStagioniItem → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap PizzaRucolaeParmigiano2Freshchampignons PizzaRucolaeParmigiano → tap PizzaRucolaeParmigiano2Freshbellpepper PizzaRucolaeParmigiano → tap PizzaQuattroStagioni2 PizzaQuattroStagioni → tap PizzaRucolaeParmigiano2Cookedham PizzaRucolaeParmigiano → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap Orders → tap(wait) ServeOrderCard → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap RoopaDCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number1 → tap Number0 → tap userInputBtn → tap Guest1select → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap tipBtn → tap Number9 → tap Decimal point → tap Number5 → tap userInputBtn → tap cashPaymentBtn → tap Number1 → tap Number5 → tap Number3 → tap Decimal point → tap Number4 → tap userInputBtn → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PAY3 — Payment by E-Payment
- **Consumer:** tap walletTab
- **Business:** tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap ePaymentBtn → tap userInputBtn → tap RoopaDselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn

### PAY4 — Payment by Food Voucher
- **Business:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap Any → tap counterPlus → tap guestAdd → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T8 → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap CrispyThaiSpringRollsItem → tap 6pcsBtn → tap applyOptionBtn → tap Pasta & PizzaBtn → tap PizzaQuattroStagioniItem → tap assignToBtn → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap CrispyThaiSpringRolls26pcs CrispyThaiSpringRolls → tap PizzaQuattroStagioni2 PizzaQuattroStagioni → tap orderReadyBtn → tap orderCloseBtn → tap(wait) Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap RoopaDCard → tap(wait) foodVoucherBtn → tap foodVoucherBtn → tap foodVoucher10CounterIncrement → tap inputVoucher → tap Guest1select → tap Apply → tap cashPaymentBtn → tap Number5 → tap Number0 → tap Number0 → tap userInputBtn → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PAY5 — Payment All 3 Modes
- **Business:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap Any → tap counterPlus → tap guestAdd → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T8 → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap Pasta & PizzaBtn → tap SpaghettiAglioeOlioItem → tap FreshbellpepperBtn → tap applyOptionBtn → tap PizzaQuattroStagioniItem → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap SpaghettiAglioeOlio2Freshbellpepper SpaghettiAglioeOlio → tap PizzaQuattroStagioni2 PizzaQuattroStagioni → tap orderReadyBtn → tap orderCloseBtn → tap(wait) Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap RoopaDCard → tap(wait) tipBtn → tap tipBtn → tap Number1 → tap Number0 → tap userInputBtn → tap cashPaymentBtn → tap Number3 → tap Number0 → tap userInputBtn → tap Guest1select → tap Apply → tap epaymentBtn → tap Number2 → tap Number0 → tap Decimal point → tap Number5 → tap userInputBtn → tap foodVoucherBtn → tap foodVoucher10CounterIncrement → tap inputVoucher → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PAY6 — Participant Pays for Others
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap guestAdd → tap RoopaInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap walletTab → tap(wait) NooluNagaInviteCard → tap NooluNagaInviteCard → tap eventAccept → tap orderLater
- **Business:** tap Orders → tap ReservedOrderCard → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap FishAmritsariItem → tap largeBtn → tap applyOptionBtn → tap PadThaiNoodlesItem → tap prawnBtn → tap addspicyBtn → tap applyOptionBtn → tap Spinach&CheeseRavioliItem → tap regularBtn → tap AddGarlicOilBtn → tap applyOptionBtn → tap(wait) assignToBtn → tap assignToBtn → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap FishAmritsari3largeroastedfish → tap PadThaiNoodles3prawnaddspicy → tap Spinach&CheeseRavioli3regularAddGarlicOil → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap RoopaDCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap Number9 → tap Number8 → tap Number0 → tap Number0 → tap userInputBtn → tap Guest1select → tap NooluNagaselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

### PAY7 — Guest Pays for Others
- **Consumer:** tap(wait) NylaiKitchen → tap NylaiKitchen → tap counterPlus → tap guestAdd → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap orderLater
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap PaneerTikkaItem → tap regularBtn → tap extracheeseBtn → tap applyOptionBtn → tap Pasta & PizzaBtn → tap PizzaQuattroStagioniItem → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap(wait) assignProductsBtn → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap PaneerTikka3regularextracheese PaneerTikka → tap PizzaQuattroStagioni3 PizzaQuattroStagioni → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap Guest1Card → tap(wait) paidCashPaymentBtn → tap paidCashPaymentBtn → tap Number1 → tap Number0 → tap Number0 → tap Number0 → tap userInputBtn → tap RoopaDselect → tap NooluNagaselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn

## SV — Status Verification (lifecycle) (15)

### SV1 — Confirmation Pending Status
- **Consumer:** tap walletTab → tap(wait) Nylai KitchenInviteCard
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn

### SV2 — Reserved Status B-App
- **Business:** tap Home

### SV3 — Event Declination
- **Consumer:** tap walletTab → tap(wait) Nylai KitchenInviteCard → tap eventDecline
- **Business:** tap(wait) addNewEvent → tap anyBtn → tap saveBtn

### SV4 — Pre-Order Status C-App
- **Consumer:** tap NylaiKitchen → tap(wait) chip-container → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap walletTab → tap optionThree → tap optionSubmit → tap BOOKING CANCELLED

### SV6 — Menu Order C-App
- **Consumer:** tap walletTab → tap NylaiKitchenCard → tap(wait) TomYumSoupInc → tap TomYumSoupInc → tap prawnProduct → tap confirmProduct → tap cartImage → tap CONFIRM ORDER → tap orderConfirmedMenu
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap T0AssignAnyBtn → tap AssignTableBtn

### SV7 — Order Status B-App
- **Business:** tap(wait) Orders → tap Orders → tap(wait) OrderOrderCard → tap OrderOrderCard

### SV8 — In Progress Status B-App
- **Business:** tap(wait) addItemsBtn → tap addItemsBtn → tap(wait) Macaroni&CheeseBakeItem → tap Cheesy & Comfort PlatesBtn → tap Macaroni&CheeseBakeItem → tap regularBtn → tap AddTruffleOilBtn → tap applyOptionBtn → tap assignToBtn → tap selectAll → tap assignProductsBtn → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap Macaroni&CheeseBake1regularAddTruffleOil Macaroni&CheeseBake → tap TomYumSoup1vegchickenprawn TomYumSoup → tap orderReadyBtn → tap orderCloseBtn → tap Orders

### SV9 — Serve Status B-App
- **Business:** tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap backButton

### SV10 — Payment Status B-App
- **Business:** tap(wait) Payment Payment

### SV11 — Payment Requested C-App
- **Consumer:** tap(wait) walletTab → tap walletTab → tap(wait) NylaiKitchen2Card PAYMENT REQUESTED → tap NylaiKitchen2Card PAYMENT REQUESTED

### SV12 — Payment Done B-App
- **Business:** tap(wait) Orders → tap Orders → tap PaymentOrderCard → tap(wait) Payment Payment → tap Payment Payment → tap RoopaDCard → tap cashPaymentBtn → tap Number2 → tap Number0 → tap userInputBtn → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) RoopaDCard → tap Overview Overview → tap backButton → tap(wait) PaymentDoneOrderCard → tap PaymentDoneOrderCard → tap closeTableBtn

### SV13 — Completed Status B-App
- **Business:** tap Home → tap(wait) RoopaDCompletedCard

### SV14 — Completed Status C-App
- **Consumer:** tap walletTab → tap(wait) NylaiKitchenCard COMPLETED → tap NylaiKitchenCard COMPLETED

### SV15 — No-Show Status Verification
- **Consumer:** tap homeTab → tap NylaiKitchen → tap counterPlus → tap guestAdd → tap guestAdd → tap NooluInvite → tap inviteUsers → tap chip-container → tap(wait) bookAppoitment → tap bookAppoitment → tap(wait) orderLater → tap orderLater → tap walletTab → tap(wait) RoopaDInviteCard → tap RoopaDInviteCard → tap eventAccept → tap orderLater → tap walletTab
- **Business:** tap(wait) Orders → tap Orders → tap ReservedOrderCard → tap(wait) T0AssignAnyBtn → tap T0AssignAnyBtn → tap AssignTableBtn → tap(wait) addItemsBtn → tap addItemsBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap vegBtn → tap addspicyBtn → tap applyOptionBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap addNewCustomSelection → tap tofuBtn → tap extrapeanutsBtn → tap applyOptionBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap addNewCustomSelection → tap chickenBtn → tap extrasauceBtn → tap applyOptionBtn → tap ThaiGreenCurrywithJasmineRiceItem → tap addNewCustomSelection → tap chickenBtn → tap addmayoBtn → tap applyOptionBtn → tap assignToBtn → tap RoopaDselect → tap Guest1select → tap(wait) assignProductsBtn → tap assignProductsBtns → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap sendItemsBtn → tap(wait) backButton → tap backButton → tap(wait) inProgressOrderCard → tap inProgressOrderCard → tap ThaiGreenCurrywithJasmineRice1vegaddspicy ThaiGreenCurrywithJasmineRice → tap ThaiGreenCurrywithJasmineRice1tofuextrapeanuts ThaiGreenCurrywithJasmineRice → tap ThaiGreenCurrywithJasmineRice1chickenaddmayoOnions ThaiGreenCurrywithJasmineRice → tap ThaiGreenCurrywithJasmineRice1chickenextrasauceMor ThaiGreenCurrywithJasmineRice → tap orderReadyBtn → tap orderCloseBtn → tap Orders → tap ServeOrderCard → tap(wait) selectAllItemsBtn → tap selectAllItemsBtn → tap serveItemsBtn → tap(wait) notifyPaymentBtn → tap notifyPaymentBtn

### SV16 — Payment Done via C-App
- **Consumer:** tap(wait) walletTab → tap walletTab → tap(wait) NylaiKitchenCard PAYMENT REQUESTED → tap NylaiKitchenCard PAYMENT REQUESTED → tap payTotal → tap proceedPayment → tap ePayment → tap walletTab
- **Business:** tap Orders → tap PaymentDoneOrderCard

## FE — Filter Events (4)

### FE1 — Filter Events by Status
- **Business:** tap orderFilterBtn → tap Reserved → tap Confirm → tap Remove filter → tap orderFilterBtn → tap Serve → tap Confirm → tap Remove filter

### FE2 — Filter Events by Table
- **Business:** tap modifyTable → tap T3 → tap Confirm → tap removeTableFilter → tap modifyTable → tap T5 → tap Confirm → tap removeTableFilter

### FE3 — Status Filter in Home Page Popup
- **Business:** tap Home → tap orderFilterBtn → tap Order → tap Confirm → tap Remove filter

### FE4 — Table Filter in Home Page Popup
- **Business:** tap modifyTable → tap T10 → tap Confirm → tap closeEventModal

## PDF — PDF / Receipts (3)

### PDF1 — Generate PDF - Individual Payment
- **Consumer:** tap walletTab
- **Business:** tap Orders → tap ServeOrderCard → tap selectAllItemsBtn → tap serveItemsBtn → tap notifyPaymentBtn → tap Payment Payment → tap NooluNagaCard → tap(wait) cashPaymentBtn → tap cashPaymentBtn → tap userInputBtn → tap RoopaDselect → tap Apply → tap(wait) paymentConfirmBtn → tap paymentConfirmBtn → tap(wait) pdfGenerateBtn → tap pdfGenerateBtn

### PDF2 — Generate PDF for Entire Event
- **Business:** tap Assign/Split Assign/Split → tap(wait) addItemsBtn → tap addItemsBtn → tap eventInvoice → tap(wait) printNow → tap printNow

### PDF3 — Generate PDF after Event Completion (Individual)
- **Business:** tap backButton → tap(wait) addItemsBtn → tap addItemsBtn → tap individualInvoice → tap NooluNagaselect → tap(wait) printNow → tap printNow → tap RoopaDCard → tap backButton → tap Overview Overview → tap(wait) closeTableBtn → tap closeTableBtn
