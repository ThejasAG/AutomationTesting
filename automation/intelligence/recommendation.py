import logging
from typing import List, Dict, Any

from automation.intelligence.git_analyzer import git_analyzer
from automation.ai.provider import llm_provider

logger = logging.getLogger(__name__)

class TestRecommendationEngine:
    def __init__(self):
        # Default mapping of Module -> Test Suites
        self.default_test_map = {
            "Login": ["test_login.py", "test_auth.py", "test_security.py"],
            "Registration": ["test_registration.py", "test_onboarding.py"],
            "Checkout": ["test_checkout.py", "test_cart.py"],
            "Profile": ["test_profile.py", "test_settings.py"]
        }
        
    def generate_recommendations(self, repo_path: str, commit_sha: str = "HEAD", config_map: Dict = None) -> Dict[str, Any]:
        """Analyzes a git diff and recommends tests with an AI explanation."""
        changed_files = git_analyzer.extract_changed_files(repo_path, commit_sha)
        affected_modules = git_analyzer.identify_modules(changed_files)
        
        mapping = config_map or self.default_test_map
        
        recommended_tests = set()
        for module in affected_modules:
            if module in mapping:
                recommended_tests.update(mapping[module])
                
        # If no modules mapped but files changed, recommend a generic smoke test
        if not recommended_tests and changed_files:
            recommended_tests.add("test_smoke.py")
            
        # Generate AI Explanation
        explanation = self._generate_ai_explanation(changed_files, affected_modules, list(recommended_tests))
        
        return {
            "changed_files": changed_files,
            "affected_modules": affected_modules,
            "recommended_tests": list(recommended_tests),
            "explanation": explanation
        }
        
    def _generate_ai_explanation(self, files: List[str], modules: List[str], tests: List[str]) -> str:
        if not files:
            return "No file changes detected, skipping recommendation logic."
            
        prompt = f"""
        You are an AI Test Intelligence Engine.
        The developer just modified the following files: {', '.join(files)}
        This affected the application modules: {', '.join(modules) if modules else 'Unknown'}
        Based on this, the system recommended running: {', '.join(tests) if tests else 'Smoke tests'}
        
        Provide a concise, 1-2 sentence explanation of WHY these tests were recommended based on the changed files.
        Example: "Recommended Login tests because AuthService.kt was modified."
        """
        try:
            # We use the existing llm_provider
            response = llm_provider.generate(prompt)
            return response.strip()
        except Exception as e:
            logger.error(f"Failed to generate recommendation explanation: {e}")
            return f"Recommended these tests because files in {', '.join(modules) if modules else 'the repository'} were changed."

recommendation_engine = TestRecommendationEngine()
