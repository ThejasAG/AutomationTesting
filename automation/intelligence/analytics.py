import logging
from typing import Dict, Any
from automation.database.config import SessionLocal
from automation.database import database

logger = logging.getLogger(__name__)

class AnalyticsService:
    def __init__(self):
        pass
        
    def get_global_metrics(self) -> Dict[str, Any]:
        with SessionLocal() as db:
            runs = db.query(database.TestRun).all()
            
        total = len(runs)
        if total == 0:
            return {"total_runs": 0, "pass_rate": 0, "avg_duration_ms": 0}
            
        passed = sum(1 for r in runs if r.status == "passed")
        durations = [r.duration_ms for r in runs if r.duration_ms]
        
        avg_dur = sum(durations) / len(durations) if durations else 0
        
        return {
            "total_runs": total,
            "pass_rate": round((passed / total) * 100, 1),
            "avg_duration_ms": round(avg_dur, 0),
            "failed_runs": total - passed
        }

analytics_service = AnalyticsService()
