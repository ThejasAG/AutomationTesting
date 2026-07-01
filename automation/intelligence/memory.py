import logging
from typing import List, Dict, Any
from automation.database.config import SessionLocal
from automation.database import database

logger = logging.getLogger(__name__)

class FailureMemory:
    def __init__(self):
        pass
        
    def search_similar_failures(self, error_message: str, test_name: str, limit: int = 3) -> List[Dict[str, Any]]:
        """
        MVP Semantic Search: Uses basic token matching against historical RCA reports
        to find similar prior failures.
        """
        if not error_message:
            return []
            
        with SessionLocal() as db:
            reports = database.get_all_rca_reports(db, limit=100)
            
        # Basic BM25-like token overlap scoring
        query_tokens = set(error_message.lower().replace('\n', ' ').split())
        
        scored_reports = []
        for r in reports:
            # We skip exactly identical runs or empty root causes
            if not r.root_cause:
                continue
                
            doc_tokens = set(r.root_cause.lower().replace('\n', ' ').split())
            overlap = len(query_tokens.intersection(doc_tokens))
            
            # Boost if it's the same test
            if r.run and r.run.test_name == test_name:
                overlap += 5
                
            if overlap > 0:
                scored_reports.append((overlap, r))
                
        # Sort by overlap score descending
        scored_reports.sort(key=lambda x: x[0], reverse=True)
        
        results = []
        for score, r in scored_reports[:limit]:
            results.append({
                "run_id": r.run_id,
                "root_cause": r.root_cause,
                "suggested_fix": r.suggested_fix,
                "similarity_score": score,
                "generated_at": r.generated_at
            })
            
        return results

failure_memory = FailureMemory()
