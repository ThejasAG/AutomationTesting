from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from datetime import datetime
import shutil
import subprocess
import os

from automation.database.config import get_db
from automation.database.models import TestRun, ExecutionAgent, SystemAlert, AuditLog, TestProject
from automation.auth.security import get_current_user

router = APIRouter(prefix="/ops", tags=["Operations"])

@router.get("/health")
def get_health(db: Session = Depends(get_db)):
    """Quick platform health check — suitable for load balancer probes."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database connection failed")

@router.get("/health/full")
def get_full_health(db: Session = Depends(get_db)):
    """
    Comprehensive Platform Health Center.
    Evaluates every major platform component and returns a health score.
    """
    components = []

    def _check(name: str, fn) -> Dict[str, Any]:
        try:
            result = fn()
            return {"name": name, "status": "ok", "detail": result}
        except Exception as e:
            return {"name": name, "status": "error", "detail": str(e)}

    # 1. Database
    components.append(_check("Database", lambda: (db.execute(text("SELECT 1")), "Connected")[1]))

    # 2. Appium
    def _check_appium():
        result = subprocess.run(["npx", "appium", "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return f"Appium {result.stdout.strip()}"
        raise Exception("Appium not found")
    components.append(_check("Appium", _check_appium))

    # 3. ADB
    def _check_adb():
        if not shutil.which("adb"):
            raise Exception("adb not in PATH")
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5)
        lines = [l for l in result.stdout.splitlines() if "\t" in l]
        return f"{len(lines)} device(s) connected"
    components.append(_check("ADB / Android Bridge", _check_adb))

    # 4. Git
    def _check_git():
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            raise Exception("git not found")
        return result.stdout.strip()
    components.append(_check("Git", _check_git))

    # 5. Execution Agents
    def _check_agents():
        agents = db.query(ExecutionAgent).all()
        online = [a for a in agents if a.status in ("online", "busy")]
        if not agents:
            raise Exception("No agents registered")
        return f"{len(online)}/{len(agents)} agents online"
    components.append(_check("Execution Agents", _check_agents))

    # 6. Repositories
    def _check_repos():
        projects = db.query(TestProject).count()
        repos_dir = os.path.abspath("repos")
        cloned = sum(1 for p in db.query(TestProject).all() if os.path.isdir(os.path.join(repos_dir, p.id)))
        return f"{projects} projects, {cloned} cloned"
    components.append(_check("Repositories", _check_repos))

    # 7. Reports directory
    def _check_reports():
        reports_dir = os.path.abspath("reports")
        if not os.path.isdir(reports_dir):
            raise Exception(f"Reports directory not found: {reports_dir}")
        count = len([f for f in os.listdir(reports_dir) if f.endswith(".html")])
        return f"{count} HTML report(s) in {reports_dir}"
    components.append(_check("Reports Storage", _check_reports))

    # 8. LLM Provider
    def _check_llm():
        from automation.ai.provider import default_config
        provider_type = default_config.get("type", "mock")
        if provider_type == "mock":
            raise Exception("Using MockLLMProvider — set LLM_PROVIDER_TYPE env var for real AI")
        return f"Provider: {provider_type}"
    components.append(_check("LLM / AI Provider", _check_llm))

    # 9. OpenAI Key
    def _check_openai():
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key or key == "dummy":
            raise Exception("OPENAI_API_KEY not set or is placeholder")
        return "API key configured (masked)"
    components.append(_check("OpenAI API Key", _check_openai))

    # 10. GitHub Token
    def _check_github():
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise Exception("GITHUB_TOKEN not configured — GitHub notifications disabled")
        return "Token configured (masked)"
    components.append(_check("GitHub Token", _check_github))

    ok = sum(1 for c in components if c["status"] == "ok")
    total = len(components)
    health_score = round((ok / total) * 100)

    return {
        "health_score": health_score,
        "components": components,
        "timestamp": datetime.utcnow().isoformat(),
        "verdict": "Healthy" if health_score >= 80 else "Degraded" if health_score >= 50 else "Critical"
    }

@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Get platform-wide utilization metrics"""
    total_runs = db.query(TestRun).count()
    passed_runs = db.query(TestRun).filter(TestRun.status == 'passed').count()
    failed_runs = db.query(TestRun).filter(TestRun.status == 'failed').count()
    
    agents = db.query(ExecutionAgent).all()
    active_agents = len([a for a in agents if a.status == "online" or a.status == "busy"])
    
    pass_rate = (passed_runs / total_runs * 100) if total_runs > 0 else 0
    
    return {
        "platform_health": "Optimal" if pass_rate > 80 else "Degraded",
        "running_jobs": db.query(TestRun).filter(TestRun.status == 'running').count(),
        "queued_jobs": db.query(TestRun).filter(TestRun.job_state == 'queued').count(),
        "pass_rate": round(pass_rate, 2),
        "failure_rate": round(100 - pass_rate, 2) if total_runs > 0 else 0,
        "total_executions": total_runs,
        "active_agents": active_agents,
        "total_agents": len(agents),
        "system_uptime": "Live"
    }

@router.get("/alerts")
def get_alerts(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Get active system alerts"""
    alerts = db.query(SystemAlert).filter(SystemAlert.status == "active").order_by(SystemAlert.created_at.desc()).all()
    return {"alerts": alerts}

@router.get("/agents")
def get_agents(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """List all agents and their metrics"""
    agents = db.query(ExecutionAgent).all()
    return {"agents": agents}

@router.post("/agents/{agent_id}/drain")
def drain_agent(agent_id: str, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Mark an agent to drain (stop accepting new jobs)"""
    agent = db.query(ExecutionAgent).filter(ExecutionAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent.is_draining = True
    db.commit()
    
    audit = AuditLog(user_id=current_user.username, action="DRAIN_AGENT", resource_type="ExecutionAgent", resource_id=agent_id)
    db.add(audit)
    db.commit()
    return {"status": "draining"}

@router.get("/audit")
def get_audit_logs(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Get audit logs"""
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(100).all()
    return {"logs": logs}


