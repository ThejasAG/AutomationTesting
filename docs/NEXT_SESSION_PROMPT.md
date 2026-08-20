# Prompt — continue the scenario-runner performance and reliability work

Paste everything below into a fresh session.

---

Continue work on the Vya automation platform at
`/Users/roops/Desktop/AutomationTestining/AutomationTesting`, branch
`feature/android-bridge-and-platform-fixes`. Everything is uncommitted.

## Operational hazards — read first, these cost hours

1. **`scripts/supervise_backend.sh` respawns the backend.** `pkill` looks like it
   failed and a manually started uvicorn dies on `address already in use`, while
   the *supervisor's* process keeps serving — with whatever code it started with.
   Python holds modules in memory, so a long-lived backend serves **stale code**
   after an edit. Restart with:
   `kill -9 $(lsof -nP -iTCP:8000 -sTCP:LISTEN | awk 'NR==2{print $2}')`, wait ~6s,
   poll `/api/v1/health`. Never start uvicorn by hand.
   Confirm the serving process is newer than your edit before concluding a change
   did not work: `ps -o pid,lstart= -p <pid>`.
2. **The real backend log is `logs/supervisor.log`, not `logs/backend.log`.**
3. **Ask before restarting.** The user runs demos on this same stack. Two demo
   failures were caused purely by restarting mid-run, not by code.
4. **Tokens expire.** Mint one: `python -c "from datetime import*; from jose import jwt; import os; from dotenv import load_dotenv; load_dotenv('.env'); print(jwt.encode({'sub':'admin','exp':datetime.now(timezone.utc)+timedelta(hours=12)}, os.getenv('JWT_SECRET_KEY'), algorithm='HS256'))"`
5. **Two separate engines.** Saved scenarios run through
   `automation/intelligence/scenario_runner.py`. Cross-app flows run through
   `automation/scenarios/cross_app_flows.py`, which *also* calls ScenarioRunner.
   A fix in one does not reach the other; a change to ScenarioRunner affects both.
6. **Long runs need background execution.** A scenario takes minutes. Python
   buffers stdout — use `python -u` or results never reach the file.

## Measured performance facts (do not re-measure)

| call | cost |
|---|---|
| `driver.page_source` | ~12–17s (130–240 KB tree) |
| `idb ui describe-all` | ~1.6–3.4s |
| `_resolve("Home")` | **68.9s**, returns a 0.7-confidence PARTIAL match |
| `_idb_element("Home")` | **1.6s**, exact label match |

## Already fixed and verified — do not redo

- `_page_source` TTL was **1.5s guarding a 17.5s call**, so the cache could never
  hit. Now 60s, relying on `_invalidate_source()` which fires after every action.
- The wait-for-element poll in `_tap_step` now calls `_invalidate_source()` each
  pass. **This is load-bearing**: without it a 60s TTL makes four passes re-read
  one stale tree, turning "wait for late render" into fast-and-wrong.
- `_idb_tap_name` invalidates after tapping (it mutates the screen).
- `_tap_step` order is now `_exact_id` → **exact idb label** → fuzzy `_resolve`.
  An exact `AXLabel` match is *stricter* than the partial match it replaces.
- `_idb_on_screen()` guards idb taps: a raw coordinate tap will NOT scroll a
  target into view the way an Appium click does. Off-screen targets still use
  the resolver.
- `_screen_signature()` uses idb instead of `page_source`.
- Rich report: endpoint requires auth (deliberate); `ReportsPage.tsx` now fetches
  it with credentials instead of `window.open`, which sent no header and 401'd.

Result on a 5-step probe: **335.9s with 3 failures → 120.8s with 0 failures.**

## Ruled out — do not re-investigate

- BOOK NOW being disabled when first tapped. Measured `enabled: True`
  continuously before and for 10s after slot selection.
- BOOK NOW coordinates moving. Stable at `(201, 804)` throughout.
- Missing `.env` in the app repos. The apps hardcode URLs in `App/Config/Api.js`;
  `.env.example` is unused react-native-config boilerplate.
- Groq model being retired. Verify the key/base first:
  `curl -s -o /dev/null -w '%{http_code}\n' https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`
  404 there means key/base is wrong, not the model.

## Tasks, in order

1. **`@book_appointment` needs a retry to succeed.** It logs
   `tap 1 did not open the dialog; retrying` then succeeds on attempt 2.
   Leading UNVERIFIED hypothesis: a LogBox toast lands over BOOK NOW between
   `_clear_logbox()` and the tap — Dismiss sits at y≈850, BOOK NOW at y≈804, and
   this debug build emits warnings continuously. Verify by dumping every idb
   element whose frame contains `(201, 804)` at tap time. Fix the real cause; do
   not just raise the retry count.
2. **`click NylaiKitchen2` still costs ~63.8s** because it falls through to the
   fuzzy resolver — the card's real label is
   `card-container-outer-layer NylaiKitchen2 …`, not an exact match. Make the idb
   path handle a label that *contains* the phrase as a distinct token, keeping it
   unambiguous (reject if more than one element matches).
3. **Point `_visible_labels` (scenario_runner.py, the `_page_source` caller near
   the bottom) at idb**, as `_screen_signature` already does. Remaining
   `page_source` callers: `_fuzzy_resolve`, `_screen_numbers`, `_verify_bill`.
4. **Rename `click book now` → `click bookAppoitment`** in the saved scenarios.
   The BOOK NOW button's accessibility id is `bookAppoitment` — confirmed from a
   screenshot plus the idb tree.
5. **Delete the 10 "Consumer App" scenarios.** They target the production bundle,
   which is installed only on the iPad while the scenarios are pinned to the
   iPhone, and they are near-duplicates of the "Vya Staging connsumer" set. Do
   **not** repoint the project — that buys a second copy of existing coverage.
6. **Run all 19 "Vya Staging connsumer" scenarios and report per-scenario
   pass/fail with timings.** Fix what fails. Note that 12 of them were missing a
   `click Home` step (the app resumes on its last screen, usually the Wallet);
   that has been added already.

## Ground rules

- Verify with a real run before claiming anything works. This codebase's
  recurring defect is steps that report success without checking they achieved
  anything — do not add another.
- State what you measured, not what you expect.
- If a fix regresses something that was passing, say so plainly.
