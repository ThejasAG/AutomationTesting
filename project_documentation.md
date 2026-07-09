# Enterprise AI Test Orchestration Platform — Project Documentation

> **Version:** v1.0-RC &nbsp;|&nbsp; **Status:** Release Candidate — Production Ready &nbsp;|&nbsp; **Release Date:** July 2026

---

## Table of Contents

1. [Overview](#overview)
2. [Technology Stack](#technology-stack)
3. [Architecture](#architecture)
4. [Project Structure](#project-structure)
5. [Core Modules](#core-modules)
6. [AI Capabilities](#ai-capabilities)
7. [Intelligence Modules](#intelligence-modules)
8. [CI/CD Integrations](#cicd-integrations)
9. [Third-Party Integrations](#third-party-integrations)
10. [Configuration](#configuration)
11. [Dashboard UI](#dashboard-ui)
12. [Demo Test Suite](#demo-test-suite)
13. [Deployment](#deployment)
14. [Security](#security)
15. [Data Retention](#data-retention)
16. [Known Limitations](#known-limitations)
17. [Roadmap (v2.0)](#roadmap-v20)

---

## Overview

The **Enterprise AI Test Orchestration Platform** is an AI-powered system for orchestrating mobile test automation at scale. It combines a FastAPI backend, a React dashboard, distributed Python execution agents, and a pluggable LLM intelligence layer to provide end-to-end automation for Android applications.

**Key Capabilities:**
- Register Git-hosted test projects and run them against real/emulated Android devices
- Distributed, stateless execution agents — each agent manages one or more devices
- AI-driven Root Cause Analysis (RCA), failure clustering, self-healing, and a chat assistant
- Integrated evidence collection (screenshots, logs, page source) uploaded after each run
- HTML and JUnit report generation
- GitHub, Jira, and Slack integrations
- Native CI/CD support — GitHub Actions, Jenkins, and Azure Pipelines
- JWT-secured REST API with automatic secret masking in all logs
- GDPR-compliant configurable data retention policy

---

## Technology Stack

### Backend
| Layer | Technology |
|---|---|
| Web Framework | FastAPI ≥ 0.100 |
| ASGI Server | Uvicorn ≥ 0.23 |
| ORM | SQLAlchemy ≥ 2.0 |
| DB (Dev) | SQLite |
| DB (Prod) | PostgreSQL 15 |
| Cache / Queue | Redis 7 |
| Migrations | Alembic |
| HTTP Client | httpx ≥ 0.24 |
| Auth | JWT (`python-jose`) + `passlib[bcrypt]` |
| Templating | Jinja2 ≥ 3.0 |
| Data Validation | Pydantic ≥ 2.0 |

### Frontend
| Layer | Technology |
|---|---|
| Framework | React (Vite + TypeScript) |
| Charts | Recharts |
| Dev Server | `npm run dev` → `http://localhost:5173` |

### Mobile Automation
| Layer | Technology |
|---|---|
| Driver | Appium (UiAutomator2) |
| Client | Appium-Python-Client ≥ 3.0 |
| Test Runner | pytest ≥ 7.4 |
| Device Discovery | ADB (Android Debug Bridge) |

### AI / LLM
| Provider | Notes |
|---|---|
| OpenAI (GPT-4o-mini) | Default cloud provider |
| Azure OpenAI | Enterprise Azure deployment |
| Anthropic Claude | Claude API |
| Google Gemini | Gemini API |
| Ollama | Self-hosted local LLM |
| MockLLMProvider | Default if no env vars set |

### DevOps / CI
| Tool | Usage |
|---|---|
| Docker Compose | Local stack (PostgreSQL + Redis) |
| GitHub Actions | CI pipeline (`automation/ci_cd/github_actions.yml`) |
| Jenkins | Enterprise CI (`automation/ci_cd/Jenkinsfile`) |
| Azure Pipelines | Azure DevOps (`automation/ci_cd/azure-pipelines.yml`) |
| systemd | Production agent deployment |
| Nginx | Reverse proxy / HTTPS termination |
| Alembic | Database schema migrations |
| Ruff | Python linting |
| mypy | Static type checking |

---

## Architecture

The platform is organized into **three independently deployable tiers**:

```
┌─────────────────────────────────────────────────────────┐
│                     React Dashboard                      │
│            (Vite + TypeScript + Recharts)               │
└──────────────────────┬──────────────────────────────────┘
                       │ REST API (JWT-secured)
┌──────────────────────▼──────────────────────────────────┐
│                  FastAPI Backend                         │
│  Auth · Projects · Jobs · Analytics · Ops · AI APIs    │
└───────┬──────────────────────────────────┬──────────────┘
        │ SQLite (dev) / PostgreSQL (prod)  │ Agent Polling (5s)
┌───────▼────────────┐            ┌────────▼─────────────┐
│   AI Services      │            │  Execution Agents     │
│  (pluggable LLM)   │            │  (per-device workers) │
└────────────────────┘            └────────┬──────────────┘
                                           │ ADB / Appium
                                  ┌────────▼──────────────┐
                                  │   Android Devices      │
                                  │  (physical/emulator)   │
                                  └───────────────────────┘
```

### Execution Job Data Flow

```
User clicks Execute (Dashboard)
         │
         ▼
POST /api/v1/automation/run
         │
         ▼
TestRun created → job_state = "queued"
         │
         ▼ (Agent polls /api/v1/jobs/poll every 5s)
Platform assigns job by device match
         │
         ▼
Pre-Execution Validation (10+ pre-flight checks)
         │
         ▼
Git sync → yaml validate → venv create → pip install
         │
         ▼
Appium server started on a dynamic free port
         │
         ▼
pytest executed inside venv (APPIUM_URL injected via env)
         │
         ▼
Evidence collected → uploaded to platform (retry x3)
         │
         ▼
AI RCA triggered (on failure)
         │
         ▼
HTML report generated
         │
         ▼
job_state = "completed" | "failed"
```

---

## Project Structure

```
AutomationTesting/
├── install.py                    # Installation wizard (validates all deps)
├── requirements.txt              # Core Python dependencies
├── pyproject.toml                # Project metadata + optional dev deps
├── package.json                  # npm scripts (dev, test, lint, typecheck)
├── alembic.ini                   # Alembic database migration config
├── docker-compose.yml            # PostgreSQL + Redis local stack
│
├── automation/
│   ├── api/                      # FastAPI application
│   │   ├── main.py               # App factory, CORS, global error handler
│   │   └── v1/routers/           # REST endpoint routers (see below)
│   ├── agent/                    # Execution agent process
│   │   └── main.py               # Polling loop + job runner
│   ├── ai/                       # AI/LLM services
│   │   ├── provider.py           # LLMProvider abstract class + factory
│   │   ├── providers.py          # Concrete LLM providers
│   │   ├── prompts.py            # LLM prompt templates
│   │   ├── service.py            # Top-level AI service coordinator
│   │   └── services/             # Intelligence, execution, reporting, chat, generator, knowledge
│   ├── appium_service/           # Appium server lifecycle manager + router
│   ├── auth/                     # JWT authentication helpers
│   ├── ci_cd/                    # CI/CD pipeline definitions
│   │   ├── github_actions.yml    # GitHub Actions workflow
│   │   ├── Jenkinsfile           # Jenkins declarative pipeline
│   │   └── azure-pipelines.yml   # Azure DevOps pipeline
│   ├── dashboard/                # React frontend (Vite + TypeScript)
│   │   └── src/pages/            # 13 full-featured UI pages
│   ├── database/                 # SQLAlchemy models + Alembic migrations
│   │   ├── models.py             # All ORM model definitions
│   │   ├── database.py           # DB access/query functions
│   │   ├── config.py             # SessionLocal, engine, Base
│   │   ├── retention.py          # GDPR data retention policy engine
│   │   └── temp_trends.py        # Temporary trend aggregation helpers
│   ├── device_manager/           # ADB device discovery
│   ├── evidence/                 # Evidence bundle handling
│   ├── integrations/             # GitHub, Jira, Slack connectors
│   ├── intelligence/             # Advanced test intelligence modules
│   │   ├── analytics.py          # Analytics processing
│   │   ├── chat.py               # AI chat interface logic
│   │   ├── flaky_detector.py     # Flaky test scoring engine
│   │   ├── git_analyzer.py       # Git diff → module impact mapper
│   │   ├── memory.py             # Contextual memory for AI assistant
│   │   ├── prediction.py         # Predictive test risk scoring
│   │   └── recommendation.py     # AI-generated recommendations
│   ├── notifications/            # Notification dispatch
│   ├── ops/                      # Operational metrics + audit logs
│   ├── plugins/                  # Framework plugins (Appium)
│   ├── projects/                 # Repository management
│   ├── reporting/                # HTML / Markdown report generation
│   ├── runner/                   # Test runner orchestration
│   ├── scripts/                  # Utility scripts
│   └── utils/                    # Validator, security masking, helpers
│
├── demo-mobile-tests/            # Official sample Banking App test suite
│   ├── automation.yaml           # Platform configuration file
│   ├── requirements.txt          # Test-level dependencies
│   ├── pages/                    # Page Object Model (LoginPage, DashboardPage)
│   └── tests/                    # pytest test suites (test_login, test_dashboard)
│
├── docs/                         # Documentation
│   ├── ARCHITECTURE.md
│   ├── DEPLOYMENT.md
│   ├── RELEASE_NOTES_v1.md
│   └── ADR-001-llm-provider.md   # Architecture Decision Records
│
└── reports/                      # Auto-created output directory for HTML reports
```

---

## Core Modules

### API Routers (`automation/api/v1/routers/`)

| Router | Path Prefix | Responsibility |
|---|---|---|
| `auth.py` | `/api/v1/auth` | JWT login, user registration |
| `projects.py` | `/api/v1/projects` | Git project CRUD, health checks |
| `automation.py` | `/api/v1/automation` | Device management, run orchestration |
| `jobs.py` | `/api/v1/jobs` | Agent job polling, status updates, evidence upload |
| `agents.py` | `/api/v1/agents` | Agent registration, heartbeat tracking |
| `analytics.py` | `/api/v1/analytics` | Trends, failure analytics |
| `intelligence.py` | `/api/v1/intelligence` | AI recommendations, chat assistant |
| `ops.py` | `/api/v1/ops` | Metrics, system alerts, audit logs |

Additionally, the Appium service router is mounted directly at the app level (outside the `/api/v1` prefix) via `automation.appium_service.router`.

### Core API Endpoints (`automation/api/main.py`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/health` | Platform health check |
| `GET` | `/api/v1/runs` | List test runs (filter by suite, status) |
| `GET` | `/api/v1/runs/{run_id}` | Get test run details |
| `GET` | `/api/v1/runs/{run_id}/rca` | Get AI RCA report for a run |
| `GET` | `/api/v1/runs/{run_id}/evidence` | Get evidence bundle for a run |
| `GET` | `/api/v1/trends` | Get failure trends (configurable days) |
| `POST` | `/api/v1/runs/{run_id}/analyze` | Trigger background RCA analysis |

### Database Models (`automation/database/models.py`)

| Model | Table | Description |
|---|---|---|
| `User` | `users` | User accounts with role-based access (`admin`, `qa`, `developer`, `viewer`) |
| `TestProject` | `test_projects` | Registered Git projects |
| `TestRun` | `test_runs` | Individual test execution records with CI metadata, flakiness flag, and risk score |
| `ExecutionAgent` | `execution_agents` | Registered agents with health metrics (CPU, memory, uptime, drain state) |
| `ModuleStability` | `module_stability` | Per-module pass/fail/flaky aggregated counts |
| `UserFeedback` | `user_feedback` | User feedback on AI recommendations |
| `RCAReport` | `rca_reports` | AI-generated root cause analysis linked to a run |
| `EvidenceBundle` | `evidence_bundles` | Screenshots, Appium logs, device logs, page source, git metadata |
| `AIRecommendation` | `ai_recommendations` | AI-generated test plans and self-heal suggestions |
| `ProjectSettings` | `project_settings` | Per-project Jira + GitHub credential overrides |
| `AuditLog` | `audit_logs` | Full audit trail of all API actions |
| `SystemAlert` | `system_alerts` | Operational alerts (agent offline, queue overflow, etc.) |

### Execution Agent (`automation/agent/main.py`)

The agent is a **stateless Python process** that:
1. Registers with the platform API on startup
2. Sends a **heartbeat every 10 seconds** with its connected device list
3. Polls `/api/v1/jobs/poll` for new jobs every 5 seconds
4. Clones or pulls the target Git repository
5. Creates a virtual environment (`.venv`) and installs dependencies
6. Starts a local Appium server on a **dynamically chosen free port**
7. Executes `pytest` inside the venv with the `APPIUM_URL` environment variable injected
8. Collects evidence (logs, screenshots, page source)
9. Uploads the evidence bundle to the platform (with **3× retry + exponential back-off**)
10. Terminates the Appium server

### Pre-Execution Validator (`automation/utils/validator.py`)

Every job runs through **10+ pre-flight checks** before execution begins. Each failure surfaces:
- **Problem** — what failed
- **Cause** — why it failed
- **Impact** — what this blocks
- **Resolution** — how to fix it

---

## AI Capabilities

| Capability | Module | Description |
|---|---|---|
| **AI RCA** | `ai/services/intelligence.py` | Root cause analysis for every failed test |
| **Bug Report Generator** | `ai/services/reporting.py` | Generates structured bug reports from failures |
| **Script Generator** | `ai/services/generator.py` | AI-generates test scripts from natural language |
| **Knowledge Base** | `ai/services/knowledge.py` | Persistent knowledge base for the AI assistant |
| **Failure Clustering** | `ai/services/intelligence.py` | Groups similar failures across multiple runs |
| **Self-Healing Engine** | `ai/services/execution.py` | Suggests alternate element locators on "element not found" |
| **AI Chat Assistant** | `ai/services/chat.py` | Natural language Q&A over test history + KnowledgeBase |

### LLM Provider Configuration

Set in environment variables:

```env
LLM_PROVIDER_TYPE=openai          # openai | azure | claude | gemini | ollama
OPENAI_API_KEY=sk-...
LLM_MODEL_NAME=gpt-4o-mini
```

> [!NOTE]
> If no `LLM_PROVIDER_TYPE` is set, the platform defaults to `MockLLMProvider`, which returns placeholder AI responses. Real AI analysis requires a configured provider.

---

## Intelligence Modules

The `automation/intelligence/` package provides standalone analysis engines that run independently of the LLM provider:

| Module | Class | Description |
|---|---|---|
| `git_analyzer.py` | `GitImpactAnalyzer` | Extracts changed files from git diff and maps them to application modules using keyword mapping (configurable via `automation.yaml`) |
| `flaky_detector.py` | `FlakyTestDetector` | Scores test flakiness by analyzing pass/fail toggle patterns over the last N runs. Confidence < 70% → flagged as flaky |
| `prediction.py` | *(predictor)* | Predictive risk scoring for test runs based on history |
| `recommendation.py` | *(recommender)* | Generates AI-driven test selection recommendations |
| `analytics.py` | *(analytics)* | Aggregates module stability and trends data |
| `memory.py` | *(memory)* | Stores and retrieves contextual memory for the AI chat assistant |
| `chat.py` | *(chat)* | Handles contextual AI chat sessions with test history awareness |

### Flakiness Algorithm

```
confidence = 100.0 - (toggle_count × 15.0)
is_flaky   = confidence < 70.0

Special case: If pass_rate < 50% and toggles == 0:
  → confidence = 80.0 (consistently broken, not flaky)
```

### Git Impact Mapping

The `GitImpactAnalyzer` uses a default module map that can be overridden per-project in `automation.yaml`:

```
Login module    → files containing: Auth, Login, SignIn, Security
Registration    → files containing: Signup, Register, Onboarding
Checkout        → files containing: Cart, Payment, Checkout, Order
Profile         → files containing: User, Settings, Account
```

---

## CI/CD Integrations

The `automation/ci_cd/` directory provides ready-to-use pipeline definitions for three major CI platforms:

### GitHub Actions (`github_actions.yml`)

Triggers on push/PR to `main`. Pipeline steps:
1. Checkout with full git history (`fetch-depth: 0`)
2. Set up Python 3.10
3. Install platform dependencies
4. Install and start Appium + UiAutomator2 driver
5. Start the Automation API server
6. Trigger a test run via REST API and poll for completion
7. Upload RCA reports to GitHub Artifacts on failure

### Jenkins (`Jenkinsfile`)

Declarative pipeline with credential injection for Azure OpenAI and Slack. Stages:
1. **Checkout** — SCM checkout
2. **Setup Environment** — pip install + Appium setup
3. **Run Test Suite & AI RCA** — Start API, trigger run, poll completion
4. **Publish Reports** — Archive all HTML/Markdown reports and Appium logs

### Azure Pipelines (`azure-pipelines.yml`)

Azure DevOps compatible YAML pipeline for running within Azure-hosted agents.

---

## Third-Party Integrations

| Integration | Module | Functionality |
|---|---|---|
| **GitHub** | `integrations/github.py` | Pull PR diffs for Git Impact Analyzer |
| **Jira** | `integrations/jira.py` | Auto-create Jira tickets from bug reports |
| **Slack** | *(notifications module)* | Notify channels on test run completion |

All integration tokens are configured exclusively through environment variables — never hardcoded. Per-project Jira and GitHub credentials can also be stored in `ProjectSettings` (via the database) to support multi-project setups.

---

## Configuration

### automation.yaml (per project)

Every test project must include an `automation.yaml` file in its repository root. Key sections:

```yaml
project:
  name: "My App Tests"
  version: "1.0.0"

framework:
  type: appium
  language: python
  driver: uiautomator2

execution:
  command: pytest tests/ -v --tb=short --junit-xml=reports/results.xml
  retries: 1
  timeout_minutes: 30

environment:
  platform: android
  app: builds/MyApp.apk
  capabilities:
    platformName: Android
    automationName: UiAutomator2

evidence:
  screenshots: true
  page_source: true
  appium_logs: true

ai:
  enabled: true
  rca: true
  self_healing: true
  flaky_detection: true

notifications:
  slack: false
  github: false
  jira: false
```

### Environment Variables (`.env`)

```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/automation_db

# Auth
SECRET_KEY=your-jwt-secret-key-64-chars-minimum

# AI
LLM_PROVIDER_TYPE=openai
OPENAI_API_KEY=sk-...
LLM_MODEL_NAME=gpt-4o-mini

# Integrations
GITHUB_TOKEN=ghp_...
JIRA_URL=https://your-org.atlassian.net
JIRA_TOKEN=...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

---

## Dashboard UI

The React dashboard (`automation/dashboard/`) provides **13 full-featured pages**:

| Page | File | Description |
|---|---|---|
| **Login** | `LoginPage.tsx` | JWT authentication UI |
| **Dashboard Home** | `DashboardHome.tsx` | Executive summary, KPIs, recent activity |
| **Automation** | `AutomationPage.tsx` | Device management, test execution control |
| **Run Details** | `RunDetails.tsx` | Full run detail view with RCA, evidence, logs |
| **Insights** | `InsightsPage.tsx` | AI recommendations, flaky tests, risk scores |
| **Analytics** | `AnalyticsPage.tsx` | Charts and trends (powered by Recharts) |
| **Operations** | `OperationsDashboard.tsx` | System alerts, audit logs, ops metrics |
| **Device Ops** | `DeviceOpsCenter.tsx` | Real-time device health and ADB management |
| **Command Center** | `CommandCenter.tsx` | Bulk job management and queue control |
| **CI/CD** | `CIPage.tsx` | CI pipeline status and integration setup |
| **Script Generator** | `ScriptGeneratorPage.tsx` | AI-powered test script generation UI |
| **Admin Center** | `AdminCenter.tsx` | User management and platform settings |
| **Settings** | `SettingsPage.tsx` | User preferences |

---

## Demo Test Suite

The `demo-mobile-tests/` directory is an **official sample repository** demonstrating how to structure a test project for the platform.

**Suite:** Banking App Appium Tests (Android)

| File | Description |
|---|---|
| `automation.yaml` | Full platform configuration reference |
| `pages/LoginPage.py` | Page Object for login screen |
| `pages/DashboardPage.py` | Page Object for dashboard screen |
| `tests/conftest.py` | Shared Appium driver fixture (reads from env vars) |
| `tests/test_login.py` | 5 login test scenarios |
| `tests/test_dashboard.py` | 3 dashboard test scenarios |
| `requirements.txt` | Appium-Python-Client + pytest |

---

## Deployment

### Local Development

```bash
# 1. Validate environment
python install.py

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install dashboard dependencies
cd automation/dashboard && npm install

# 4. Start backend (Terminal 1)
uvicorn automation.api.main:app --reload

# 5. Start dashboard (Terminal 2)
cd automation/dashboard && npm run dev
# → http://localhost:5173

# 6. Start an execution agent (Terminal 3)
python -m automation.agent.main
```

Default credentials: **admin / admin123**

### Docker (Recommended for Production)

```bash
# Start PostgreSQL + Redis
docker-compose up -d

# Run database migrations
alembic upgrade head

# Start backend
uvicorn automation.api.main:app
```

### Systemd (Agent as a Service)

```ini
[Unit]
Description=AI Test Orchestration Agent
After=network.target

[Service]
Type=simple
User=automation
WorkingDirectory=/opt/automation-platform
ExecStart=/opt/automation-platform/.venv/bin/python -m automation.agent.main
Restart=always
RestartSec=10
EnvironmentFile=/opt/automation-platform/.env
```

### Nginx Reverse Proxy

```nginx
server {
    listen 443 ssl;
    server_name automation.yourcompany.com;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
    }

    location / {
        proxy_pass http://127.0.0.1:5173;
    }
}
```

---

## Security

| Mechanism | Details |
|---|---|
| **Authentication** | JWT Bearer tokens on all API routes |
| **Password Hashing** | `passlib[bcrypt]` |
| **Role-Based Access** | Four roles: `admin`, `qa`, `developer`, `viewer` |
| **Secret Masking** | `SecretFilter` installed on the root Python logger at startup — automatically redacts JWTs, API keys, GitHub tokens, Slack webhooks |
| **CORS** | Configurable in `automation/api/main.py` — restricted to `localhost:5173` and `localhost:3000` by default; restrict `allow_origins` in production |
| **Credentials** | All secrets via environment variables only — never hardcoded |
| **Structured Error Responses** | Global exception handler returns structured JSON (problem/cause/resolution/doc_link) — no raw stack traces exposed |

> [!CAUTION]
> Change the default `admin / admin123` credentials immediately before any production deployment.

---

## Data Retention

The platform implements a **GDPR-compliant configurable data retention policy** via `automation/database/retention.py`.

### Default Retention Windows

| Data Type | Default Retention |
|---|---|
| Execution Logs | 90 days |
| Screenshots | 30 days |
| Reports | 365 days |
| Audit Logs | 730 days (2 years) |

### Usage

```python
from automation.database.retention import RetentionPolicy, enforce_retention

policy = RetentionPolicy(
    logs_days=90,
    screenshots_days=30,
    reports_days=365,
    audit_logs_days=730,
)

deleted_counts = enforce_retention(storage_backend, policy)
```

---

## Known Limitations

| Limitation | Workaround |
|---|---|
| SQLite not suitable for >10 concurrent agents | Migrate to PostgreSQL via `docker-compose.yml` |
| AI defaults to `MockLLMProvider` | Set `LLM_PROVIDER_TYPE` + `OPENAI_API_KEY` in `.env` |
| No iOS Appium support | iOS requires WebDriverAgent on a macOS host (planned for v2.0) |
| No built-in APK storage | Configure APK path in `automation.yaml`; use a file server or artifact store |
| No multi-tenancy | All data shares a single database schema (multi-tenancy planned for v2.0) |

---

## Roadmap (v2.0)

- 🐘 **PostgreSQL + Redis** as default infrastructure (replacing SQLite)
- 🏢 **Multi-tenancy** with organization-scoped data isolation
- 🍎 **iOS / XCUITest** automation support
- ☁️ **Cloud device providers** — BrowserStack, Sauce Labs, AWS Device Farm
- ⚙️ **GitHub Actions / GitLab CI** native integration (enhanced)
- 🔭 **Distributed tracing** with OpenTelemetry
- 🔐 **Role-Based Access Control (RBAC)** enforcement at the API layer
- 📦 **APK/IPA artifact management** with configurable storage backends

---

*Generated: 2026-07-08 | Source: [d:/AutomationTesting](file:///d:/AutomationTesting)*
