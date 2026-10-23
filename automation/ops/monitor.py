import threading
import time
from datetime import datetime, timedelta
from automation.database.config import SessionLocal
from automation.database.models import ExecutionAgent, SystemAlert, TestRun

# Job states that mean an agent is supposed to be actively working on the job.
# A job sitting in one of these with no live agent behind it is stranded: poll_job
# only ever hands out `queued`, so nothing can recover it on its own.
IN_FLIGHT_JOB_STATES = ("assigned", "downloading", "preparing", "running",
                        "collecting_evidence")

# Same window check_agents() uses to declare an agent offline — one mechanism,
# not two disagreeing ones.
AGENT_STALE_AFTER = timedelta(seconds=60)

# Give up after this many total attempts, so a job that strands every time it is
# picked up cannot cycle through the queue forever.
MAX_JOB_ATTEMPTS = 3


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
                self.reclaim_stranded_jobs()
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
            
    def reclaim_stranded_jobs(self):
        """Return jobs to the queue when the agent that claimed them is gone.

        Deliberately narrow. A job is only reclaimed when ALL of these hold:

          * its job_state is one an agent is meant to be actively driving
          * it HAS an agent_id — runs started in-process by the backend (the
            Scenarios tab writes job_state='running' with no agent) are never
            touched, because no agent is coming back for them and requeueing one
            would hand a backend run to the agent
          * that agent is genuinely stale: missing, marked offline, or past the
            same heartbeat window check_agents() uses

        A healthy agent's running job is left completely alone.
        """
        db = SessionLocal()
        reclaimed = []
        try:
            threshold = datetime.utcnow() - AGENT_STALE_AFTER
            candidates = db.query(TestRun).filter(
                TestRun.job_state.in_(IN_FLIGHT_JOB_STATES),
                TestRun.agent_id.isnot(None),
            ).all()

            for job in candidates:
                agent = db.query(ExecutionAgent).filter(
                    ExecutionAgent.id == job.agent_id
                ).first()
                agent_is_stale = (
                    agent is None
                    or agent.status == "offline"
                    or agent.last_heartbeat is None
                    or agent.last_heartbeat < threshold
                )
                if not agent_is_stale:
                    continue                      # healthy agent — hands off

                was_state = job.job_state
                lost_agent = job.agent_id
                attempts = job.attempts or 1

                if attempts >= MAX_JOB_ATTEMPTS:
                    # Stop cycling it; record why rather than leaving it stuck.
                    job.job_state = "failed"
                    job.status = "failed"
                    job.error_message = (
                        f"Abandoned after {attempts} attempts: the agent running it "
                        f"({lost_agent}) stopped responding while the job was "
                        f"'{was_state}'."
                    )
                    severity, verb = "critical", "abandoned"
                else:
                    job.job_state = "queued"
                    job.status = "queued"
                    job.agent_id = None           # clear the stale ownership
                    job.attempts = attempts + 1
                    job.error_message = (
                        f"Requeued: the agent running it ({lost_agent}) stopped "
                        f"responding while the job was '{was_state}'."
                    )
                    severity, verb = "warning", "requeued"

                db.add(SystemAlert(
                    type="job_reclaimed",
                    severity=severity,
                    message=(f"Run {job.id} {verb} — agent {lost_agent} went stale "
                             f"while the job was '{was_state}'."),
                ))
                reclaimed.append((job.id, verb))

            if reclaimed:
                db.commit()
                for job_id, verb in reclaimed:
                    print(f"OpsMonitor: {verb} stranded run {job_id}")
        finally:
            db.close()
        return reclaimed

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
