"""Main RCA Service - Coordinates evidence collection and LLM analysis"""

from typing import Any
import json

from .provider import create_provider, RCAAnalysis
from .prompts import RCA_SYSTEM_PROMPT, RCA_OUTPUT_SCHEMA, build_rca_prompt
from automation.evidence.pii_scrub import PIIConfig


class RCAService:
    """Root Cause Analysis Service"""

    def __init__(self, provider_config: dict[str, Any], pii_config: PIIConfig = None):
        self.provider = create_provider(provider_config)
        self.pii_config = pii_config or PIIConfig()
        self.prompt_version = "1.0.0"

    def analyze(self, evidence: dict[str, Any]) -> RCAAnalysis:
        """Perform RCA on test failure evidence"""
        # Scrub PII before sending to LLM
        scrubbed_evidence = self.pii_config.scrub(evidence)

        # Build prompt
        user_prompt = build_rca_prompt(scrubbed_evidence)

        # Call LLM
        response = self.provider.generate(
            system_prompt=RCA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            json_schema=RCA_OUTPUT_SCHEMA,
            temperature=0.1,
            max_tokens=2000,
        )

        # Parse JSON response
        try:
            parsed = json.loads(response.content)
        except json.JSONDecodeError:
            # Fallback for non-JSON responses
            parsed = {
                "root_cause": response.content[:200],
                "failure_category": "Unknown",
                "affected_modules": [],
                "confidence": 0.5,
                "possible_reason": "Failed to parse LLM response format.",
                "impact": "Unknown",
                "suggested_fix": "Manual analysis required",
                "priority": "Medium",
                "severity": "Minor",
                "responsible_module": "Unknown",
                "summary": response.content,
            }

        return RCAAnalysis(
            root_cause=parsed.get("root_cause", ""),
            failure_category=parsed.get("failure_category", "Unknown"),
            affected_modules=parsed.get("affected_modules", []),
            confidence=parsed.get("confidence", 0.0),
            possible_reason=parsed.get("possible_reason", ""),
            impact=parsed.get("impact", ""),
            suggested_fix=parsed.get("suggested_fix", ""),
            priority=parsed.get("priority", "Medium"),
            severity=parsed.get("severity", "Minor"),
            responsible_module=parsed.get("responsible_module", ""),
            summary=parsed.get("summary", ""),
            evidence_used=scrubbed_evidence,
        )