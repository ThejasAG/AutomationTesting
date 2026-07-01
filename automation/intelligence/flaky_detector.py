import logging
from typing import List, Dict, Any
from automation.database.config import SessionLocal
from automation.database import database

logger = logging.getLogger(__name__)

class FlakyTestDetector:
    def __init__(self):
        pass
        
    def calculate_flakiness(self, test_name: str, history_limit: int = 15) -> Dict[str, Any]:
        """
        Analyzes the last N runs of a test to determine a confidence score
        and flag if it's considered flaky.
        """
        with SessionLocal() as db:
            runs = database.get_historical_test_runs(db, test_name, limit=history_limit)
            
        if not runs:
            return {"test_name": test_name, "is_flaky": False, "confidence": 100.0, "reason": "No history"}
            
        total_runs = len(runs)
        passed_runs = sum(1 for r in runs if r.status == "passed")
        
        # A test is extremely flaky if it toggles between pass and fail
        toggles = 0
        for i in range(1, total_runs):
            if runs[i].status != runs[i-1].status:
                toggles += 1
                
        pass_rate = (passed_runs / total_runs) * 100
        
        # Confidence logic:
        # High toggles = Low confidence. Low pass rate without toggles = Stable failure (not flaky, just broken).
        confidence = max(0.0, 100.0 - (toggles * 15.0))
        if pass_rate < 50 and toggles == 0:
            confidence = 80.0 # It consistently fails, we are confident it's broken
            
        is_flaky = confidence < 70.0
        
        return {
            "test_name": test_name,
            "is_flaky": is_flaky,
            "confidence": round(confidence, 1),
            "pass_rate": round(pass_rate, 1),
            "toggles": toggles,
            "total_evaluated": total_runs,
            "reason": f"Toggled {toggles} times in last {total_runs} runs." if is_flaky else "Stable execution pattern."
        }

flaky_detector = FlakyTestDetector()
