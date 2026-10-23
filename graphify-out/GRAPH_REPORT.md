# Graph Report - /Users/roops/AutomationTestining/AutomationTesting/repos/bd34a47c-c099-4d36-ac61-810edfff31ca/App  (2026-07-31)

## Corpus Check
- Large corpus: 484 files · ~585,754 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder.

## Summary
- 2282 nodes · 4283 edges · 160 communities (63 shown, 97 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 199 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- UpcomingCard cluster
- ModalIndex cluster
- index cluster
- Review cluster
- axios-config cluster
- Api cluster
- StoryContainer cluster
- Reservation cluster
- ContactListView cluster
- Home cluster
- Home cluster
- PaymentModel cluster
- index cluster
- index cluster
- Home cluster
- UpcomingCard cluster
- Review cluster
- ExamplesRegistry cluster
- Cart cluster
- Upcoming cluster
- index cluster
- .onClose() cluster
- Product cluster
- PaymentModel cluster
- BottomNavigator cluster
- Favorite cluster
- ProductBlock cluster
- RestaurantContainer cluster
- ContactsView cluster
- AuthRedux cluster
- Store cluster
- Store cluster
- Review cluster
- showToast() cluster
- index cluster
- Store cluster
- index cluster
- Map cluster
- Reservation cluster
- Cart cluster
- index cluster
- Wallet cluster
- showToast() cluster
- ModelVideo cluster
- WishlistRedux cluster
- FinishedCard cluster
- utils cluster
- Invitation cluster
- CouponBs cluster
- Quantity cluster
- Store cluster
- CategoryTabBar cluster
- CustomSlider cluster
- RootContainer cluster
- CartRedux cluster
- Quantity cluster
- StoreBlock cluster
- EditMobileNumber cluster
- Payment cluster
- StoreBlock cluster
- linking cluster
- SearchModal cluster
- index cluster
- emailValidator() cluster
- Calendar cluster
- ProductBlock cluster
- Coupon cluster
- Search cluster
- ColapsibleCard cluster
- .getData() cluster
- Text cluster
- MobileValidationModal cluster
- Quadrilateral cluster
- HomeContainer cluster
- SearchBar cluster
- Filter cluster
- SquareLoader cluster
- App cluster
- GithubRedux cluster
- Info cluster
- Finished cluster
- MobileValidationModal cluster
- confirmPasswordValidator() cluster
- Button cluster
- AccordionView cluster
- index cluster
- BottomNavigator cluster
- Invitation cluster
- HeaderIndexCopy cluster
- FilterModel cluster
- index cluster
- EditProfile cluster
- Coupon cluster
- OtpEmailVerifymodel cluster
- OtpEmailVerifymodel cluster
- PaymentSuccessFull cluster
- AmazingRedux cluster
- UserRedux cluster
- .componentDidMount() cluster
- Coupons cluster
- .componentDidMount() cluster
- ADS cluster
- Avatar cluster
- App cluster
- QRScanner cluster
- ProductInfoModal cluster
- StackNavigator cluster
- SearchRedux cluster
- PayingForOthers cluster
- EditEmail cluster
- EditPassword cluster
- Info3 cluster
- index cluster
- Settings cluster
- LanguageSettings cluster
- ConfirmOrderModel cluster
- Sort cluster
- FavSearchModal cluster
- FavSearchModal cluster
- DeleteAccount cluster
- SettingsPaymentMethods cluster
- Invoice cluster
- Accordion cluster
- ImmutablePersistenceTransfor cluster
- Fonts cluster
- AdsInfo cluster
- AnimatedDropDownButtonModal cluster
- CartPaymentDone cluster
- CreditCardModal cluster
- MinsLeft cluster
- ProductInfoModal cluster
- ReviewDone cluster
- AdsInfo cluster
- AnimatedDropDownButtonModal cluster
- CreditCardModal cluster
- MinsLeft cluster
- PreorderCancelNotification cluster
- ChangeLanguage cluster
- Info2 cluster
- SettingsNotification cluster
- OrderDetails cluster
- EventNoMoreModal cluster
- LocationModalOption cluster
- PickupCancelNotification cluster
- SureToLoosePoints cluster
- BuyCoupon cluster
- RedeemCoupon cluster
- Account cluster
- Refund cluster
- packageon cluster
- AmazingTypes cluster
- GithubTypes cluster
- SearchTypes cluster
- TemperatureTypes cluster
- StartupTypes cluster
- GithubTypes cluster
- AuthTypes cluster

## God Nodes (most connected - your core abstractions)
1. `colors` - 108 edges
2. `UpcomingCard` - 69 edges
3. `Reservation` - 59 edges
4. `instance` - 49 edges
5. `Home` - 37 edges
6. `Home` - 36 edges
7. `Review` - 29 edges
8. `ApplicationStyles` - 28 edges
9. `Product` - 26 edges
10. `Upcoming` - 26 edges

## Surprising Connections (you probably didn't know these)
- `INFO()` --references--> `linking`  [EXTRACTED]
  Screens/StoreView/Store.js → Navigation/Linking.js
- `CategoriesCard()` --indirect_call--> `index`  [INFERRED]
  Components/HomeChips/ScrollableChips.js → Components/CountDown/index.js
- `SegmentedControl()` --indirect_call--> `index`  [INFERRED]
  Components/SegmentControl/index.js → Components/CountDown/index.js
- `Stories()` --indirect_call--> `index`  [INFERRED]
  Components/Story/screens/Stories.js → Components/CountDown/index.js
- `SearchProducts()` --indirect_call--> `index`  [INFERRED]
  Components/TabSectionList/searchList.js → Components/CountDown/index.js

## Import Cycles
- 1-file cycle: `Redux/index.js -> Redux/index.js`
- 3-file cycle: `Components/Modal/index.js -> Components/NewModal/index.js -> Screens/Cart/ColapsibleCard.js -> Components/Modal/index.js`
- 4-file cycle: `Components/Modal/index.js -> Components/NewModal/index.js -> Screens/Cart/ColapsibleCard.js -> Screens/Cart/Quantity.js -> Components/Modal/index.js`
- 4-file cycle: `Components/Modal/index.js -> Components/NewModal/index.js -> Screens/Cart/ColapsibleCard.js -> Screens/Cart/ProductQuantity.js -> Components/Modal/index.js`
- 5-file cycle: `Components/BottomSheet/BottomUpdate.js -> Components/Modal/index.js -> Components/NewModal/index.js -> Screens/Cart/ColapsibleCard.js -> Screens/Cart/Quantity.js -> Components/BottomSheet/BottomUpdate.js`

## Communities (160 total, 97 thin omitted)

### Community 0 - "UpcomingCard cluster"
Cohesion: 0.06
Nodes (3): cartTotal(), showToast(), UpcomingCard

### Community 1 - "ModalIndex cluster"
Cohesion: 0.05
Nodes (20): AlertPrevCart(), AllUserlist(), BuyCoupon, CancelBooking, CancelBookingBottomModal(), EarnPointsToast, FeaturesConformationModel(), LeftEvent (+12 more)

### Community 2 - "index cluster"
Cohesion: 0.09
Nodes (14): Styles, FavContainer, size, getActiveMenu(), SearchProducts(), style, NotificationScreen(), BlockedNumbers() (+6 more)

### Community 3 - "Review cluster"
Cohesion: 0.05
Nodes (17): ColapsibleCard, HEADER_IMAGE_HEIGHT, styles, Test, EarnPointsToast, Styles, Subscribe(), SubscribeToast() (+9 more)

### Community 4 - "axios-config cluster"
Cohesion: 0.09
Nodes (10): instance, FullScreenLogoLoader(), Styles, customAppearance, styles, Info1, CustomPhoneInput(), CustomTextInput() (+2 more)

### Community 5 - "Api cluster"
Cohesion: 0.08
Nodes (16): ProductStoreView(), Styles, ProductInfoCounter(), ProductInfoFeaCounter(), ProductStoreCounter(), Styles, Sort, FeaturesConformationModel() (+8 more)

### Community 6 - "StoryContainer cluster"
Cohesion: 0.06
Nodes (21): NotIntrested, Report, ProgressArray(), styles, ProgressBar(), styles, Readmore, styles (+13 more)

### Community 8 - "ContactListView cluster"
Cohesion: 0.07
Nodes (7): checkRequired(), UpComingDetails, ContactListView, getAvatarInitials(), showToast(), index, SectionList

### Community 9 - "Home cluster"
Cohesion: 0.10
Nodes (4): checkData(), formate(), Home, showToast()

### Community 10 - "Home cluster"
Cohesion: 0.10
Nodes (4): checkData(), formate(), Home, showToast()

### Community 11 - "PaymentModel cluster"
Cohesion: 0.08
Nodes (3): CartPaymentModel, PaymentModel, ViewPdfModal()

### Community 12 - "index cluster"
Cohesion: 0.07
Nodes (18): styles, ListItem, styles, styles, { width }, Styles, {width, height}, AddNewAddress() (+10 more)

### Community 13 - "index cluster"
Cohesion: 0.07
Nodes (15): AlertPrevCart(), AllUserlist(), ProAbModal(), ScanQrFailModal(), styles, MobileValidationStyles, Styles, Timeline (+7 more)

### Community 14 - "Home cluster"
Cohesion: 0.12
Nodes (4): checkData(), formate(), Home, showToast()

### Community 15 - "UpcomingCard cluster"
Cohesion: 0.07
Nodes (12): CancelBooking, CancelBookingBottomModal(), CancelPickupOptionsModal, CancelPreorderOptionsModal, CartPaymentDone, LeftEvent, Reinitialize(), SureToLoosePoints (+4 more)

### Community 17 - "ExamplesRegistry cluster"
Cohesion: 0.10
Nodes (10): AlertMessage, DrawerButton, FullButton, RoundedButton, globalComponentExamplesRegistry, globalPluginExamplesRegistry, renderComponentExample(), renderComponentExamples() (+2 more)

### Community 18 - "Cart cluster"
Cohesion: 0.11
Nodes (4): Cart, getTotal(), showToast(), Paypal

### Community 20 - "index cluster"
Cohesion: 0.10
Nodes (10): Back(), CartIcon(), CartRight(), HomeRight, HomeSearchBar(), Logo(), CategoriesCard(), ScrollableChips (+2 more)

### Community 21 - ".onClose() cluster"
Cohesion: 0.09
Nodes (12): LoaderModel(), MobileValidation(), MoreVis(), NewcontactModal(), NotificationsDeleteModal, PaymentDoneShowWaiter, PickupCancelNotification, PointsDeductionModal (+4 more)

### Community 24 - "BottomNavigator cluster"
Cohesion: 0.11
Nodes (11): UploadImageMenu(), BottomTab, DrawerContent(), MENU, Menu(), MAX_HEIGHT, { width, height }, getExpoRoot() (+3 more)

### Community 28 - "ContactsView cluster"
Cohesion: 0.14
Nodes (4): ContactsView, hasExistingAppointment(), normalizeAppointmentUpdate(), showToast()

### Community 29 - "AuthRedux cluster"
Cohesion: 0.09
Nodes (4): AuthSelectors, INITIAL_STATE, reducer, { Types, Creators }

### Community 31 - "Store cluster"
Cohesion: 0.12
Nodes (4): PRESENTATION(), REVIEWS(), showToast(), Store

### Community 33 - "showToast() cluster"
Cohesion: 0.13
Nodes (6): AssignnHost(), ImageReportReview, Payment(), ReportReview, ReviewImageReport, showToast()

### Community 34 - "index cluster"
Cohesion: 0.15
Nodes (10): AuthTypes, CartTypes, WishlistTypes, getAuth(), userRegister(), addToCart(), fetchCart(), root() (+2 more)

### Community 38 - "Reservation cluster"
Cohesion: 0.13
Nodes (12): ResBkModal(), UploadImgModal(), defaultShadowStyle, getActiveSegmentedBackgroundColor(), getActiveSegmentedTextColor(), getSegmentedBackgroundColor(), getSegmentedTextColor(), SegmentedControl() (+4 more)

### Community 40 - "index cluster"
Cohesion: 0.14
Nodes (5): AcceptInvitaionOrder(), InvitaionScreenModal(), SpamModal(), Tab, globalSocket

### Community 42 - "showToast() cluster"
Cohesion: 0.12
Nodes (6): CancelPickupOptionsModal, CancelPreorderOptionsModal, NotIntrested, PayingForOthers, Report, showToast()

### Community 43 - "ModelVideo cluster"
Cohesion: 0.12
Nodes (3): CarousalItem, ModelVideo, VIST_STORE()

### Community 44 - "WishlistRedux cluster"
Cohesion: 0.11
Nodes (3): INITIAL_STATE, reducer, { Types, Creators }

### Community 46 - "utils cluster"
Cohesion: 0.23
Nodes (7): Register, firstNameValidator(), lastNameValidator(), mobileValidator(), referalCodeValidator(), showReferalBoxValidator(), termsValidator()

### Community 48 - "CouponBs cluster"
Cohesion: 0.20
Nodes (3): CheckDate(), CouponBs, showToast()

### Community 49 - "Quantity cluster"
Cohesion: 0.21
Nodes (10): CustomCounter(), DeleteItemFromCart(), CollapseContent, getTotal(), groupData(), ProductQuantity(), getTotal(), groupData() (+2 more)

### Community 50 - "Store cluster"
Cohesion: 0.14
Nodes (7): Sort(), LocationModalOption, MealTypeFilter(), parseMarkdownText(), SERVICE(), Tab, { width, height }

### Community 52 - "CustomSlider cluster"
Cohesion: 0.16
Nodes (6): CustomMarker, styles, CustomSlider, styles, Item, styles

### Community 53 - "RootContainer cluster"
Cohesion: 0.21
Nodes (6): REDUX_PERSIST, data, reducers, getCurrentRouteName(), screenTracking(), { Types, Creators }

### Community 54 - "CartRedux cluster"
Cohesion: 0.17
Nodes (8): add_products(), firstFeatureOrEmpty(), getCount(), INITIAL_STATE, normalizeModifiers(), reducer, {Types, Creators}, update_cart()

### Community 55 - "Quantity cluster"
Cohesion: 0.21
Nodes (4): getTotal(), groupData(), Quantity, showToast()

### Community 57 - "EditMobileNumber cluster"
Cohesion: 0.19
Nodes (3): countriesCode, EditMobileNumber, showToast()

### Community 58 - "Payment cluster"
Cohesion: 0.26
Nodes (3): AssignnHost(), InvitaionScreenModal(), Payment

### Community 59 - "StoreBlock cluster"
Cohesion: 0.19
Nodes (4): CheckDate(), CouponBlock, Ribbon, Styles

### Community 60 - "linking cluster"
Cohesion: 0.16
Nodes (9): FilterInfo(), config, linking, Drawer, MyTheme, Navigation(), INFO(), hasLocationPermission() (+1 more)

### Community 62 - "index cluster"
Cohesion: 0.14
Nodes (13): Heading, HeadingReg, Para, ParaBold, SemiTiny, Small, SmallBold, Span (+5 more)

### Community 63 - "emailValidator() cluster"
Cohesion: 0.21
Nodes (3): Login, showToast(), emailValidator()

### Community 64 - "Calendar cluster"
Cohesion: 0.26
Nodes (5): Calendar, checkCurrent(), isSelectedMonth(), styles, weekDays

### Community 66 - "Coupon cluster"
Cohesion: 0.18
Nodes (3): Coupon, CouponBlocks, showToast()

### Community 67 - "Search cluster"
Cohesion: 0.15
Nodes (3): INITIAL_STATE, reducer, {Types, Creators}

### Community 70 - "Text cluster"
Cohesion: 0.15
Nodes (12): Heading, HeadingReg, Para, ParaBold, SemiTiny, SmallBold, Span, SubHeading (+4 more)

### Community 77 - "App cluster"
Cohesion: 0.18
Nodes (3): App, store, languageCode

### Community 78 - "GithubRedux cluster"
Cohesion: 0.18
Nodes (4): GithubSelectors, INITIAL_STATE, reducer, { Types, Creators }

### Community 82 - "confirmPasswordValidator() cluster"
Cohesion: 0.31
Nodes (4): Mobile, showToast(), confirmPasswordValidator(), otpValidator()

### Community 83 - "Button cluster"
Cohesion: 0.20
Nodes (4): Button, styles, CardFormScreen, styles

### Community 85 - "index cluster"
Cohesion: 0.20
Nodes (3): ConfirmOrderModel, PreOrderStripePayment(), showToast()

### Community 90 - "index cluster"
Cohesion: 0.22
Nodes (6): FilterContainer(), FiltersContainer, SortContainer(), ViewContainer(), Small, Tiny

### Community 96 - "AmazingRedux cluster"
Cohesion: 0.25
Nodes (4): AmazingSelectors, INITIAL_STATE, reducer, { Types, Creators }

### Community 97 - "UserRedux cluster"
Cohesion: 0.25
Nodes (4): GithubSelectors, INITIAL_STATE, reducer, { Types, Creators }

### Community 98 - ".componentDidMount() cluster"
Cohesion: 0.39
Nodes (3): getTotal(), getTotalForVAT(), getVatData()

### Community 107 - "SearchRedux cluster"
Cohesion: 0.29
Nodes (4): INITIAL_STATE, LIST_DATA, reducer, { Types, Creators }

### Community 112 - "index cluster"
Cohesion: 0.40
Nodes (3): PDFExample, showToast(), styles

### Community 124 - "Fonts cluster"
Cohesion: 0.40
Nodes (4): size, style, type, weight

## Knowledge Gaps
- **142 isolated node(s):** `SELECTED`, `styles`, `styles`, `weekDays`, `Styles` (+137 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **97 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `linking` connect `linking cluster` to `UpcomingCard cluster`, `AdsInfo cluster`, `Map cluster`, `MobileValidationModal cluster`, `Home cluster`, `FinishedCard cluster`, `utils cluster`, `Info cluster`, `MobileValidationModal cluster`, `BottomNavigator cluster`, `BottomNavigator cluster`, `AdsInfo cluster`, `Store cluster`?**
  _High betweenness centrality (0.190) - this node is a cross-community bridge._
- **Why does `colors` connect `index cluster` to `Review cluster`, `axios-config cluster`, `Api cluster`, `StoryContainer cluster`, `index cluster`, `index cluster`, `UpcomingCard cluster`, `Cart cluster`, `index cluster`, `BottomNavigator cluster`, `Reservation cluster`, `index cluster`, `Quantity cluster`, `Store cluster`, `EditMobileNumber cluster`, `StoreBlock cluster`, `linking cluster`, `Text cluster`, `FilterModel cluster`, `index cluster`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Why does `showToast()` connect `showToast() cluster` to `ModalIndex cluster`, `MobileValidationModal cluster`, `PaymentModel cluster`, `Payment cluster`, `OtpEmailVerifymodel cluster`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **What connects `SELECTED`, `styles`, `styles` to the rest of the system?**
  _142 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `UpcomingCard cluster` be split into smaller, more focused modules?**
  _Cohesion score 0.06277665995975855 - nodes in this community are weakly interconnected._
- **Should `ModalIndex cluster` be split into smaller, more focused modules?**
  _Cohesion score 0.04915824915824916 - nodes in this community are weakly interconnected._
- **Should `index cluster` be split into smaller, more focused modules?**
  _Cohesion score 0.08521870286576169 - nodes in this community are weakly interconnected._