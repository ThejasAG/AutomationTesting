# Architecture Guide — Enterprise AI Test Orchestration Platform

## System Overview

The platform is organized into three independently deployable tiers:

1. **Control Plane** — FastAPI backend + React dashboard
2. **Execution Plane** — Python execution agents + Appium servers
3. **Intelligence Plane** — Pluggable LLM provider layer

---

## Component Architecture

### Backend (FastAPI)

```
automation/api/
├── main.py              — App factory, CORS, global error handler, startup
└── v1/routers/
    ├── auth.py          — JWT login/register endpoints
    ├── projects.py      — Git project CRUD + health checks
    ├── automation.py    — Device management + run orchestration
    ├── jobs.py          — Agent polling, status updates, evidence upload
    ├── agents.py        — Agent registration and heartbeat
    ├── analytics.py     — Trends and failure analytics
    ├── intelligence.py  — AI recommendations + chat assistant
    └── ops.py           — Metrics, alerts, audit logs
```

### Database Layer

- **ORM**: SQLAlchemy (SQLite for development, PostgreSQL for production)
- **Models**: `TestRun`, `TestProject`, `ExecutionAgent`, `RCAReport`, `EvidenceBundle`, `AIRecommendation`, `SystemAlert`, `AuditLog`
- **Migrations**: Alembic

### Execution Agents

Each agent is a standalone Python process that:
1. Registers with the platform API
2. Sends heartbeat every 10 seconds with connected device list
3. Polls `/api/v1/jobs/poll` for new jobs
4. Clones/pulls the repository
5. Creates a `.venv` and installs dependencies
6. Starts a local Appium server on a dynamic free port
7. Executes pytest within the venv
8. Collects evidence (logs, screenshots, page source)
9. Uploads evidence bundle back to the platform
10. Terminates the Appium server

### AI Services Architecture

```
automation/ai/
├── provider.py          — LLMProvider abstract class + factory
├── providers.py         — Concrete providers (OpenAI, Ollama, Azure, Claude, Gemini)
└── services/
    ├── intelligence.py  — GitImpactAnalyzer, TestRecommendationEngine, FailureClusteringEngine
    ├── execution.py     — SelfHealingEngine, FlakyTestDetector
    ├── reporting.py     — BugReportGenerator
    └── assistant.py     — ChatAssistant, KnowledgeBase
```

**Provider Configuration** (via environment variables):
```env
LLM_PROVIDER_TYPE=openai
OPENAI_API_KEY=sk-...
LLM_MODEL_NAME=gpt-4o-mini
```

---

## Data Flow: Execution Job

```
User clicks Execute (Dashboard)
         │
         ▼
POST /api/v1/automation/run
         │
         ▼
TestRun created with job_state="queued"
         │
         ▼ (Agent polls every 5s)
POST /api/v1/jobs/poll
         │
         ▼
Platform assigns job to agent by device match
         │
         ▼
Pre-Execution Validation (validator.py)
  Checks: repo, yaml, venv, device, appium
         │
         ▼
Git sync → yaml validate → venv create → pip install
         │
         ▼
Appium server on dynamic free port
         │
         ▼
pytest in venv with APPIUM_URL env var
         │
         ▼
Evidence collected → uploaded to platform (with retry)
         │
         ▼
AI analysis triggered (if failed)
         │
         ▼
HTML report generated
         │
         ▼
job_state = "completed" | "failed"
```

---

## Security Architecture

- **Authentication**: JWT Bearer tokens
- **Secret Masking**: `SecretFilter` installed on root logger at startup
- **CORS**: Configurable in `main.py` — restrict `allow_origins` in production
- **Credentials**: All secrets via environment variables only

---

## Scalability Notes

| Component | Current | Scaling Path |
|---|---|---|
| Database | SQLite | Migrate to PostgreSQL |
| Agents | Unlimited | Each agent is stateless |
| Reports | Local filesystem | Mount shared NFS / S3 |