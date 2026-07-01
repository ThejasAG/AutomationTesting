# Deployment Guide

## Docker (Recommended for Production)

### Prerequisites
- Docker 24+
- Docker Compose 2.20+

### Start the Full Stack

```bash
docker-compose up -d
```

This starts:
- FastAPI backend on port 8000
- PostgreSQL on port 5432
- Redis on port 6379

### Configure Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/automation_db
SECRET_KEY=your-jwt-secret-key-here
LLM_PROVIDER_TYPE=openai
OPENAI_API_KEY=sk-...
GITHUB_TOKEN=ghp_...
JIRA_URL=https://your-org.atlassian.net
JIRA_TOKEN=...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

---

## Production Checklist

- [ ] Set `DATABASE_URL` to PostgreSQL (not SQLite)
- [ ] Set a strong `SECRET_KEY` (64+ random characters)
- [ ] Restrict CORS `allow_origins` in `automation/api/main.py`
- [ ] Configure all integration tokens in `.env`
- [ ] Set `LLM_PROVIDER_TYPE` and API keys for AI features
- [ ] Mount `reports/` to persistent storage
- [ ] Set up HTTPS (nginx reverse proxy recommended)
- [ ] Run `alembic upgrade head` to migrate database

---

## Running the Execution Agent (Systemd)

Create `/etc/systemd/system/automation-agent.service`:

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

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable automation-agent
systemctl start automation-agent
```

---

## Nginx Reverse Proxy

```nginx
server {
    listen 443 ssl;
    server_name automation.yourcompany.com;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        proxy_pass http://127.0.0.1:5173;
    }
}
```
