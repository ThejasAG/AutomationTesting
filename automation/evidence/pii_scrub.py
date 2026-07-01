"""PII/Secret Scrubbing for GDPR Compliance"""

import re
from typing import Any


PII_PATTERNS = {
    "email": (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL]"),
    "phone": (r"\b(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})\b", "[PHONE]"),
    "ssn": (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
    "credit_card": (r"\b(?:\d[ -]*?){13,16}\b", "[CARD]"),
    "api_key": (r"\b(api[_-]?key|apikey|secret)[\s=:]+['\"]?[a-zA-Z0-9_-]{20,}['\"]?\b", "[API_KEY]"),
    "bearer_token": (r"\bBearer\s+[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b", "[TOKEN]"),
    "password": (r"\b(password|passwd|pwd)[\s=:]+['\"]?[^\s'\"]{4,}['\"]?\b", "[PASSWORD]"),
    "ip_address": (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]"),
}

CUSTOM_PATTERNS: dict[str, tuple[str, str]] = {}


def scrub_text(text: str, custom_patterns: dict[str, tuple[str, str]] = None) -> str:
    """Scrub PII and secrets from text"""
    patterns = {**PII_PATTERNS, **(custom_patterns or {})}

    for name, (pattern, replacement) in patterns.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    return text


def scrub_dict(data: dict[str, Any], custom_patterns: dict[str, tuple[str, str]] = None) -> dict[str, Any]:
    """Recursively scrub PII from dictionary values"""
    import copy

    result = copy.deepcopy(data)

    for key, value in result.items():
        if isinstance(value, str):
            result[key] = scrub_text(value, custom_patterns)
        elif isinstance(value, dict):
            result[key] = scrub_dict(value, custom_patterns)
        elif isinstance(value, list):
            result[key] = [scrub_text(v, custom_patterns) if isinstance(v, str) else v for v in value]

    return result


def scrub_logs(logs: list[dict], custom_patterns: dict[str, tuple[str, str]] = None) -> list[dict]:
    """Scrub PII from log entries"""
    import copy

    result = []
    for entry in logs:
        scrubbed = copy.deepcopy(entry)
        if "message" in scrubbed:
            scrubbed["message"] = scrub_text(scrubbed["message"], custom_patterns)
        result.append(scrubbed)
    return result


class PIIConfig:
    """Configuration for PII scrubbing patterns"""

    def __init__(self, custom_patterns: dict[str, tuple[str, str]] = None):
        self.custom_patterns = custom_patterns or {}

    def add_pattern(self, name: str, pattern: str, replacement: str):
        """Add custom regex pattern for scrubbing"""
        self.custom_patterns[name] = (pattern, replacement)

    def scrub(self, data: Any) -> Any:
        """Apply all scrubbing patterns to data"""
        if isinstance(data, str):
            return scrub_text(data, self.custom_patterns)
        elif isinstance(data, dict):
            return scrub_dict(data, self.custom_patterns)
        elif isinstance(data, list):
            return scrub_logs(data, self.custom_patterns)
        return data