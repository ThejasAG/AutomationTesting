import logging
import os
from typing import List, Dict, Any, Optional

from automation.intelligence.git_analyzer import git_analyzer
from automation.ai.provider import llm_provider

logger = logging.getLogger(__name__)

# Where a project's suite lives inside its clone.
_TEST_DIRS = ("e2e", "tests", "test")


class TestRecommendationEngine:
    def __init__(self):
        # Module -> candidate test files. These are CANDIDATES: every name is
        # filtered against the files actually present in the clone before it is
        # recommended. The map used to name test_checkout.py / test_cart.py /
        # test_smoke.py, none of which exist on disk, so a payment PR selected a
        # suite of phantom files and ran nothing while reporting success.
        self.default_test_map = {
            "Login": ["test_login.py", "test_auth.py", "test_security.py"],
            "Registration": ["test_registration.py", "test_onboarding.py"],
            "Payment": ["test_payment.py", "test_checkout.py"],
            "Cart": ["test_cart.py", "test_payment.py"],
            "Booking": ["test_preorder.py", "test_booking.py"],
            "Checkout": ["test_payment.py", "test_checkout.py", "test_cart.py"],
            "Profile": ["test_profile.py", "test_settings.py"],
        }

    # ── existence filtering ──────────────────────────────────────────────────

    def list_existing_tests(self, repo_path: str) -> Dict[str, str]:
        """{filename: repo-relative path} for every test file in the clone."""
        found: Dict[str, str] = {}
        if not repo_path or not os.path.isdir(repo_path):
            return found
        for d in _TEST_DIRS:
            test_dir = os.path.join(repo_path, d)
            if not os.path.isdir(test_dir):
                continue
            for root, dirs, files in os.walk(test_dir):
                dirs[:] = [x for x in dirs if x not in ("__pycache__", ".venv")]
                for f in files:
                    if f.startswith("test_") and f.endswith(".py"):
                        found.setdefault(
                            f, os.path.relpath(os.path.join(root, f), repo_path)
                        )
        return found

    def filter_to_existing(self, tests, repo_path: str):
        """(runnable, missing) — a recommendation for a file that is not there
        is not a test run, it is a silent no-op."""
        present = self.list_existing_tests(repo_path)
        runnable = sorted({present[t] for t in tests if t in present})
        missing = sorted({t for t in tests if t not in present})
        if missing:
            logger.warning(
                "Dropping %d recommended test(s) that do not exist in %s: %s",
                len(missing), repo_path, ", ".join(missing),
            )
        return runnable, missing

    def recommend_for_files(
        self,
        changed_files: List[str],
        repo_path: str,
        config_map: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Changed files -> modules -> tests that ACTUALLY exist in the clone."""
        affected_modules = git_analyzer.identify_modules(changed_files)
        mapping = config_map or self.default_test_map

        candidates = set()
        for module in affected_modules:
            candidates.update(mapping.get(module, []))

        # No module matched, but something changed — fall back to the whole
        # suite rather than a "test_smoke.py" that has never existed here.
        fell_back = False
        if not candidates and changed_files:
            candidates = set(self.list_existing_tests(repo_path).keys())
            fell_back = True

        runnable, missing = self.filter_to_existing(candidates, repo_path)
        return {
            "changed_files": changed_files,
            "affected_modules": affected_modules,
            "recommended_tests": runnable,
            "missing_tests": missing,
            "fell_back_to_full_suite": fell_back,
            "no_coverage": bool(affected_modules) and not runnable,
        }

    def generate_recommendations(self, repo_path: str, commit_sha: str = "HEAD", config_map: Dict = None) -> Dict[str, Any]:
        """Analyzes a git diff and recommends tests that exist in the clone."""
        changed_files = git_analyzer.extract_changed_files(repo_path, commit_sha)
        result = self.recommend_for_files(changed_files, repo_path, config_map)
        result["explanation"] = self._generate_ai_explanation(
            changed_files, result["affected_modules"], result["recommended_tests"]
        )
        return result
        
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
            # generate() takes the system and user prompts separately and returns
            # an LLMResponse — a single positional arg raises TypeError, which is
            # why this always fell through to the except branch.
            response = llm_provider.generate(
                system_prompt="You are an AI Test Intelligence Engine.",
                user_prompt=prompt,
            )
            return response.content.strip()
        except Exception as e:
            logger.error(f"Failed to generate recommendation explanation: {e}")
            return f"Recommended these tests because files in {', '.join(modules) if modules else 'the repository'} were changed."

recommendation_engine = TestRecommendationEngine()
