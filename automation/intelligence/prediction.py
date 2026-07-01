import logging
from typing import Dict, Any

from automation.database.config import SessionLocal
from automation.database import database

logger = logging.getLogger(__name__)

class FailurePredictionEngine:
    def __init__(self):
        pass
        
    def predict_risk(self, project_id: str, branch: str = "main") -> Dict[str, Any]:
        """
        Calculates a Risk Score (0-100) for an upcoming execution.
        Based on recent flakiness, module stability, and previous run statuses.
        """
        with SessionLocal() as db:
            runs = db.query(database.TestRun).filter(
                database.TestRun.project_id == project_id
            ).order_by(database.TestRun.created_at.desc()).limit(10).all()
            
        if not runs:
            return {"risk_score": 10, "confidence": 50, "reason": "Not enough historical data."}
            
        recent_fails = sum(1 for r in runs if r.status == "failed")
        flaky_runs = sum(1 for r in runs if r.is_flaky)
        
        # Base risk: 20%
        # +10% per recent failure (max 50)
        # +15% per flaky run (max 30)
        risk_score = 20 + min(50, recent_fails * 10) + min(30, flaky_runs * 15)
        
        confidence = min(100, len(runs) * 10)
        
        reason = "Stable history."
        if risk_score > 70:
            reason = f"High risk due to {recent_fails} recent failures and {flaky_runs} flaky runs."
        elif risk_score > 40:
            reason = "Moderate risk based on recent instability."
            
        return {
            "risk_score": min(100, risk_score),
            "confidence": confidence,
            "reason": reason
        }

prediction_engine = FailurePredictionEngine()
