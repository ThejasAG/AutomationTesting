from typing import List, Dict, Any
from pydantic import BaseModel
from automation.ai.providers import get_provider

class ImpactReport(BaseModel):
    impacted_modules: List[str]
    risk_level: str
    suggested_tests: List[str]

class FlakyAnalysis(BaseModel):
    is_flaky: bool
    confidence: float
    reason: str

class CoverageReport(BaseModel):
    coverage_percentage: float
    missing_areas: List[str]

class Cluster(BaseModel):
    cluster_name: str
    failure_ids: List[str]
    common_root_cause: str

class AIGitImpactAnalyzer:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def analyze(self, commit_diff: str) -> ImpactReport:
        prompt = f"Analyze git diff:\n{commit_diff[:2000]}"
        return self.provider.generate_json(prompt, schema=ImpactReport)

class AITestRecommendationEngine:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def recommend(self, impact_report: ImpactReport) -> List[str]:
        prompt = f"Recommend tests for impact:\n{impact_report.model_dump_json()}"
        res = self.provider.generate(prompt)
        return ["test_login", "test_checkout"] # dummy

class AIFlakyTestDetector:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def analyze(self, test_history: List[Dict[str, Any]]) -> FlakyAnalysis:
        prompt = f"Analyze history for flakiness:\n{test_history}"
        return self.provider.generate_json(prompt, schema=FlakyAnalysis)

class AITestCoverageAnalyzer:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def analyze(self, codebase: str, tests: str) -> CoverageReport:
        prompt = f"Analyze coverage."
        return self.provider.generate_json(prompt, schema=CoverageReport)

class AIFailureClusteringEngine:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def cluster(self, failures: List[Dict[str, Any]]) -> List[Cluster]:
        return [Cluster(cluster_name="Network Errors", failure_ids=[], common_root_cause="Timeout")]
