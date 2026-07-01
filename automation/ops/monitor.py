import threading
import time
from datetime import datetime, timedelta
from automation.database.config import SessionLocal
from automation.database.models import ExecutionAgent, SystemAlert, TestRun

class OpsMonitor:
    def __init__(self):
        self.running = False
        self.thread = None

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            print("OpsMonitor started in background.")

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self.check_agents()
                self.check_queues()
            except Exception as e:
                print(f"OpsMonitor error: {e}")
            time.sleep(30) # run every 30 seconds
            
    def check_agents(self):
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            threshold = now - timedelta(seconds=60) # 1 minute timeout
            
            # Find agents that were online but haven't sent a heartbeat
            stale_agents = db.query(ExecutionAgent).filter(
                ExecutionAgent.status.in_(["online", "busy"]),
                ExecutionAgent.last_heartbeat < threshold
            ).all()
            
            for agent in stale_agents:
                agent.status = "offline"
                # Create an alert
                alert = SystemAlert(
                    type="agent_offline",
                    severity="critical",
                    message=f"Agent {agent.hostname} (ID: {agent.id}) missed heartbeat and went offline."
                )
                db.add(alert)
                
            db.commit()
        finally:
            db.close()
            
    def check_queues(self):
        db = SessionLocal()
        try:
            queued_count = db.query(TestRun).filter(TestRun.job_state == "queued").count()
            if queued_count > 50: # Threshold for overflow
                # Check if an alert already exists
                existing = db.query(SystemAlert).filter(
                    SystemAlert.type == "queue_overflow", 
                    SystemAlert.status == "active"
                ).first()
                
                if not existing:
                    alert = SystemAlert(
                        type="queue_overflow",
                        severity="warning",
                        message=f"Queue overflow: {queued_count} jobs are waiting in the queue."
                    )
                    db.add(alert)
                    db.commit()
            else:
                # Resolve existing alerts if queue drops below 50
                existing = db.query(SystemAlert).filter(
                    SystemAlert.type == "queue_overflow", 
                    SystemAlert.status == "active"
                ).all()
                for alert in existing:
                    alert.status = "resolved"
                    alert.resolved_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

ops_monitor = OpsMonitor()
