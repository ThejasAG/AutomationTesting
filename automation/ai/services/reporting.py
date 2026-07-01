from typing import Dict, Any
from pydantic import BaseModel
from automation.ai.providers import get_provider

class BugReport(BaseModel):
    title: str
    description: str
    reproduction_steps: list[str]
    expected_result: str
    actual_result: str
    severity: str

class PRReview(BaseModel):
    approved: bool
    comments: list[str]
    risk_score: int

class AIBugReportGenerator:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def generate(self, failed_run: Dict[str, Any], evidence: Dict[str, Any]) -> BugReport:
        prompt = f"Generate bug report for failure:\n{failed_run}"
        return self.provider.generate_json(prompt, schema=BugReport)

class AIPRReviewer:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def review(self, diff: str, test_results: str) -> PRReview:
        prompt = f"Review PR diff:\n{diff[:2000]}\nTests:\n{test_results}"
        return self.provider.generate_json(prompt, schema=PRReview)
