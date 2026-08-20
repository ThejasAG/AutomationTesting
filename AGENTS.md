# AGENTS.md — Vyapy AI Regression Testing Platform

A guide for an AI agent (or new engineer) to understand this project fast. Read this before
making changes. It captures the architecture **and** the non-obvious, hard-won gotchas that
will otherwise cost you hours.

---

## 1. What this is

An **AI-powered mobile regression-testing platform** for the **Vyapy** restaurant apps. It
drives the real React-Native apps on **iOS simulators** (via Appium + idb), plans what to test
from GitHub PRs / Jira tickets, runs recorded + AI-drafted scenarios, self-heals locators, and
reports pass/fail with RCA. The goal: point it at an app and it *understands the app* and tests
it reliably — grounded in recorded flows + a code knowledge graph + the app source.

**Two apps under test (both React Native, iOS):**
- **Consumer / diner app** — runs on the **iPhone** simulator (`DA24A392-…`), bundle
  `org.vyapy.sarls.vyaconsumerstaging`. The diner books tables, pre-orders, pays.
- **Business app** — runs on the **iPad** simulator (`D19D3EC7-…`), bundle
  `org.vyapy.sarls.vyabusinessipadstaging`. **Two roles share this one app:** the **waiter**
  (books/assigns/serves/pays) and the **kitchen** (marks orders ready). Different login
  accounts, same bundle/device.

So "cross-app" = **three roles**: **consumer** (iPhone), **waiter** + **kitchen** (both iPad).

---

## 2. Run it

```bash
./start.sh     # boots sims → Appium → SUPERVISED backend (:8000) → dashboard (:5173)
./status.sh    # health of everything
./stop.sh      # tear it all down
```
- Backend is **supervised** — it auto-restarts on crash. **Reload backend code with
  `./stop.sh && ./start.sh`**, NOT by killing `backend.pid` directly (that can orphan the
  supervisor). uvicorn does not hot-reload.
- Dashboard: <http://localhost:5173>  · API/docs: <http://localhost:8000/docs>
- Run a flow from the terminal: `PYTHONPATH=. .venv/bin/python scripts/dryrun.py flow_book_demo staging`

---

## 3. Architecture / directory map

- **`automation/api/`** — FastAPI backend. `main.py` loads `.env` (`load_dotenv`) → real Groq
  LLM. Routers in `api/v1/routers/` (pull_requests, scenarios, recorder, reports, tickets,
  webhooks, automation, …).
- **`automation/dashboard/`** — React 18 + Vite + TS UI. Pages in `src/pages/`.
- **`automation/scenarios/cross_app_flows.py`** — the cross-app flow engine (FLOWS dict,
  `FlowRunner`, per-role segments, the `@token` handlers). **The most-edited file.**
- **`automation/scenarios/cross_app_orchestrator.py`** — Appium `_options()` (session caps),
  `_fill_field`, metro helpers, bundle ids.
- **`automation/intelligence/`** — `pr_planner.py` (picks test paths for a PR, grouped by
  role via `_role_of`), `scenario_runner.py` (`ScenarioRunner` — resolves plain-language
  steps against the LIVE tree, self-heals), `impact_selection.py`, `learned_locators.py`.
- **`automation/integrations/`** — `github.py` (list PRs), `jira.py` (`get_issue(key)` reads
  ticket descriptions; configured via `.env` JIRA_URL/EMAIL/TOKEN).
- **`automation/knowledge/`** — grounded facts: `locator_map.md` (REAL element ids per screen,
  extracted from app source — use this instead of guessing), `default.md`, `vya_scenario_library.md`.
- **`automation/database/`** — SQLAlchemy models + SQLite (`test_automation_new.db`). Key
  models: `TestRun`, `ScenarioResult`, `SavedScenario`, `Ticket`, `TestProject`.
- **`repos/<uuid>/`** — cloned app source checkouts (vya-consumer, vya-business, vya_web_app).
  The **real testIDs live here** — grep the source, don't guess selectors.
- **`scripts/`** — `dryrun.py` (run a flow + poll), `supervise_backend.sh`.
- Root: `start.sh` / `stop.sh` / `status.sh`.

---

## 4. Core concepts

- **Flows** (`FLOWS` in cross_app_flows.py): ordered **segments**, each with a `role`
  (consumer/waiter/kitchen) and a list of steps. The runner switches device/app/account per
  segment. Quick demos: `flow_book_demo` (consumer), `flow_waiter_demo`, `flow_kitchen_demo`.
- **Steps**: either `click <id>` / plain-language (resolved by `ScenarioRunner` against the
  live tree, self-healing) or `@token` handlers (special logic — see below).
- **`@token` handlers** (in `FlowRunner`): `@open_reservation`, `@assign_table`,
  `@ensure_order_items` (adds items only if no pre-order), `@kitchen_ready`, `@first_time_slot`,
  `@add_all_products`, `@to_checkout`, `@pay_stripe`, `@pay:<method>`, `@consumer_home`,
  `@hide_keyboard`, `@got_it`. All must be dispatched in `_handle_special`.
- **Recorder** (`api/v1/routers/recorder.py`): mirrors a sim in the browser; each tap is
  hit-tested and saved as a replayable step using the element's **real unique id** (or visible
  text) → a `SavedScenario`. This is the grounded "record once, replay + self-heal" path.
- **PR planning** (`pr_planner.plan_pr_tests`): reads the PR diff, picks the SavedScenarios
  that reach the changed feature by full path, and returns them grouped `by_role`
  (consumer/waiter/kitchen) for cross-app verification.
- **Jira/ticket link** (`pull_requests._attach_tickets`): each PR → its ticket description,
  matched by local ticket (pr_number/key) or a live Jira fetch by the `NEWVYA-###` key in the
  branch/title.

---

## 5. CRITICAL GOTCHAS (read these — they are the whole game)

1. **The apps have a HUGE accessibility tree.** Appium `find_elements` / `page_source` /
   `MATCHES`/`ENDSWITH` predicates traverse the whole tree and can **HANG for minutes**. The
   pattern: use **idb** (`idb ui describe-all` — fast JSON dump ~1-2s) to read the screen and
   **tap by coordinate**, OR targeted `ACCESSIBILITY_ID` lookups. "Map the screen once, resolve
   locally." `_IDB = /usr/local/bin/idb`; the sim udids passed to idb must be the FULL udid.
2. **idb reports CONTENT-space coordinates, not screen positions.** In scroll views (e.g. the
   waiter "My Bookings" timeline) a card can sit at y≈2412 on an 834-tall screen — an idb tap
   can't reach it. To open an off-screen element, use **Appium element `.click()`** (WDA
   auto-scrolls it into view), after DETECTING its label via idb.
3. **The booking card only opens within 30 min of the booking time** (`BookingCard.onPress`:
   navigates only if `now >= start-30min`, else a toast). So book a slot ~5–28 min ahead:
   near enough to open, far enough not to go stale mid-booking.
4. **Sessions die at 60s idle** unless `newCommandTimeout` is set. `_options()` sets it to
   1200s — a pre-warmed session or an idb-heavy step (>60s without an Appium call) would
   otherwise terminate ("A session is either terminated or not started"). Don't remove it.
5. **Per-step timeout**: `STEP_TIMEOUT=150` bounds a wedged step. It uses a manual
   `ThreadPoolExecutor` + `shutdown(wait=False)` — do NOT switch to `with ThreadPoolExecutor()`
   (its `__exit__` blocks on the hung thread and defeats the timeout).
6. **Real ids are in the app source and `knowledge/locator_map.md`, NOT guessable.** Examples:
   table buttons are `T{idx}AssignAnyBtn` (T0 = first free table), confirm is `AssignTableBtn`,
   kitchen items are `itemName-<name>`, `orderReadyBtn`/`orderCloseBtn`. The card label pattern
   is `<UsernameNoSpaces><StatusNoSpaces>Card`. Some screens (table modal historically, Stripe
   card fields, the duration slider) have NO id — match by text or the whole widget.
7. **The restaurant is literally named "NylaiKitchen2"** — a bare `"kitchen"` string match
   mis-tags every consumer scenario. Match the kitchen *flow* (mark-ready ids), not the word.
8. **The book button id is misspelled `bookAppoitment`** (missing an "n"). This is real; keep it.
9. **Staging backend (`vya.xorstack.com`) flaps 502.** When it's 502, bookings can't complete
   (the app stays on the reservation form), so any live flow — and any live demo — fails at
   `bookAppoitment`. This is external; only the server team can fix it. Check with
   `curl -o /dev/null -w "%{http_code}" https://vya.xorstack.com`.
10. **Secrets**: `.env` is gitignored and holds GITHUB_TOKEN, Groq key, business creds, and
    JIRA_TOKEN. The GITHUB_TOKEN has been in git history — it must be rotated (owner-only).
    Never commit `.env` or local staging config.

---

## 6. Common tasks

- **Add a flow**: add an entry to `FLOWS` (id, name, description, `segments=[_seg(...)]`). Every
  `@token` used must be dispatched in `_handle_special` (there's a token-validation check).
- **Fix a broken selector**: find the REAL id in `knowledge/locator_map.md` or grep the app
  source in `repos/<uuid>/App/`. Don't invent ids.
- **Reload after a code change**: `./stop.sh && ./start.sh`.
- **Run a demo**: dashboard "Demo run" button (pinned green run), or Automation →
  `flow_book_demo` / `flow_waiter_demo` / `flow_kitchen_demo`.
- **Golden run** for the demo is pinned in `logs/golden_run.txt` (a passed `flow_book_demo`).

---

## 7. Persistent memory

Cross-session memory for the AI agent lives in
`~/.claude/projects/-Users-roops-…/memory/` (indexed by `MEMORY.md`). It records non-obvious
project facts (metro ports, QR build fixes, staging-backend behavior, the waiter bookings
screen mechanics, the grounded drafter). Check it and keep it current.
