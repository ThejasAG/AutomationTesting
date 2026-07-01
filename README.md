# Enterprise AI Test Orchestration Platform v1.0-RC

[![Platform](https://img.shields.io/badge/Platform-Enterprise-blue)](.)
[![Status](https://img.shields.io/badge/Status-Release%20Candidate-orange)](.)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green)](.)
[![Node](https://img.shields.io/badge/Node-18%2B-green)](.)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](.)

An AI-powered enterprise platform for orchestrating mobile automation at scale. Supports distributed execution agents, real device farming, AI-driven failure analysis, and a fully functional web dashboard.

---

## 🚀 Quick Start

### Prerequisites

Run the installation wizard to verify your environment:

```bash
python install.py
```

The wizard checks: Python 3.10+, Node.js 18+, Git, Java, ADB, Appium, and required packages.

### 1. Install Dependencies

```bash
# Python backend
pip install -r requirements.txt

# React dashboard
cd automation/dashboard
npm install
```

### 2. Start the Platform

```bash
# Terminal 1 — Backend API
uvicorn automation.api.main:app --reload

# Terminal 2 — Dashboard (http://localhost:5173)
cd automation/dashboard
npm run dev
```

### 3. Start an Execution Agent

Connect a physical Android device or start an emulator, then:

```bash
python -m automation.agent.main
```

### 4. Register a Project

1. Open the dashboard at `http://localhost:5173`
2. Log in (`admin` / `admin123` by default)
3. Go to **Test Orchestration → Register Project**
4. Enter your Git repository URL
5. Select a connected device → Click **Execute**

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     React Dashboard                      │
│            (Vite + TypeScript + Recharts)               │
└──────────────────────┬──────────────────────────────────┘
                       │ REST API
┌──────────────────────▼──────────────────────────────────┐
│                  FastAPI Backend                         │
│  Auth · Projects · Jobs · Analytics · Ops · AI APIs    │
└───────┬──────────────────────────────────┬──────────────┘
        │ SQLite (dev) / PostgreSQL (prod)  │ Agents Poll
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full architecture guide.

---

## 📁 Project Structure

```
automation-platform/
├── install.py                    # Installation wizard
├── requirements.txt              # Python dependencies
├── automation/
│   ├── api/                      # FastAPI backend
│   │   ├── main.py               # App factory + global error handler
│   │   └── v1/routers/           # REST endpoint routers
│   ├── agent/                    # Execution agent
│   │   └── main.py               # Polling loop + job runner
│   ├── ai/                       # AI services (LLM providers, services)
│   ├── appium_service/           # Appium server lifecycle manager
│   ├── auth/                     # JWT authentication
│   ├── dashboard/                # React frontend (Vite)
│   ├── database/                 # SQLAlchemy models + migrations
│   ├── device_manager/           # ADB device discovery
│   ├── integrations/             # GitHub, Jira, Slack
│   ├── ops/                      # Operations monitor
│   ├── plugins/                  # Framework plugins (Appium)
│   ├── projects/                 # Repository management
│   ├── reporting/                # HTML/Markdown report generation
│   └── utils/                    # Validator, security masking
├── demo-mobile-tests/            # Official sample test repository
│   ├── automation.yaml           # Platform configuration
│   ├── requirements.txt
│   ├── pages/                    # Page Object Model classes
│   └── tests/                    # Pytest test suites
├── docs/                         # Full documentation
│   ├── ARCHITECTURE.md
│   ├── OPERATIONS.md
│   ├── DEPLOYMENT.md
│   └── RELEASE_NOTES_v1.md
└── reports/                      # Generated HTML reports (auto-created)
```

---

## 🤖 AI Capabilities

| Capability | Description |
|---|---|
| AI RCA | Root cause analysis for every failed test |
| Bug Report Generator | AI-generated structured bug reports |
| Failure Clustering | Groups similar failures across runs |
| Git Impact Analyzer | Recommends tests based on changed files |
| Self-Healing Engine | Suggests alternate locators on element not found |
| Flaky Test Detector | Identifies non-deterministic tests over time |
| AI Chat Assistant | Natural language Q&A over test history |

---

## 📖 Documentation

- [Architecture Guide](docs/ARCHITECTURE.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Deployment Guide](docs/DEPLOYMENT.md)
- [Release Notes v1.0](docs/RELEASE_NOTES_v1.md)

---

## 🔒 Security

- All API routes protected by JWT
- Secrets and tokens masked in all log output
- Environment variables used for all credentials (never hardcoded)
- See `automation/utils/security.py` for the secret masking module

---

## 📝 License

MIT License — See [LICENSE](LICENSE) for details.