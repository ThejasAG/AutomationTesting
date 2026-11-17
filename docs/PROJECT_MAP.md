# Project map

Every folder and file in this repo: what it is for, and what uses it. Generated on 2026-11-17 from each file's own
header and its real users (imports and calls), then completed by hand. When you add a file, add its row.

**Used by** lists up to three real users outside tests. `—` means it is an entry point (a command, a page,
a config file read by a tool) or used only inside its own folder.

Not committed, so not listed: `logs/` (runtime logs), `repos/` (cloned app source), `config/.env` and
`config/cross_app_config.json` (secrets), `.venv/`, `node_modules/`.

## Contents

- [(root)](#root)
- [.claude](#claude)
- [.claude/agents](#claudeagents)
- [.claude/skills/analyze-apps](#claudeskillsanalyze-apps)
- [.claude/skills/heal-failure](#claudeskillsheal-failure)
- [.claude/skills/run-all-heal](#claudeskillsrun-all-heal)
- [.claude/skills/write-scenario](#claudeskillswrite-scenario)
- [alembic](#alembic)
- [alembic/versions](#alembicversions)
- [automation](#automation)
- [automation/agent](#automationagent)
- [automation/agentic](#automationagentic)
- [automation/ai](#automationai)
- [automation/ai/services](#automationaiservices)
- [automation/api](#automationapi)
- [automation/api/v1](#automationapiv1)
- [automation/api/v1/routers](#automationapiv1routers)
- [automation/appium_service](#automationappiumservice)
- [automation/auth](#automationauth)
- [automation/ci_cd](#automationcicd)
- [automation/claude_code](#automationclaudecode)
- [automation/claude_code/prompts](#automationclaudecodeprompts)
- [automation/dashboard](#automationdashboard)
- [automation/dashboard/public](#automationdashboardpublic)
- [automation/dashboard/src](#automationdashboardsrc)
- [automation/dashboard/src/components](#automationdashboardsrccomponents)
- [automation/dashboard/src/pages](#automationdashboardsrcpages)
- [automation/dashboard/src/pages/agent](#automationdashboardsrcpagesagent)
- [automation/database](#automationdatabase)
- [automation/device_manager](#automationdevicemanager)
- [automation/device_manager/discovery](#automationdevicemanagerdiscovery)
- [automation/evidence](#automationevidence)
- [automation/inspector](#automationinspector)
- [automation/integrations](#automationintegrations)
- [automation/intelligence](#automationintelligence)
- [automation/knowledge](#automationknowledge)
- [automation/mcp](#automationmcp)
- [automation/notifications](#automationnotifications)
- [automation/ops](#automationops)
- [automation/performance](#automationperformance)
- [automation/plugins](#automationplugins)
- [automation/projects](#automationprojects)
- [automation/reporting](#automationreporting)
- [automation/reports](#automationreports)
- [automation/runner](#automationrunner)
- [automation/scenarios](#automationscenarios)
- [automation/utils](#automationutils)
- [automation/workflow](#automationworkflow)
- [config](#config)
- [docs](#docs)
- [scripts](#scripts)
- [seeds](#seeds)
- [tests](#tests)
- [vendor](#vendor)

## (root)

Only what tools need at the root. Everything else lives in a folder below.

| File | What it does | Used by |
|---|---|---|
| `.gitignore` | What git must never pick up: secrets (config/.env, cross_app_config.json), logs/, repos/, build output, caches | `projects/repository.py`, `scenarios/cross_app_config.py` |
| `.mcp.json` | Claude Code MCP servers for this project (Appium MCP, Maestro MCP) — must stay at the root | Claude Code (and the AI Agent's sessions) |
| `README.md` | Start here: what the platform is, set-up step by step, the commands, how to run a test | people |
| `alembic.ini` | Alembic settings: where the migrations live and how it logs (the database comes from DATABASE_URL) | `alembic/env.py` |
| `pyproject.toml` | Python project settings and pytest config (testpaths = tests) | `projects/detector.py` |
| `requirements.txt` | Direct runtime dependencies, derived from the actual import graph of automation/, scripts/ and tests/ — not from `pip freeze`. | `projects/config.py`, `projects/detector.py`, `projects/preparation.py` +5 |

## .claude

Claude Code's project setup: its instructions (CLAUDE.md), the procedures it can run as skills, and two read-only helper agents. The AI Agent page uses the same procedures.

| File | What it does | Used by |
|---|---|---|
| `CLAUDE.md` | Claude Code's project instructions: imports docs/AGENTS.md and lists the skills, helper agents and agent_tools.py | Claude Code (and the AI Agent's sessions) |

## .claude/agents

| File | What it does | Used by |
|---|---|---|
| `app-analyst.md` | Helper agent: reads one app's source and returns its screens, rules and real element ids (read-only) | Claude Code (and the AI Agent's sessions) |
| `flow-debugger.md` | Helper agent: explains why a flow run failed (test, platform, app, server or infra problem) | Claude Code (and the AI Agent's sessions) |

## .claude/skills/analyze-apps

| File | What it does | Used by |
|---|---|---|
| `SKILL.md` | Analyze the apps | Claude Code (and the AI Agent's sessions) |

## .claude/skills/heal-failure

| File | What it does | Used by |
|---|---|---|
| `SKILL.md` | Heal one failed run | Claude Code (and the AI Agent's sessions) |

## .claude/skills/run-all-heal

| File | What it does | Used by |
|---|---|---|
| `SKILL.md` | Run the flows and heal what fails | Claude Code (and the AI Agent's sessions) |

## .claude/skills/write-scenario

| File | What it does | Used by |
|---|---|---|
| `SKILL.md` | Write a new scenario | Claude Code (and the AI Agent's sessions) |

## alembic

Database migrations: the history of table changes, used to upgrade an existing database (e.g. Postgres). The platform also creates missing tables itself at start-up.

| File | What it does | Used by |
|---|---|---|
| `env.py` | Alembic's migration runner: reads DATABASE_URL and the models, runs upgrades online/offline | the alembic tool (`python -m alembic upgrade head`) |
| `script.py.mako` | Template Alembic uses to write a new migration file (`alembic revision`) | the alembic tool (`python -m alembic upgrade head`) |

## alembic/versions

| File | What it does | Used by |
|---|---|---|
| `778d43e546f3_initial_migration.py` | Initial migration | the alembic tool (`python -m alembic upgrade head`) |
| `84af52af343c_complete_schema_baseline.py` | complete schema baseline | the alembic tool (`python -m alembic upgrade head`) |
| `9c1f4b2ad70e_repair_saved_scenario_json.py` | repair double-encoded saved_scenarios.steps/covers | the alembic tool (`python -m alembic upgrade head`) |
| `b3e21c07f4d9_add_missing_indexes.py` | add the six indexes the models declare but production never got | the alembic tool (`python -m alembic upgrade head`) |
| `c7a4f9e2b118_reconcile_test_runs.py` | reconcile test_runs with the models: foreign keys, nullability, types | the alembic tool (`python -m alembic upgrade head`) |
| `d5e8a1c3f702_add_mcp_servers.py` | add mcp_servers: MCP servers connected to the platform (Settings → MCP servers) | the alembic tool (`python -m alembic upgrade head`) |
| `e7b2c9d4a815_add_agent_tables.py` | add agent tables: autonomous test agent (AI Agent page) | the alembic tool (`python -m alembic upgrade head`) |
| `f3a8d1e6b927_add_agent_sessions.py` | add agent_sessions: the AI Agent's Claude Code sessions | the alembic tool (`python -m alembic upgrade head`) |

## automation

The platform itself: backend, dashboard, flow engine, AI Agent and everything they use.

| File | What it does | Used by |
|---|---|---|
| `config.py` | The one place process configuration is loaded. | `alembic/env.py`, `agent/main.py`, `api/main.py` +6 |
| `formulas.py` | **Every money formula, in one place** (exact to the cent): VAT, invoice sums, coupon/discount, tip, change, split fair shares, void, comp, grand Total. Other files only read figures and draw tables. | `scenarios/invoice_check.py`, `scenarios/cross_app_flows.py`, `reporting/step_tables.py` +4 |

## automation/agent

The **execution agent**: a worker that runs on a test Mac, takes queued PR jobs from the backend and drives the device. Started by the Job Queue page (scripts/start-agent.sh).

| File | What it does | Used by |
|---|---|---|
| `main.py` | The execution agent: registers this Mac, polls queued PR jobs, reserves a device, builds/installs the app, runs the suite, reports results and performance | `plugins/appium_framework.py`, `scripts/start-agent.sh`, `scripts/stop.sh` |

## automation/agentic

Plain, free helpers the AI Agent uses: sort a failure, list every screen and whether a flow covers it, check the rig is ready, summarise a run.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | The AI Agent's plain (no-AI) parts, shared by its Claude Code sessions. | — |
| `classifier.py` | Rule-based failure sorter: decides what kind of failure a run had, for free. | `scripts/agent_tools.py` |
| `inventory.py` | What the apps contain, and how much of it the tests touch — computed, not guessed. | `api/v1/routers/agentic.py`, `scripts/agent_tools.py` |
| `preflight.py` | Can the rig run a flow right now? | `api/v1/routers/agentic.py`, `claude_code/runner.py`, `scripts/agent_tools.py` |
| `runinfo.py` | Plain readers over runs and flows — what the Claude Code agent's tools print. | `api/v1/routers/agentic.py`, `claude_code/gates.py`, `claude_code/prompts/__init__.py` +2 |
| `settings.py` | AI Agent configuration: one DB row (edited from the AI Agent page) over defaults. | `api/v1/routers/agentic.py`, `api/v1/routers/jobs.py`, `claude_code/cli.py` +5 |
| `sources.py` | Read-only access to the app source checkouts and the platform's knowledge files. | `agentic/inventory.py`, `api/v1/routers/agentic.py`, `scripts/agent_tools.py` |

## automation/ai

The LLM used by PR planning, run summaries, RCA and the AI Chat. It runs on the Claude Code CLI (claude_code_llm.py); providers.py holds the other options.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | LLM Service Module - Provider abstraction and RCA analysis | — |
| `claude_chat_guard.py` | PreToolUse hook for the AI Chat on Claude Code: an MCP server that drives the simulators is refused while a test run is using them (the registry's own rule, … | `ai/claude_code_llm.py` |
| `claude_code_llm.py` | Claude Code as the platform's language model (LLM_PROVIDER_TYPE=claude_code). | `ai/provider.py`, `intelligence/chat.py` |
| `prompts.py` | LLM Prompt Templates for Root Cause Analysis | `ai/__init__.py`, `ai/service.py` |
| `provider.py` | LLM Provider Abstraction Layer | `ai/__init__.py`, `ai/service.py`, `ai/services/intelligence.py` +9 |
| `providers.py` | LLM provider classes (OpenAI, Gemini, Claude, Ollama, Azure, mock) and get_provider() | `ai/services/intelligence.py`, `ai/services/reporting.py` |
| `service.py` | Main RCA Service - Coordinates evidence collection and LLM analysis | `ai/__init__.py`, `api/main.py`, `api/v1/routers/jobs.py` +4 |

## automation/ai/services

Small AI helpers called by the backend: bug report and failure clustering for a failed job, the run summary.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |
| `intelligence.py` | Failure clustering for a failed job's evidence (called from the evidence upload) | `api/v1/routers/jobs.py` |
| `reporting.py` | AI bug-report generator for a failed job (called from the evidence upload) | `api/v1/routers/jobs.py` |
| `summary.py` | Auto-generated, PM-friendly test summaries (Ollama-backed, DB-cached). | `api/v1/routers/jobs.py`, `api/v1/routers/projects.py`, `runner/service.py` +1 |

## automation/api

The FastAPI backend (port 8000). main.py builds the app and mounts the routers.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | API Module - FastAPI endpoints for querying runs and reports | — |
| `main.py` | FastAPI REST API for querying test runs and RCA reports | `scripts/supervise_backend.sh` |

## automation/api/v1

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |

## automation/api/v1/routers

One file per area of the API. The dashboard, the execution agent and outside services (GitHub webhook, CI) call these.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Router package | — |
| `agentic.py` | AI Agent page — Claude Code sessions (automation/claude_code/). | `api/main.py`, `agentApi.ts` |
| `agents.py` | /agents: execution-agent register + heartbeat, the Job Queue's Start/Stop agent buttons, and the agent's own device API (/me/devices reserve/activate/release) | `api/main.py` |
| `auth.py` | /auth: login, token refresh, current user, first-admin seed | `api/main.py`, `api/v1/routers/inspector.py` |
| `automation.py` | /automation: engine status, device list, run/stop a project's suite, cancel queued jobs | `api/main.py`, `api/v1/routers/__init__.py` |
| `builds.py` | Latest-build notification + one-click deploy to EVERY device. | `api/main.py` |
| `ci.py` | CI trigger endpoint. | `api/main.py` |
| `dependency.py` | Cross-app dependency analysis API. | `api/main.py` |
| `groups.py` | Application Groups — a family of related apps (e.g. | `api/main.py` |
| `inspector.py` | Live UI inspector — the accessibility tree of a running app, plus what is wrong with it. | `api/main.py` |
| `intelligence.py` | /intelligence: a run's RCA and the AI Chat (ask, stream, sessions) | `api/main.py` |
| `jobs.py` | /jobs and /runs: cross-app flow runs (run, retry, resume, run-all, edit/delete flows, step catalog), the job queue the agent polls, results, evidence, performance, … | `api/main.py`, `claude_code/session.py`, `utils/proctree.py` |
| `mcp.py` | MCP servers connected to the platform (Settings → MCP servers). | `api/main.py` |
| `ops.py` | /ops: execution agents list and the audit log (Settings page) | `api/main.py` |
| `projects.py` | /projects: register/edit/delete apps, branches, clone/pull, prepare (build + install) and its status, trend summary | `api/main.py` |
| `pull_requests.py` | Pull-request testing. | `api/main.py`, `ci_cd/pr_poller.py`, `intelligence/pr_autotest.py` |
| `recorder.py` | Live scenario recorder — mirror the simulator, turn clicks into steps. | `api/main.py` |
| `reports.py` | Test Reports — a proper, stored report for every run. | `api/main.py` |
| `scenario.py` | Scenario runner API — plain-language steps drive the app and record a script. | `api/main.py` |
| `scenarios.py` | Saved-scenario CRUD + device list for the Scenarios tab. | `api/main.py` |
| `tickets.py` | Tickets: paste an issue, link it to a PR + scenarios, and auto-test it. | `api/main.py`, `api/v1/routers/webhooks.py` |
| `webhooks.py` | GitHub webhook receiver — auto-triggers regression tests on PR/push events. | `api/main.py` |
| `workflow.py` | Workflow / coverage: the app's spine + the full test-suite coverage matrix. | `api/main.py` |

## automation/appium_service

Appium on this Mac: start/track the server, build WebDriverAgent and pick its ports.

| File | What it does | Used by |
|---|---|---|
| `manager.py` | Starts and tracks Appium server processes (free port, readiness) | `plugins/appium_framework.py` |
| `wda.py` | Single source of truth for the WebDriverAgent lifecycle. | `api/v1/routers/recorder.py`, `appium_service/manager.py`, `scenarios/cross_app_orchestrator.py` +1 |

## automation/auth

Login, tokens and roles; the execution agent's own credential.

| File | What it does | Used by |
|---|---|---|
| `security.py` | Passwords, JWT access/refresh tokens, current-user and role checks, execution-agent credentials | `api/main.py`, `api/v1/routers/agentic.py`, `api/v1/routers/agents.py` +15 |

## automation/ci_cd

The PR poller: watches GitHub for new PR commits and queues their test runs (this is what starts PR runs today).

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |
| `pr_poller.py` | PR auto-test poller. | `api/main.py` |

## automation/claude_code

The **AI Agent**: Claude Code sessions (heal, fix_run, analyze, generate, nightly), each in its own git worktree, checked by gates and approved by a person.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | The AI Agent: Claude Code (the CLI, on the logged-in Claude plan — no API key) analyzing the Vyapy apps, writing scenarios, and fixing the automation when it fails. | — |
| `baselines.py` | Unit-test failures of a commit, measured once and kept. | `claude_code/runner.py`, `scripts/agent_tools.py` |
| `cli.py` | The AI Agent from a terminal (and from the nightly launchd job). | `scripts/claude_nightly.sh` |
| `events.py` | Turn a session's stream-json transcript into short lines for the AI Agent page. | `claude_code/runner.py`, `claude_code/session.py` |
| `fingerprint.py` | Which code a flow run executed — so a later edit makes only the passes it can affect stale. | `claude_code/runner.py`, `scripts/agent_tools.py` |
| `gates.py` | The checks a session's changes must pass before a person may approve them. | `agentic/runinfo.py`, `claude_code/baselines.py`, `claude_code/runner.py` +2 |
| `guard.py` | PreToolUse hook for AI Agent sessions: the second lock on what Claude may do. | Claude Code runs it as the AI Agent's tool-use hook (`claude_code/runner.py`) |
| `impact.py` | Which flows does a change to the flow engine reach? | `claude_code/fingerprint.py`, `claude_code/gates.py`, `claude_code/runner.py` +1 |
| `paths.py` | Where the AI Agent keeps things. | `claude_code/baselines.py`, `claude_code/report.py`, `claude_code/runner.py` +1 |
| `report.py` | The email + Slack note a session sends when it ends. | `claude_code/runner.py` |
| `runner.py` | One AI Agent session, start to finish. | `claude_code/baselines.py`, `claude_code/session.py` |
| `schedule.py` | The nightly AI Agent session, started by the backend at the configured time. | `api/main.py`, `api/v1/routers/agentic.py`, `claude_code/cli.py` +1 |
| `session.py` | AI Agent sessions as the API and the CLI see them: start, follow, stop, approve. | `api/main.py`, `api/v1/routers/agentic.py`, `api/v1/routers/jobs.py` +4 |

## automation/claude_code/prompts

The instructions each kind of AI Agent session starts with.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | The prompt a session starts with: common.md (where, tools, rules, report) with the kind's task (<kind>.md) filled in from the session's options. | — |
| `analyze.md` | AI Agent prompt for an 'analyze' session: map both apps' features against the flows | `claude_code/prompts/__init__.py`, by session kind |
| `common.md` | Vyapy AI Agent — $title ($date) | `claude_code/prompts/__init__.py`, by session kind |
| `fix_run.md` | AI Agent prompt for 'Fix with Claude Code' on one failed run | `claude_code/prompts/__init__.py`, by session kind |
| `generate.md` | AI Agent prompt for a 'generate' session: write new cc_ flows for uncovered features | `claude_code/prompts/__init__.py`, by session kind |
| `heal.md` | AI Agent prompt for a 'heal' session: run the flows and fix what the automation got wrong | `claude_code/prompts/__init__.py`, by session kind |
| `nightly.md` | AI Agent prompt for the nightly session: heal + analyze + generate | `claude_code/prompts/__init__.py`, by session kind |

## automation/dashboard

The React + Vite dashboard (port 5173).

| File | What it does | Used by |
|---|---|---|
| `.gitignore` | Logs | npm / TypeScript / lint tooling |
| `.oxlintrc.json` | Lint rules for the dashboard (oxlint) | npm / TypeScript / lint tooling |
| `index.html` | The dashboard's HTML shell (Vite entry) | `scripts/start.sh` |
| `package-lock.json` | Exact versions of the dashboard's npm packages | npm / TypeScript / lint tooling |
| `package.json` | Dashboard npm packages and scripts (dev, build, lint) | `scripts/start.sh` |
| `tsconfig.app.json` | TypeScript settings for the dashboard code | npm / TypeScript / lint tooling |
| `tsconfig.json` | TypeScript project root: only points at the app and node configs (use `tsc -b`) | npm / TypeScript / lint tooling |
| `tsconfig.node.json` | TypeScript settings for vite.config.ts | npm / TypeScript / lint tooling |
| `vite.config.ts` | Vite build/dev-server config | `scripts/start.sh` |

## automation/dashboard/public

Static files served as-is.

| File | What it does | Used by |
|---|---|---|
| `favicon.svg` | Browser-tab icon | the browser |

## automation/dashboard/src

The dashboard's code: the API client, shared helpers and the app shell.

| File | What it does | Used by |
|---|---|---|
| `App.tsx` | Routes, the left menu, login guard and layout of every page | the dashboard entry (`index.html` → `main.tsx` → `App.tsx`) |
| `agentApi.ts` | AI Agent page API (backend: automation/api/v1/routers/agentic.py). | `pages/AgentPage.tsx`, `pages/RunDetails.tsx`, `pages/agent/EventFeed.tsx` +4 |
| `api.ts` | Where the backend lives. Hardcoding "localhost" meant a COWORKER opening the dashboard hit THEIR OWN machine, not this one — the page loaded and every call failed. … | `App.tsx`, `agentApi.ts`, `components/BuildUpdateBell.tsx` +23 |
| `flowScript.ts` | A cross-app flow as an editable script, for the Scripts page. | `pages/ScriptEditorPage.tsx` |
| `index.css` | Global styles and theme colours | the dashboard entry (`index.html` → `main.tsx` → `App.tsx`) |
| `main.tsx` | Starts the React app | the dashboard entry (`index.html` → `main.tsx` → `App.tsx`) |
| `richReport.ts` | Opens or fetches a run's rich HTML report (Export without screenshots) | `pages/ReportsPage.tsx`, `pages/RunDetails.tsx` |
| `time.check.mjs` | Self-check for parseServerDate. | run by hand: `node src/time.check.mjs` |
| `time.ts` | Parse a timestamp the API returned. | `pages/ChatPage.tsx`, `pages/DashboardHome.tsx`, `pages/JobQueuePage.tsx` +5 |
| `useLivePoll.ts` | Hook: refresh a page's data on an interval while it is visible | `pages/AgentPage.tsx`, `pages/DashboardHome.tsx`, `pages/JobQueuePage.tsx` +2 |

## automation/dashboard/src/components

Pieces shared between pages: dialogs, editors, the file tree, the recorder.

| File | What it does | Used by |
|---|---|---|
| `ArchifyFrame.tsx` | Shows an archify diagram (Workflow page) | `pages/WorkflowPage.tsx` |
| `BuildUpdateBell.tsx` | Top-bar bell: new app builds on GitHub, deploy them to the simulators | `App.tsx` |
| `BundleIdSelect.tsx` | Bundle-id picker (staging / production) for a project | `components/ScenarioPanel.tsx` |
| `ChatMessage.tsx` | One AI Chat message (markdown, code, tool output) | `pages/ChatPage.tsx` |
| `CrossAppRunModal.tsx` | Dashboard 'Run' dialog: pick devices/accounts and run one flow, the suite, or all flows | `pages/DashboardHome.tsx` |
| `FileTree.tsx` | Folder/file tree (the Scripts page's ios/android tree) | `pages/ScriptEditorPage.tsx` |
| `FlowEditorModal.tsx` | Edit a flow's segments and steps (save/delete a stored edit) | `components/CrossAppRunModal.tsx` |
| `McpServersCard.tsx` | Settings card: add, edit, test MCP servers for the AI Chat | `pages/SettingsPage.tsx` |
| `ModalPortal.tsx` | Renders a dialog above the page (React portal) | `components/BuildUpdateBell.tsx`, `components/CrossAppRunModal.tsx`, `components/FlowEditorModal.tsx` +7 |
| `MonacoEditor.tsx` | The code editor (Monaco) used on the Scripts page | `pages/ScriptEditorPage.tsx` |
| `RecorderModal.tsx` | Recorder: mirrors a simulator in the browser and saves each tap as a step | `pages/ScenariosPage.tsx` |
| `ScenarioEditorModal.tsx` | Create/edit a saved scenario (name, project, device, steps, covers) | `pages/PullRequestsPage.tsx`, `pages/ScenariosPage.tsx` |
| `ScenarioPanel.tsx` | Run a saved scenario live and show each step's result (Pull Requests page) | `pages/PullRequestsPage.tsx` |

## automation/dashboard/src/pages

One file per page in the left menu.

| File | What it does | Used by |
|---|---|---|
| `AgentPage.tsx` | AI Agent page (/agent): start sessions, follow them live, approve or reject their changes, feature map, settings | `App.tsx` (the page's route) |
| `ChatPage.tsx` | AI Chat page (/chat): ask about runs and the apps; streamed answers; saved sessions | `App.tsx` (the page's route) |
| `DashboardHome.tsx` | Dashboard home (/): recent runs, trends, devices, demo run | `App.tsx` (the page's route) |
| `GraphPage.tsx` | Dependency Graph page (/graph): app groups, cross-app endpoints, module graph, impact | `App.tsx` (the page's route) |
| `InspectorPage.tsx` | Live UI inspector — the screen next to its accessibility tree, with the elements that cannot be tapped called out. | `App.tsx` (the page's route) |
| `JobQueuePage.tsx` | Job Queue page (/queue): queued/running PR jobs and the execution agent's Start/Stop | `App.tsx` (the page's route) |
| `LoginPage.tsx` | Login page (/login) | `App.tsx` (the page's route) |
| `ProjectsPage.tsx` | Projects page (/projects): register apps, branches, groups, prepare (build + install) on simulators | `App.tsx` (the page's route) |
| `PullRequestsPage.tsx` | Pull Requests page (/pull-requests): PRs with their Jira tickets, planned flows, run them | `App.tsx` (the page's route) |
| `ReportsPage.tsx` | Reports page (/reports): every run, trends, Export HTML/PDF | `App.tsx` (the page's route) |
| `RunDetails.tsx` | Run Details (/run/:id): segments with Live Steps, notes, calculation report, crash screenshot, RCA, performance | `App.tsx` (the page's route) |
| `ScenariosPage.tsx` | Scenarios page (/scenarios): saved scenarios and cross-app flows; run one or Run all; recorder | `App.tsx` (the page's route) |
| `ScriptEditorPage.tsx` | Scripts page (/scripts): ios/end_to_end_scenario (edit, save, run), android/ (empty); Tickets tab | `App.tsx` (the page's route) |
| `SettingsPage.tsx` | Settings page (/settings): your account, MCP servers, audit log | `App.tsx` (the page's route) |
| `WorkflowPage.tsx` | Workflow page (/workflow): the apps' workflow diagram, coverage, what changed between refs | `App.tsx` (the page's route) |

## automation/dashboard/src/pages/agent

The parts of the AI Agent page.

| File | What it does | Used by |
|---|---|---|
| `Bits.tsx` | Small shared pieces for the AI Agent page (status chips, labels) | `pages/AgentPage.tsx`, `pages/agent/SessionDetail.tsx` |
| `EventFeed.tsx` | An AI Agent session's live event feed | `pages/AgentPage.tsx`, `pages/agent/SessionDetail.tsx` |
| `FeatureMap.tsx` | The feature map: each app feature as covered / partial / missing, with risk | `pages/AgentPage.tsx` |
| `SessionDetail.tsx` | One AI Agent session: diff, gates, runs, Approve / Reject | `pages/AgentPage.tsx` |
| `SettingsCard.tsx` | AI Agent settings (nightly time, auto-heal, report e-mails) | `pages/AgentPage.tsx` |
| `StartPanel.tsx` | Start an AI Agent session (heal, analyze, generate, nightly) | `pages/AgentPage.tsx` |
| `ui.tsx` | Shared look for the AI Agent page: the same chip / ghost button / input / section helpers the page has always used, plus the session labels. | `pages/AgentPage.tsx`, `pages/agent/Bits.tsx`, `pages/agent/EventFeed.tsx` +4 |

## automation/database

The database: tables (models.py), engine and sessions, and small helpers.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Storage Layer - Models and persistence | — |
| `config.py` | Database engine and sessions (DATABASE_URL), create/upgrade tables at start-up, get_db() | `agentic/inventory.py`, `agentic/runinfo.py`, `agentic/settings.py` +53 |
| `database.py` | Small read/write helpers over the models (projects, runs, RCA, evidence) | `api/main.py`, `api/v1/routers/jobs.py`, `api/v1/routers/projects.py` +9 |
| `models.py` | Every database table as a SQLAlchemy model (users, projects, runs, scenario results, devices, agent sessions, ...) | `alembic/env.py`, `agentic/inventory.py`, `agentic/runinfo.py` +53 |

## automation/device_manager

Which devices exist on which Mac, their health and reservations, so two runs never share a simulator.

| File | What it does | Used by |
|---|---|---|
| `models.py` | Device, DeviceStatus, DeviceHealth types | `api/v1/routers/automation.py`, `api/v1/routers/pull_requests.py`, `database/models.py` +5 |
| `reservation.py` | Exclusive use of one simulator, so two jobs cannot drive it at once. | `api/v1/routers/agents.py`, `appium_service/wda.py` |
| `service.py` | The device registry: machines, devices per machine, hydration at start-up, which machine owns a simulator | `api/main.py`, `api/v1/routers/agents.py`, `api/v1/routers/automation.py` +7 |

## automation/device_manager/discovery

Finding devices: iOS (simctl) and Android (adb).

| File | What it does | Used by |
|---|---|---|
| `android.py` | Finds connected Android devices/emulators (adb) for the execution agent | `agent/main.py` |
| `base.py` | Interface for a device-discovery provider | `device_manager/discovery/android.py`, `device_manager/discovery/ios.py` |
| `ios.py` | Finds iOS simulators/devices (simctl) for the execution agent | `agent/main.py` |

## automation/evidence

What a failure leaves behind: JS console, device log, crash reports — with personal data scrubbed.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Evidence Collector Module - Gather test logs, diffs, screenshots | — |
| `collector.py` | Evidence Collection Module | `runner/service.py` |
| `crash_report.py` | Crash reports for the app under test, from the last few minutes. | `scenarios/cross_app_flows.py` |
| `device_log.py` | The device's own log for the app, around the moment a step failed. | `scenarios/cross_app_flows.py` |
| `js_console.py` | The app's own JS console, reduced to the lines that explain a failure. | `scenarios/cross_app_flows.py` |
| `pii_scrub.py` | PII/Secret Scrubbing for GDPR Compliance | `ai/service.py` |

## automation/inspector

The Inspector page's backend: the live UI tree of a simulator.

| File | What it does | Used by |
|---|---|---|
| `overlap.py` | Which elements are covered by something else — the check that would have caught three of the four bugs that ate 2026-09-02. | `api/v1/routers/inspector.py` |

## automation/integrations

GitHub and Jira, and posting results on a PR.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |
| `github.py` | GitHub API: list PRs, diffs, commits (PR page, PR poller, PR auto-test) | `api/v1/routers/pull_requests.py`, `ci_cd/pr_poller.py`, `intelligence/pr_autotest.py` |
| `jira.py` | Jira API: read a ticket by key, create issues (PR page, notifications) | `api/v1/routers/pull_requests.py`, `notifications/dispatch.py` |
| `pr_comment.py` | PR comment bot — posts a QA verdict comment and a commit status back onto the GitHub PR. | `api/v1/routers/webhooks.py`, `runner/service.py` |

## automation/intelligence

The 'brains': which tests a PR needs, impact across apps, the self-healing step runner, learned locators, RCA, PR auto-test.

| File | What it does | Used by |
|---|---|---|
| `bill_validator.py` | Bill Validator for Vyapy QA Bot. | `intelligence/scenario_runner.py` |
| `chat.py` | AI Chat Assistant with streaming, context injection, and structured output. | `api/v1/routers/intelligence.py` |
| `cross_app_analyzer.py` | Cross-app dependency analysis. | `api/v1/routers/dependency.py`, `intelligence/hybrid_impact_analyzer.py` |
| `element_catalog.py` | Automation identifier catalog — how an element was found, and how sure we are. | `intelligence/scenario_runner.py` |
| `git_analyzer.py` | Which modules a git diff touches (GitHub webhook PR runs) | `api/v1/routers/webhooks.py`, `intelligence/recommendation.py` |
| `hybrid_impact_analyzer.py` | Hybrid cross-app impact analysis. | `api/v1/routers/dependency.py`, `api/v1/routers/webhooks.py`, `intelligence/impact_selection.py` |
| `impact_predictor.py` | Test impact prediction — score each test's chance of failing so we run the riskiest first. | `api/v1/routers/jobs.py`, `api/v1/routers/webhooks.py` |
| `impact_selection.py` | Graph-driven smart test selection. | `intelligence/pr_planner.py` |
| `learned_locators.py` | Persistent self-healing locator memory. | `intelligence/scenario_runner.py` |
| `pr_autotest.py` | Autonomous PR test loop. | `api/v1/routers/ci.py`, `api/v1/routers/pull_requests.py`, `ci_cd/pr_poller.py` |
| `pr_planner.py` | PR-aware test planner. | `api/v1/routers/pull_requests.py`, `intelligence/pr_autotest.py` |
| `recommendation.py` | Which tests to run for the modules a PR touched (GitHub webhook PR runs) | `api/v1/routers/webhooks.py` |
| `scenario_runner.py` | Interpret plain-language scenario steps and DRIVE the app, one step at a time. | `api/v1/routers/scenario.py`, `scenarios/cross_app_flows.py`, `scenarios/cross_app_orchestrator.py` +1 |
| `visual_regression.py` | Visual regression analysis — pixel-diff current screenshots vs a stored baseline. | `api/v1/routers/jobs.py`, `runner/service.py` |

## automation/knowledge

Grounded facts the flows and the AI read: real element ids, known issues, how to write a flow, the app graph.

| File | What it does | Used by |
|---|---|---|
| `app_graph.json` | The app's code knowledge graph (from graphify): navigation hubs the ticket drafter anchors flows on | `api/v1/routers/tickets.py` (ticket scenario drafting) |
| `default.md` | Vya Consumer app — curated facts (AUTHORITATIVE) | `api/v1/routers/tickets.py` (ticket scenario drafting) |
| `full_event_map.md` | Full-event scenario — app map (read from source 2026-10-29) | people and the AI Agent (the END TO END SCENARIO's map) |
| `known_issues.md` | Known issues — read before investigating a failure | `.claude/CLAUDE.md`, `.claude/agents/flow-debugger.md`, `claude_code/prompts/common.md` |
| `learned_locators.json` | Element ids the runner learned per app (self-healing memory) | `intelligence/learned_locators.py`, `agentic/inventory.py`, `scripts/agent_tools.py` |
| `locator_map.md` | Grounded Locator Map — Vya Consumer + Business apps | `claude_code/prompts/common.md` |
| `scenario_authoring.md` | Writing a scenario fast | `.claude/skills/write-scenario/SKILL.md`, `claude_code/prompts/generate.md`, `claude_code/prompts/nightly.md` |
| `vya_scenario_library.md` | Vya scenario library (extracted from Vya-agentic-Bot, verified against the real flow) | `api/v1/routers/tickets.py` (ticket scenario drafting) |

## automation/mcp

MCP servers the AI Chat can use (registry and presets).

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | MCP (Model Context Protocol) servers connected to the platform. | — |
| `client.py` | A small MCP client: JSON-RPC 2.0 over stdio or Streamable HTTP. | `mcp/registry.py` |
| `registry.py` | Live connections to the configured MCP servers, shared by the API and the chat. | `ai/claude_chat_guard.py`, `api/main.py`, `api/v1/routers/mcp.py` +1 |

## automation/notifications

Sending results out: Slack, Jira, e-mail.

| File | What it does | Used by |
|---|---|---|
| `dispatch.py` | Notification & ticketing dispatch — reads config from env and fans out. | `api/v1/routers/reports.py`, `claude_code/report.py`, `integrations/pr_comment.py` +2 |
| `email.py` | SMTP email — the morning report. | `claude_code/report.py` |
| `realtime_alerts.py` | Real-time failure alerts — push a Slack message the moment something fails, not at run end. | `runner/service.py` |

## automation/ops

Background ops monitor started with the backend.

| File | What it does | Used by |
|---|---|---|
| `monitor.py` | Background ops monitor started with the backend (agent health, alerts) | `api/main.py` |

## automation/performance

App performance during a PR job: CPU, memory, timings, API call times from the Metro log.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |
| `api_interceptor.py` | Parse Metro bundler / console output for API call timing. | `performance/collector.py` |
| `collector.py` | Performance collection for iOS simulator/device runs. | `agent/main.py` |

## automation/plugins

How the platform runs an app repo's own test suite (Appium + pytest).

| File | What it does | Used by |
|---|---|---|
| `appium_framework.py` | Runs an app repo's own Appium + pytest suite on a device and returns the verdict | `agent/main.py`, `runner/service.py` |
| `base.py` | Interface every test-framework plugin implements | `plugins/appium_framework.py` |

## automation/projects

The registered apps: clone, branches, environments, build the app, install it on simulators, Metro, machine setup.

| File | What it does | Used by |
|---|---|---|
| `builder.py` | Application build & install. | `agent/main.py`, `api/main.py`, `api/v1/routers/automation.py` +15 |
| `config.py` | The automation.yaml schema an app repo can carry (framework, execution, reports, AI) | `projects/repository.py` |
| `deployment.py` | Make a scenario's required apps actually BE on the devices it will drive. | `api/v1/routers/builds.py`, `scenarios/cross_app_flows.py`, `scenarios/cross_app_orchestrator.py` |
| `detector.py` | Project type detection and automation.yaml template generation. | `api/v1/routers/builds.py`, `api/v1/routers/dependency.py`, `api/v1/routers/projects.py` +3 |
| `environments.py` | Resolve (project, branch, environment) -> the build configuration for that variant. | `api/v1/routers/builds.py`, `api/v1/routers/projects.py`, `projects/builder.py` +2 |
| `machine_setup.py` | Bring a Mac to a buildable state by itself -- no terminal, no hand-typed fixes. | `projects/preparation.py`, `scripts/setup_mac.sh` |
| `macos_environment.py` | What this Mac has, what the project needs, and where the two disagree. | `projects/builder.py`, `projects/machine_setup.py`, `projects/preparation.py` +2 |
| `preparation.py` | Project preparation pipeline. | `agent/main.py`, `api/v1/routers/projects.py`, `api/v1/routers/scenario.py` +3 |
| `purge.py` | Remove a project and every trace of it, except its test history. | `api/v1/routers/projects.py`, `scripts/purge_project.py` |
| `repository.py` | RepositoryManager: clone, pull, branches, health of each registered app checkout in repos/ | `agent/main.py`, `api/v1/routers/builds.py`, `api/v1/routers/dependency.py` +16 |
| `setup_measure.py` | Measured wrappers around the EXISTING preparation steps. | `projects/preparation.py` |
| `setup_report.py` | Measured, honest reporting of project setup. | `projects/preparation.py`, `projects/setup_measure.py` |
| `simulators.py` | Which iOS simulators on THIS Mac can actually be driven by Appium. | `projects/builder.py`, `projects/machine_setup.py`, `scenarios/cross_app_config.py` +2 |

## automation/reporting

The run report: HTML, calculation tables, the one invoice, export facts.

| File | What it does | Used by |
|---|---|---|
| `engine.py` | Standalone HTML report for a job run | `api/v1/routers/jobs.py` |
| `invoice_section.py` | ONE invoice in the report, in the layout of the user's invoice sample. | `reporting/scenario_report.py` |
| `run_facts.py` | What a run really did, from its segments' own notes, for the reports. | `api/v1/routers/reports.py` |
| `scenario_report.py` | Rich scenario report renderer (VAT / bill validation). | `api/v1/routers/reports.py`, `knowledge/full_event_map.md`, `scenarios/invoice_check.py` |
| `step_tables.py` | The calculation tables of the run report, read from what each step wrote down. | `knowledge/full_event_map.md`, `reporting/invoice_section.py`, `reporting/run_facts.py` +1 |

## automation/reports

Older report pieces: the PR suite's HTML report and the pass % per scenario.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Report Generator Module - Markdown/HTML/PDF reports from RCA | — |
| `generator.py` | Report Generator Module | `runner/service.py` |
| `step_stats.py` | How much of a scenario actually passed. | `api/v1/routers/jobs.py` |

## automation/runner

Runs a project's test suite on a device for PR jobs.

| File | What it does | Used by |
|---|---|---|
| `service.py` | RunnerService: runs a project's test suite on a device (PR jobs, /automation/run) | `api/v1/routers/automation.py`, `api/v1/routers/dependency.py`, `api/v1/routers/jobs.py` |

## automation/scenarios

The **flow engine**: cross_app_flows.py (every flow and step handler), the idb/Appium drivers, invoice checks, run issues and screen health.

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |
| `cross_app_config.py` | Cross-app run configuration: which simulator plays each role, and the logins. | `agentic/preflight.py`, `api/v1/routers/jobs.py`, `api/v1/routers/scenarios.py` +4 |
| `cross_app_flows.py` | Every cross-app flow (FLOWS) and the FlowRunner that runs them: segments per role, the @token step handlers, retries, crash checks and the notes the report is built from | `.claude/agents/flow-debugger.md`, `.claude/skills/heal-failure/SKILL.md`, `.claude/skills/write-scenario/SKILL.md` +13 |
| `cross_app_orchestrator.py` | Drive the Consumer (iPhone) and Business (iPad) simulators AT ONCE. | `api/v1/routers/jobs.py`, `scenarios/cross_app_flows.py` |
| `idb_coords.py` | Turn a point from `idb ui describe-all` into the point `idb ui tap` expects. | `api/v1/routers/inspector.py`, `intelligence/scenario_runner.py`, `scenarios/cross_app_flows.py` +2 |
| `idb_driver.py` | Fast, VERIFIED screen actions through idb -- the flows' first choice. | `intelligence/scenario_runner.py`, `knowledge/scenario_authoring.md`, `scenarios/cross_app_flows.py` +1 |
| `idb_fast.py` | Drop-in replacement for `subprocess.run([idb, *args], ...)` without the start-up. | `api/v1/routers/inspector.py`, `intelligence/scenario_runner.py`, `scenarios/cross_app_flows.py` +2 |
| `idb_path.py` | Where the idb binary lives. | `api/v1/routers/inspector.py`, `intelligence/scenario_runner.py`, `projects/machine_setup.py` +4 |
| `idb_worker.py` | A long-lived idb client: runs idb CLI commands in-process, one per request. | started as its own process by `scenarios/idb_fast.py` |
| `invoice_check.py` | Does an open invoice add up? | `knowledge/full_event_map.md`, `reporting/run_facts.py`, `reporting/scenario_report.py` +2 |
| `run_issues.py` | Problems a run saw that did not fail it, shown YELLOW in the report and on Run Details. | `pages/RunDetails.tsx`, `reporting/run_facts.py`, `reporting/scenario_report.py` +1 |
| `run_records.py` | Backend-local run bookkeeping for scenario execution. | `api/v1/routers/scenario.py`, `api/v1/routers/tickets.py`, `api/v1/routers/workflow.py` +2 |
| `screen_text.py` | Read the text a screen SHOWS, for what the accessibility tree does not carry. | `projects/machine_setup.py`, `scenarios/cross_app_flows.py`, `scenarios/idb_coords.py` |
| `service.py` | Scenario execution — the engine that drives plain-language steps on a device. | `agent/main.py`, `api/v1/routers/scenario.py`, `api/v1/routers/tickets.py` +3 |
| `ui_health.py` | Watches a step's screens for loading problems (spinners that never go, content that never arrives) without failing the step | `scenarios/cross_app_flows.py` |

## automation/utils

Shared utilities: secret masking in logs, environment validation.

| File | What it does | Used by |
|---|---|---|
| `proctree.py` | Track the processes THIS platform spawns, so teardown can take the whole tree. | `agent/main.py`, `api/v1/routers/agents.py`, `appium_service/manager.py` |
| `security.py` | Security Utilities - Secret Masking Prevents accidental exposure of tokens, API keys, and credentials in logs/responses. | `api/main.py` |
| `validator.py` | Pre-Execution Environment Validator | `projects/preparation.py` |

## automation/workflow

The Workflow page: diagrams of the apps' journeys (drawn by vendor/archify).

| File | What it does | Used by |
|---|---|---|
| `__init__.py` | Package marker | — |
| `archify_cli.py` | Thin wrapper around the vendored Archify CLI. | `api/v1/routers/workflow.py` |
| `archify_ir.py` | The cross-app spine as Archify JSON IR. | `api/v1/routers/workflow.py`, `workflow/catalog_at_ref.py` |
| `catalog.py` | The Vyapy test-suite coverage catalog + the app's core workflow (spine). | `api/v1/routers/workflow.py`, `api.ts`, `pages/WorkflowPage.tsx` +2 |
| `catalog_at_ref.py` | The spine as it was at a git ref — the 'before' half of the PR delta. | `api/v1/routers/workflow.py` |

## config

Settings. `.env` and `cross_app_config.json` hold secrets and are never committed; the rest is committed.

| File | What it does | Used by |
|---|---|---|
| `.env.example` | ── GitHub Webhook Secret shared with GitHub to validate the X-Hub-Signature-256 header. | `scripts/doctor.sh`, `scripts/install.py` |
| `ios-support.json` | Supported iOS range (16-26) for doctor.sh and install checks | `scripts/doctor.sh` |
| `project-environments.json` | Each app's environments: bundle ids, Metro ports, branches, source replacements | `projects/environments.py` |

## docs

Longer docs: install, architecture and traps (AGENTS.md), machine setup, and this map.

| File | What it does | Used by |
|---|---|---|
| `AGENTS.md` | AGENTS.md — Vyapy AI Regression Testing Platform | people |
| `INSTALL.md` | Install & verify | people |
| `MACHINE_SETUP.md` | Machine setup — running the platform on a new Mac | people |
| `PROJECT_MAP.md` | Project map | people |

## scripts

Commands and tools. start/stop/status/install are the day-to-day commands; agent_tools.py is every platform action the AI Agent takes.

| File | What it does | Used by |
|---|---|---|
| `agent_tools.py` | The commands the AI Agent (Claude Code) uses to work on the rig. | `.claude/CLAUDE.md`, `.claude/agents/flow-debugger.md`, `.claude/skills/analyze-apps/SKILL.md` +10 |
| `check_project_rows.py` | Check the LIVE platform DB for project rows that contradict reality. | run by hand |
| `claude_nightly.sh` | Start the nightly AI Agent session by hand, from a terminal. | `claude_code/schedule.py` |
| `clear-screenshots.sh` | Free storage by deleting failure screenshots from the DB. | run by hand |
| `doctor.sh` | Check every prerequisite this platform needs, and say how to fix each miss. | `config/ios-support.json` |
| `dryrun.py` | Trigger a cross-app flow and wait (keeping the daemon thread alive) until it ends. | `scenarios/cross_app_flows.py` |
| `install.py` | Checks a new machine: Python, Node, Git, Appium, packages, config/.env and which database it will use | `claude_code/gates.py` |
| `metro_ports.py` | Emit the Metro ports scripts/status.sh should check, split by whether anything starts them. | `scripts/status.sh` |
| `platform_seed.py` | Move the platform's OWN data — projects and saved scenarios — between machines. | `alembic/versions/9c1f4b2ad70e_repair_saved_scenario_json.py`, `database/models.py` |
| `purge_project.py` | Remove a project and every trace of it, keeping its test history. | run by hand |
| `setup_mac.sh` | One-time setup of a new Mac for the automation platform. | run by hand |
| `start-agent.sh` | Starts the execution agent that runs queued PR jobs (the Job Queue page's Start button runs it) | `api/v1/routers/agents.py`, `api.ts`, `scripts/stop.sh` |
| `start.sh` | Starts everything, safely re-runnable: boots the simulators, Appium (:4723), the supervised backend (:8000) and the dashboard (:5173) | `claude_code/cli.py`, `claude_code/gates.py`, `claude_code/runner.py` +5 |
| `status.sh` | Shows what is up or down: backend, dashboard, Appium, supervisor, Metros, booted simulators, the staging server | `claude_code/gates.py`, `pages/AgentPage.tsx`, `scripts/metro_ports.py` +1 |
| `stop.sh` | Stops the backend, its supervisor, the dashboard and the execution agent; `--all` also stops Appium and the Metros | `claude_code/cli.py`, `claude_code/gates.py`, `claude_code/session.py` +4 |
| `supervise_backend.sh` | Keep the platform backend alive — if it ever exits (crash, OOM), restart it. | `auth/security.py`, `claude_code/gates.py`, `claude_code/session.py` +3 |

## seeds

Data a fresh install imports.

| File | What it does | Used by |
|---|---|---|
| `platform.json` | The committed scenarios (and projects) a fresh install imports with scripts/platform_seed.py | `scripts/platform_seed.py` |

## tests

The unit tests (`pytest`). They need no device or simulator.

| Test file | What it checks |
|---|---|
| `conftest.py` | Keep the test suite off the real database. |
| `test_4d_integration.py` | Phase 4D.5 — end-to-end integration across the whole machine-aware stack. |
| `test_accept_appointment.py` | The 'accept the appointment' handler. |
| `test_add_items_to_order.py` | Adding items must actually reach the order, not just look like it did. |
| `test_agent_device_api.py` | Phase 4F.2A — the agent's device lifecycle moved behind authenticated HTTP. |
| `test_agent_identity.py` | Phase 4F.1 — the backend derives agent identity from a credential, not a claim. |
| `test_agentic.py` | The AI Agent's plain parts (automation/agentic/): the rule sorter, the coverage inventory, @token validation and the run/flow readers its Claude Code sessions use. |
| `test_app_installed_preflight.py` | Preflight: is the environment's app actually on the chosen simulator? |
| `test_archify_ir.py` | The spine -> Archify IR converter, and the vendored CLI. |
| `test_assign_table_idb.py` | @assign_table through idb when the table sheet is up (measured 53-75s via Appium). |
| `test_assign_table_wrong_screen.py` | The @assign_table legacy path must not tap on a screen that has no table sheet. |
| `test_bounded_calls.py` | Ceilings on Appium calls, and what happens after one is hit (review 2026-10-19). |
| `test_bundle_for_env.py` | Bundle ids translate between staging and production both ways |
| `test_bundle_id_scoping.py` | PRODUCT_BUNDLE_IDENTIFIER must never be a global xcodebuild override. |
| `test_cancel_event.py` | The My Bookings cancel handler (@cancel_event) and its consumer-side check (@wallet_invite_cancelled). |
| `test_capture_calculation.py` | Guard: `capture <label> as <name>` + `verify calculation <name> ± n = <name>`. |
| `test_card_horizontal_search.py` | Horizontal card search on the waiter bookings board. |
| `test_claude_agent.py` | The AI Agent on Claude Code (automation/claude_code/). |
| `test_connection_hardening.py` | Phase 4G.3 — engine settings for PostgreSQL, and no session across an Appium run. |
| `test_consumer_checkout.py` | C-App Checkout on this run's booking. |
| `test_continue_session.py` | Continue an interrupted AI Agent session where it stopped. |
| `test_crash_check_fast.py` | The per-step native-crash check must not call Appium when the screen read it already has proves the app is alive. |
| `test_crash_report.py` | Crash reports reduced to what a human reads first. |
| `test_crash_screenshots.py` | A run takes a screenshot ONLY when the app crashed. |
| `test_database_url_resolution.py` | Every process must resolve the SAME database. |
| `test_declared_bundle_id.py` | A project can DECLARE the bundle id it is meant to produce. |
| `test_decline_and_wallet_event.py` | @decline_appointment and @wallet_event_shows:<text> (AI Agent drafts cc_decline_invite / cc_book_two_hours). |
| `test_dependency_graph.py` | The Dependency Graph page: group -> scan -> graph -> analyze. |
| `test_deploy_progress.py` | Weighted progress + ETA for the Latest-build deploy. |
| `test_device_lock.py` | One run per simulator. |
| `test_device_log.py` | Device log, filtered to what explains a failure. |
| `test_device_registry.py` | Phase 4C — persistent, machine-aware device registry. |
| `test_device_reservation.py` | Phase 4E.1 — the per-device reservation primitive. |
| `test_device_selector.py` | Phase P0-2 — a scenario's device survives moving to another Mac. |
| `test_doctor_script.py` | The doctor must check the things that failed silently, not just the obvious ones. |
| `test_duration_slider.py` | The consumer's Duration became a 4-stop slider (Oct 2026): 1 hr, 2 hr, 3 hr, Not Sure. |
| `test_environment_preflight.py` | Machine problems met on a fresh Intel Mac after an Xcode update. |
| `test_events_list_scroll.py` | Opening a booking from the hour's events list reaches rows below the fold. |
| `test_failure_evidence_paths.py` | Every failure path must attach evidence — not just the common one. |
| `test_fast_branch_switch.py` | Switching to an already-built branch must not recompile or re-pod the app. |
| `test_faster_scenarios.py` | Creating scenarios faster. |
| `test_filter_orders.py` | The My Orders status-filter handler (@filter_orders:<status>). |
| `test_flow_stop.py` | Guard: pressing Stop must actually stop an in-process cross-app flow run. |
| `test_folly_clockid.py` | RCT-Folly's clockid_t typedef conflicts with the iOS 26 SDK. |
| `test_folly_cxx_standard.py` | RCT-Folly must not be compiled at a standard that dropped std::unary_function. |
| `test_fresh_machine_readiness.py` | The platform must work on a machine that has only a clone of this repo. |
| `test_fresh_simulator_flow.py` | A flow must work on a simulator the app has never run on. |
| `test_formulas.py` | Every money formula in automation/formulas.py, with the real examples (10.20/2, VAT 15.43 on 125.56). |
| `test_full_event_steps.py` | The steps the full-event flow (flow_full_event) added, on scripted screens. |
| `test_gitignore_sanity.py` | Source directories must not be swallowed by generated-output ignore rules. |
| `test_guests_void_comp_split.py` | Handlers of 'Guests -> void / split / comp -> pay for all in B-App'. |
| `test_heal_verb_guard.py` | A self-heal must never swap the action verb of the control it lands on. |
| `test_id_aliases.py` | Tablet <-> phone accessibility-id aliases. |
| `test_idb_coords.py` | idb taps on the landscape iPad must land on the element describe-all names. |
| `test_idb_driver.py` | The fast idb driver: every tap is verified under the point, every fill read back. |
| `test_idb_fast.py` | idb_fast must never be worse than spawning the idb CLI: same result shape, same exceptions, and a silent fall back to the CLI whenever a worker is unavailable. |
| `test_idb_fast_companion.py` | A dead idb companion is revived instead of leaving every read of its device empty (automation/scenarios/idb_fast.py). |
| `test_idb_token_match.py` | The idb label matcher must be token-strict: it has to find the restaurant card whose label merely CONTAINS the phrase, without ever confusing NylaiKitchen2 with the … |
| `test_index_reconciliation.py` | Phase 4G.5A — the six declared-but-missing indexes, and target hardening. |
| `test_inspector_api.py` | The inspector endpoint's contract: tree + screen + what cannot be tapped. |
| `test_inspector_idb_path.py` | The Inspector must find idb wherever this machine keeps it. |
| `test_inspector_orientation.py` | The Inspector must not draw a portrait screenshot into a landscape box. |
| `test_install_doc.py` | INSTALL.md must cover the steps whose absence breaks an install silently. |
| `test_invoice_check.py` | Does an open invoice add up: automation/scenarios/invoice_check.py and its steps. |
| `test_invoice_section.py` | The report's ONE invoice (automation/reporting/invoice_section.py), in the layout of the user's invoice sample (2026-11-05: "keep only one whole invoice like this ... |
| `test_js_console.py` | Only the log lines that explain a failure — from the real Metro output. |
| `test_keyboard_dismiss.py` | The keyboard a `type` step raises must be closed before the next step taps. |
| `test_kitchen_ready_selects_products.py` | The chef's job on a ticket: select every item -> Ready -> Close Order. |
| `test_landscape_rotation.py` | idb REPORTS app-space coordinates but TAPS in device space. |
| `test_learned_locators.py` | Unit tests for the persistent self-healing locator store (no device needed). |
| `test_leave_review.py` | The 'leave a review' handler (@leave_review). |
| `test_live_steps.py` | Live Steps shows what the run is doing while it does it. |
| `test_local_devices.py` | Role devices must exist on the Mac the run is on. |
| `test_machine_aware_polling.py` | Phase 4D.3 — poll_job routes a machine-pinned job only to its machine. |
| `test_machine_setup.py` | A new Mac must become buildable without anyone typing commands. |
| `test_macos_environment.py` | The preflight must tell an environment problem from an application one. |
| `test_mcp_servers.py` | MCP servers in the platform: connect them in Settings, the AI Chat uses their tools. |
| `test_metro_port_ownership.py` | A declared Metro port with no project row is unclaimed, not down. |
| `test_metro_targeting.py` | Prepare must start Metro on the environment's port and point the app at it. |
| `test_migration_authority.py` | Phase 4G.1 — Alembic can build the complete schema from scratch. |
| `test_missing_business_login.py` | Waiter demo failed with 'could not log in as waiter … (check creds …)' when the real cause was an empty waiter password, and its AI summary said the run 'finished with … |
| `test_modify_table.py` | The Event screen's Modify Table handler (@modify_table). |
| `test_multi_machine_safety.py` | Phase 4B safety fixes, ahead of a second Mac joining the platform. |
| `test_no_partial_guess.py` | Guard: a MULTI-word step must never resolve on ONE of its words. |
| `test_no_secrets_committed.py` | Nothing committed may contain a real credential. |
| `test_not_sure_booking.py` | Every scenario books with duration 'Not Sure' (asked 2026-10-01). |
| `test_open_order_screen_identity.py` | The ADD NEW ITEM sheet must never be mistaken for the opened order summary. |
| `test_open_reservation_bounds.py` | @open_reservation must fail fast and say why, never hang the segment. |
| `test_open_reservation_hour_row.py` | Find the booked hour's row first, then the card under it. |
| `test_open_restaurant.py` | 'click NylaiKitchen2' on the consumer Home → @open_restaurant. |
| `test_orders_panel.py` | Opening a booking from the waiter's right-hand My Orders panel. |
| `test_overlap.py` | The overlap detector, checked against the three real bugs of 2026-09-02. |
| `test_participant_role.py` | The participant role: a second diner account on its OWN iPhone. |
| `test_patch_package_applied.py` | The platform applies patches/, because on these projects nothing else does. |
| `test_path_with_space.py` | A path containing a space breaks React Native 0.68's own build tooling. |
| `test_pay_business.py` | Paying on the waiter's order screen (Business app, PaymentDetails.js). |
| `test_performance_boundary.py` | Phase 4F.6 — performance data crosses the agent/backend boundary over HTTP. |
| `test_planned_scenarios_regression.py` | Guard for the regression Phase 4F.4's audit found in Phase 4F.3's code. |
| `test_platform_seed.py` | Projects and scenarios must travel to a new machine. |
| `test_postgres_integration.py` | Opt-in PostgreSQL integration coverage. |
| `test_pr_autotest_dedup.py` | The PR poller skips a commit only if a TestRun carries its sha. |
| `test_preparation_progress.py` | Preparation reports how far it has got, from the steps it already emits. |
| `test_prepare_uses_selected_branch.py` | Execute must build the branch the card is showing. |
| `test_proctree.py` | Process-tree teardown: covers the leak where killing Appium's parent pid left the xcodebuild/WDA descendants alive. |
| `test_proctree_boundary.py` | Phase 4F.7A — the orphan reaper asks the backend, not the database. |
| `test_proctree_report.py` | Observation reporting: aggregates the audit trail, touches no process. |
| `test_proctree_restart.py` | Crash/restart validation: the registry outlives the agent that wrote it. |
| `test_production_guards.py` | The security guards must FAIL CLOSED when APP_ENV is production. |
| `test_project_environment_select.py` | A project's environment is chosen on the dashboard, not in the database. |
| `test_project_environments.py` | Environment-aware builds: config resolution, artifact identity, variant safety. |
| `test_project_payload.py` | Phase 4F.5 — the agent's scenario path no longer reads TestProject. |
| `test_project_purge.py` | Purging a project removes its traces and KEEPS its test history. |
| `test_providers.py` | The LLM provider factory and the mock provider |
| `test_pull_branch_ref.py` | A pull must target exactly one ref, and say so when it cannot. |
| `test_purge_uninstalls_app.py` | A purge must take the installed app with it — but never a shared one. |
| `test_qrcode_textencoder.py` | A QR code must not red-screen the app. |
| `test_recheck_size.py` | Check the size again when it is a session's only failed check. |
| `test_registry_resolution.py` | Phase 4D.4 — queue-time device resolution reads the registry, not simctl. |
| `test_reinstall_replaces_stale_app.py` | Preparation must UNINSTALL before it installs. |
| `test_remote_branch_listing.py` | Branches come from the remote, so one can be picked instead of typed. |
| `test_removed_frameworks.py` | Name the pod that uses a framework Apple has removed. |
| `test_required_deps.py` | An imported-but-undeclared package must still be installed. |
| `test_required_deps_are_conditional.py` | The platform must not install a dependency the app does not import. |
| `test_reservation_integration.py` | Phase 4E.2 — reservation wired into the queued agent path, and machine-global WDA ports. |
| `test_reset_table.py` | The My Bookings reset-table handler (@reset_table). |
| `test_resume_run.py` | Resume a failed cross-app flow from the segment that failed. |
| `test_run_all_order.py` | Run-all (dashboard "Run all"): each flow starts only after the previous one ENDS. |
| `test_run_facts.py` | What a run really did, for the Reports page (automation/reporting/run_facts.py). |
| `test_run_failure_reason.py` | A failed run must say WHY, and a slow simctl must not fail a run. |
| `test_run_identity.py` | Phase 4F.4a — the agent's planned scenarios use the job's TestRun. |
| `test_run_issues.py` | What a run saw that did not fail it is said in YELLOW (automation/scenarios/run_issues.py): a crash after a step that did its job is relaunched past, a spinner left up, … |
| `test_run_retry.py` | Retrying a failed run re-runs the SAME thing. |
| `test_save_btn_aim.py` | Aiming the Save tap around the LogBox toast. |
| `test_save_wait_fast.py` | @save_appointment's wait for the form to close: cheap point probes, one full read to confirm. |
| `test_scenario_deployment.py` | Scenario preflight deploys the apps it needs, onto the devices it was assigned. |
| `test_scenario_json_hygiene.py` | Phase 4G.2 — SavedScenario.steps/covers are always lists. |
| `test_scenario_results_api.py` | Phase 4F.3 — scenario results are written by the backend, not the agent. |
| `test_scenario_service_boundary.py` | Phase 4F.4 — the scenario execution service and the boundary around it. |
| `test_scroll_into_view.py` | An element idb can see but that sits below the fold must be scrolled to, not abandoned. |
| `test_segment_abort_on_failure.py` | Guard: a failed segment must STOP the flow, not let later segments run. |
| `test_select_all_items.py` | Selecting every item on the waiter's Order Summary. |
| `test_serve_navigation.py` | Guard: every serve/settle segment must NAVIGATE to the order before acting on it. |
| `test_services.py` | The AI bug-report generator returns a BugReport |
| `test_settled_booking.py` | The waiter's settle steps when there is nothing left to settle. |
| `test_setup_report.py` | The setup report must only ever print what was measured. |
| `test_silence_logbox.py` | The platform silences the app's LogBox so its toasts stop covering real controls. |
| `test_sim_media_indexers.py` | Fresh iOS 26 simulators run mediaanalysisd/photoanalysisd at ~400% CPU each. |
| `test_slot_row_drag.py` | The waiter form's time chips are one horizontal row of the whole day. |
| `test_slot_toast_filter.py` | A time slot the LogBox toast covers must not be tapped. |
| `test_step_stats.py` | Step-level pass/fail accounting. |
| `test_step_tables.py` | The run report's calculation tables read from each step's '[ok]' line (automation/ reporting/step_tables.py). |
| `test_table_sheet_accessibility.py` | Guard: the "Select A Table" sheet must be exposed to accessibility on every machine. |
| `test_test_runs_reconciliation.py` | Phase 4G.5B — reconciling test_runs with the models. |
| `test_too_early.py` | A booking the app will not open yet stops the run with the time to come back. |
| `test_toolchain_fixes.py` | The platform respells old RN source that Xcode 26's clang rejects. |
| `test_type_verify.py` | Typing must be verified, not assumed. |
| `test_ui_health.py` | Passive UI loading monitor. |
| `test_verdict_from_results.py` | Guard: an agent run is judged by what RAN, not by the exit code. |
| `test_waiter_board.py` | Two SEPARATE defects on the waiter bookings board. |
| `test_watchdog_hook.py` | Hook 1 — the passive step watchdog. |
| `test_wda_resolver.py` | The WDA resolver must never hand Appium a build that cannot launch. |
| `test_xcode_error_reporting.py` | A failed build must report the error, not the warnings above it. |
| `test_yarn_install_path.py` | A Yarn project must be installed by Yarn, into a real node_modules tree. |

## vendor

A third-party copy of **archify** (`vendor/archify/`), a Node tool that draws the Workflow page's diagrams.
`automation/workflow/archify_cli.py` runs it. Don't edit these files; update the copy as a whole.

| Part | What it holds |
|---|---|
| `bin/` | the command line (`archify.mjs`) and its preview/visual-check helpers |
| `renderers/` | one renderer per diagram kind (architecture, dataflow, lifecycle, sequence, workflow) and shared helpers |
| `schemas/` | the JSON schema each diagram kind's input must follow |
| `delta/`, `recipes/` | the 'what changed' view and ready-made scenarios |
| `assets/`, `brand-marks/` | icons and fonts the diagrams use |
| `README.platform.md`, `SKILL.md`, `LICENSE`, `package*.json`, `.pinned-commit` | how the copy is used here, its licence, its packages and the upstream commit it was taken from |
