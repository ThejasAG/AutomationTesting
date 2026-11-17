# Vyapy AI Regression Testing Platform

Automated regression tests for the **Vyapy** restaurant apps, run on iOS simulators.

| Role | App | Simulator |
|---|---|---|
| Consumer (diner: books, pre-orders, pays) | Consumer app | iPhone |
| Waiter (assigns tables, serves, takes payment) | Business app | iPad |
| Kitchen (marks orders ready) | Business app | iPad |

The platform plans tests from GitHub PRs and Jira tickets, then runs recorded and
AI-written flows across the three roles. It repairs broken locators itself and reports
pass/fail with a cause. The AI features run on **Claude Code**.

Before you change anything, read [docs/AGENTS.md](docs/AGENTS.md): it has the architecture and the traps.
[docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) explains every folder and file.

---

## Contents

1. [Requirements](#1-requirements)
2. [Set up a new Mac](#2-set-up-a-new-mac)
3. [Start, check, stop](#3-start-check-stop)
4. [Run a test](#4-run-a-test)
5. [Run the unit tests](#5-run-the-unit-tests)
6. [Move to another machine](#6-move-to-another-machine)
7. [Project layout](#7-project-layout)
8. [Helper scripts](#8-helper-scripts)
9. [When something fails](#9-when-something-fails)

---

## 1. Requirements

- macOS with **Xcode** and an iOS simulator runtime (any iOS 16–26)
- **Python 3.10+**, **Node 18+**, Yarn
- **Appium** with the XCUITest driver, and **idb** (`idb-companion` + `fb-idb`)
- **CocoaPods**, **Redis**
- **Claude Code** CLI, logged in (`claude`). It powers PR planning, the AI chat and
  the AI Agent.

The exact install commands are in [docs/INSTALL.md](docs/INSTALL.md), step 1.

---

## 2. Set up a new Mac

**Step 1. Clone and switch to the working branch**

```bash
git clone https://github.com/ThejasAG/AutomationTesting.git
cd AutomationTesting
git checkout reliability-branch-2
```

**Step 2. Install the Python and dashboard packages**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
(cd automation/dashboard && npm install)
```

**Step 3. Check the machine**

```bash
.venv/bin/python scripts/install.py      # checks the tools are installed
bash scripts/doctor.sh                   # checks everything else and says how to fix each miss
```

**Step 4. Add the settings (never committed)**

```bash
cp config/.env.example config/.env
mkdir -p ~/.vya-platform
```

In `config/.env`, set at least:

```env
DATABASE_URL=sqlite:////Users/<you>/.vya-platform/platform.db   # absolute, OUTSIDE the repo
LLM_PROVIDER_TYPE=claude_code
GITHUB_TOKEN=<token with repo scope>
```

Then copy `config/cross_app_config.json` (the test accounts and simulator ids) from a
teammate. It's gitignored. Send it privately, never through git or chat.

**Step 5. Start the platform** (see [section 3](#3-start-check-stop))

```bash
scripts/start.sh
```

**Step 6. Create the first login** (once per database)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/seed
```

Open <http://localhost:5173> and sign in as `admin` / `admin`.

**Step 7. Load the scenarios**

```bash
.venv/bin/python scripts/platform_seed.py import --scenarios-only
.venv/bin/python scripts/platform_seed.py diff       # confirm nothing is missing
```

**Step 8. Register the apps**

1. In the dashboard, go to **Projects** → **Register Project**.
2. Add the consumer app and the business app, each with its git URL and branch.
3. The platform clones each one into `repos/<project-id>/`.

Building the apps and their Metro bundlers is covered in [docs/INSTALL.md](docs/INSTALL.md), step 6.

---

## 3. Start, check, stop

These are the only commands you need day to day.

| Command | What it does |
|---|---|
| `scripts/start.sh` | Boots the simulators, then starts Appium (:4723), the backend (:8000, restarts itself if it crashes) and the dashboard (:5173). Safe to run again. |
| `scripts/status.sh` | Shows each part as up or down, plus the staging server the live runs depend on. |
| `scripts/stop.sh` | Stops the backend, the dashboard and the execution agent. |
| `scripts/stop.sh --all` | Also stops Appium and the Metros. |

- **Dashboard:** <http://localhost:5173>
- **API docs:** <http://localhost:8000/docs>
- **Logs:** `logs/`

> **After any code change, restart:** `scripts/stop.sh && scripts/start.sh`. The backend does not
> reload on its own. Never kill `logs/backend.pid` directly: that leaves the supervisor
> orphaned.

---

## 4. Run a test

**From the dashboard**

- **Scripts**: the test scripts by platform. `ios/end_to_end_scenario` is the END TO END
  SCENARIO: one block per segment (`[number] name | role`), one step per line. Edit it,
  **Save** (the server checks every step first), then **Run**. `android/` stays empty
  until Android is set up.
- **Scenarios**: run one flow, or Run all.
- **Pull Requests**: pick a PR, and it plans and runs the flows that reach the change.
- **Reports**: open any run. The **Run Details** page shows each segment's calculations and lets you export HTML or PDF.
  A run saves a screenshot only when the app crashed (a red box, or a warning on a white
  screen). Ordinary failures have notes and evidence, but no screenshot.
- **AI Agent**: Claude Code runs the flows and fixes what the automation got wrong. You approve or reject each change.

**From the terminal**

```bash
PYTHONPATH=. .venv/bin/python scripts/dryrun.py flow_book_demo staging
```

Quick demo flows: `flow_book_demo` (consumer), `flow_waiter_demo`, `flow_kitchen_demo`.

> Check the rig is free first: `.venv/bin/python scripts/agent_tools.py busy`.
> Never start a run on top of someone else's.

---

## 5. Run the unit tests

No device or simulator is needed.

```bash
.venv/bin/python -m pytest tests -q
```

---

## 6. Move to another machine

Git carries the code. Copy these separately:

| What | Where | Why |
|---|---|---|
| `.env` | `config/` | logins and keys (GitHub, Jira, business app) |
| `cross_app_config.json` | `config/` | test accounts and simulator ids |
| `platform.db` | `~/.vya-platform/` | run history, scenarios and reports |

`.venv`, `node_modules` and `repos/` are rebuilt on the new machine (steps 2 and 8).

---

## 7. Project layout

```
AutomationTesting/
├── README.md
├── automation/        # the platform
│   ├── api/           #   FastAPI backend (routers in api/v1/routers/)
│   ├── dashboard/     #   React + Vite dashboard
│   ├── scenarios/     #   the flow engine: cross_app_flows.py (FLOWS)
│   ├── intelligence/  #   PR planning, step resolving, self-healing
│   ├── reporting/     #   run reports and calculation tables
│   ├── claude_code/   #   the AI Agent (sessions, gates, approval)
│   ├── knowledge/     #   real element ids, known issues, authoring guide, app graph
│   └── …              #   database, integrations, projects, agentic
├── scripts/           # start.sh · stop.sh · status.sh · install.py + helper tools
├── config/            # .env + cross_app_config.json (secrets, never committed),
│                      # .env.example, ios-support.json, project-environments.json
├── docs/              # INSTALL.md · AGENTS.md · MACHINE_SETUP.md · PROJECT_MAP.md
├── tests/             # unit tests
├── alembic/           # database migrations (alembic.ini beside it)
├── seeds/             # the committed scenarios (scripts/platform_seed.py)
├── vendor/            # archify, which draws the Workflow diagrams
├── logs/ · repos/     # runtime logs and cloned app source (not committed)
└── .claude/           # Claude Code: CLAUDE.md, skills, helper agents
```

---

## 8. Helper scripts

| Script | Use |
|---|---|
| `scripts/dryrun.py <flow> <env>` | Run one flow from the terminal and wait for it to finish |
| `scripts/agent_tools.py --help` | Every platform action: `busy`, `steps`, run info and more |
| `scripts/doctor.sh [repos/<id>]` | Check the machine, or one app checkout |
| `scripts/setup_mac.sh` | One-time setup of a new Mac (Xcode first launch, iOS platform, CocoaPods) |
| `scripts/platform_seed.py import\|diff` | Load or compare the committed scenarios |
| `scripts/clear-screenshots.sh [--keep N \| --days N]` | Free disk space by dropping stored screenshots; pass/fail and reasons stay |
| `scripts/start-agent.sh` | Start the execution agent for queued PR jobs (the dashboard's **Job Queue** button does this) |
| `scripts/claude_nightly.sh` | Start a nightly AI Agent session by hand |

---

## 9. When something fails

1. **Run `bash scripts/doctor.sh`.** Most failures come from the environment, not the code.
2. **Check the staging server.** When it returns 502, no booking can finish:
   ```bash
   curl -o /dev/null -w "%{http_code}\n" https://vya.xorstack.com
   ```
3. **Read [automation/knowledge/known_issues.md](automation/knowledge/known_issues.md)**
   before you investigate a failed run.
4. **Find real element ids** in
   [automation/knowledge/locator_map.md](automation/knowledge/locator_map.md) or in the
   app source under `repos/`. Never guess one.

More detail: [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) (every folder and file, what it is for) ·
[docs/AGENTS.md](docs/AGENTS.md) (architecture and traps) ·
[docs/INSTALL.md](docs/INSTALL.md) (full install) · [docs/MACHINE_SETUP.md](docs/MACHINE_SETUP.md).
