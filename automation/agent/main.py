import os
import sys
import time
import socket
import platform
import threading
import requests
import logging
from typing import List, Dict, Any

from automation.device_manager.discovery.android import AndroidDiscoveryProvider
from automation.device_manager.discovery.ios import IOSDiscoveryProvider
from automation.plugins.appium_framework import AppiumFramework
from automation.projects.repository import repository_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Agent")

PLATFORM_URL = os.getenv("PLATFORM_URL", "http://localhost:8000/api/v1")
AGENT_ID = None

def get_capabilities() -> Dict[str, Any]:
    return {
        "os": platform.system(),
        "python_version": sys.version.split()[0],
        "frameworks": ["appium"],
    }

def discover_devices() -> List[Dict[str, Any]]:
    providers = [AndroidDiscoveryProvider(), IOSDiscoveryProvider()]
    devices = []
    for p in providers:
        try:
            for d in p.discover_devices():
                devices.append({
                    "id": d.id,
                    "name": d.name,
                    "manufacturer": d.manufacturer,
                    "model": d.model,
                    "platform": d.platform,
                    "platform_version": d.platform_version
                })
        except Exception as e:
            logger.warning(f"Device discovery failed for {p.get_provider_name()}: {e}")
            
    return devices

def heartbeat_loop():
    global AGENT_ID
    while True:
        try:
            if AGENT_ID:
                devices = discover_devices()
                requests.post(
                    f"{PLATFORM_URL}/agents/{AGENT_ID}/heartbeat",
                    json={"status": "online", "connected_devices": devices},
                    timeout=5
                )
        except Exception as e:
            logger.error(f"Heartbeat failed: {e}")
        time.sleep(10)

def report_status(job_id: str, status: str, logs: List[str] = [], timeline_event: str = None, error_message: str = None):
    try:
        requests.post(
            f"{PLATFORM_URL}/jobs/{job_id}/status",
            json={
                "status": status,
                "logs": logs,
                "timeline_event": timeline_event,
                "error_message": error_message
            },
            timeout=5
        )
    except Exception as e:
        logger.error(f"Failed to report status: {e}")

def _upload_evidence_with_retry(job_id: str, evidence_data: dict, max_retries: int = 3):
    """Upload evidence with bounded retry on network failure."""
    import time as _time
    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(
                f"{PLATFORM_URL}/jobs/{job_id}/evidence",
                json={"evidence": evidence_data},
                timeout=15
            )
            if res.status_code == 200:
                return
            logger.warning(f"Evidence upload attempt {attempt} failed (HTTP {res.status_code})")
        except Exception as e:
            logger.warning(f"Evidence upload attempt {attempt} error: {e}")
        if attempt < max_retries:
            _time.sleep(2 ** attempt)  # exponential back-off
    logger.error(f"Evidence upload failed after {max_retries} attempts for job {job_id}")

def run_job(job: Dict[str, Any], connected_devices: List[str]):

    job_id = job["job_id"]
    project_id = job["project_id"]
    git_url = job["git_url"]
    branch = job["branch"]
    device_id = job["device_id"]
    
    if device_id not in connected_devices:
        logger.error(f"Job assigned for device {device_id} but it is no longer connected.")
        report_status(job_id, "failed", ["Device disconnected before execution."], "Failed", "Device disconnected")
        return
        
    try:
        # 0. Pre-execution validation
        from automation.utils.validator import EnvironmentValidator
        validator = EnvironmentValidator(project_id)
        validation = validator.validate_pre_execution(device_id=device_id)
        if not validation.passed:
            error_details = " | ".join(f"{i.check}: {i.resolution}" for i in validation.issues)
            report_status(job_id, "failed",
                         [f"PRE-FLIGHT FAILED: {i.check} — {i.problem}" for i in validation.issues],
                         "Pre-flight Failed",
                         f"Environment validation failed: {error_details}")
            return
        if validation.warnings:
            for w in validation.warnings:
                report_status(job_id, "running", [f"WARNING: {w.check} — {w.problem}"])

        # 1. Sync
        report_status(job_id, "downloading", [f"Synchronizing repository from {git_url} (branch: {branch})..."], "Git Sync")
        if not repository_manager.clone_or_pull(project_id, git_url, branch):
            raise Exception("Git synchronization failed. Check repository URL and credentials.")
            
        config = repository_manager.validate_yaml(project_id)
        if not config:
            raise Exception("automation.yaml validation failed or is missing. Ensure the file exists in the repository root.")
            
        # 2. Prepare
        framework = AppiumFramework()
        report_status(job_id, "preparing", ["Preparing Python virtual environment & dependencies..."], "Env Prepare")
        if not framework.prepare(project_id, device_id):
            raise Exception("Preparation failed. Check venv creation and pip install logs.")
            
        # 3. Execute
        report_status(job_id, "running", ["Executing tests..."], "Executing")
        exec_res = framework.execute(project_id, device_id, {"command": config.execution.command}, job_id)
        
        status = exec_res.get("status", "failed")
        logs = exec_res.get("logs", [])
        
        report_status(job_id, "collecting_evidence", logs, f"Execution {status}")
        
        # 4. Upload Evidence
        evidence_data = framework.collect_evidence(project_id, exec_res)
        evidence_data["run_id"] = job_id
        _upload_evidence_with_retry(job_id, evidence_data)

        final_status = "passed" if status == "passed" else "failed"
        report_status(job_id, final_status, ["Execution Complete."], "Done")
        
    except Exception as e:
        logger.error(f"Job Execution Error: {e}")
        report_status(job_id, "failed", [f"Execution Error: {str(e)}"], "Failed", str(e))
    finally:
        try:
            framework.cleanup(project_id)
        except Exception:
            pass

def poll_loop():
    global AGENT_ID
    while True:
        try:
            devices = [d["id"] for d in discover_devices()]
            if not devices:
                time.sleep(3)
                continue
                
            res = requests.post(
                f"{PLATFORM_URL}/jobs/poll",
                json={"agent_id": AGENT_ID, "connected_devices": devices},
                timeout=5
            )
            if res.status_code == 200:
                job = res.json()
                if job and job.get("job_id"):
                    logger.info(f"Accepted Job: {job['job_id']}")
                    run_job(job, devices)
        except Exception as e:
            logger.error(f"Poll failed: {e}")
        time.sleep(3)

def main():
    global AGENT_ID
    hostname = socket.gethostname()
    os_name = platform.system()
    capabilities = get_capabilities()
    devices = discover_devices()
    
    logger.info(f"Registering agent {hostname}...")
    res = requests.post(
        f"{PLATFORM_URL}/agents/register",
        json={
            "hostname": hostname,
            "os": os_name,
            "capabilities": capabilities,
            "connected_devices": devices
        }
    )
    if res.status_code != 200:
        logger.error(f"Registration failed: {res.text}")
        sys.exit(1)
        
    AGENT_ID = res.json()["id"]
    logger.info(f"Registered successfully. Agent ID: {AGENT_ID}")
    
    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()
    
    logger.info("Polling for jobs...")
    poll_loop()

if __name__ == "__main__":
    main()
