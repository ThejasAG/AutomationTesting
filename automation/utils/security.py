"""
Security Utilities - Secret Masking
Prevents accidental exposure of tokens, API keys, and credentials in logs/responses.
"""

import re
import os
from typing import Any, Dict

# ── Patterns that identify secrets ─────────────────────────────────────────
_SECRET_PATTERNS = [
    # JWT tokens
    (re.compile(r'(eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.?[A-Za-z0-9\-_.+/=]*)'), "***JWT***"),
    # Bearer tokens
    (re.compile(r'(Bearer\s+)[A-Za-z0-9\-_.~+/]+=*', re.IGNORECASE), r'\1***TOKEN***'),
    # GitHub tokens (ghp_, ghs_, github_pat_)
    (re.compile(r'(ghp_|ghs_|github_pat_)[A-Za-z0-9]+'), "***GITHUB_TOKEN***"),
    # Generic API keys (key=..., api_key=..., apikey=...)
    (re.compile(r'(api[_-]?key\s*[=:]\s*)["\']?([A-Za-z0-9\-_\.]{20,})["\']?', re.IGNORECASE), r'\1***MASKED***'),
    # OpenAI keys
    (re.compile(r'(sk-)[A-Za-z0-9]{20,}'), "***OPENAI_KEY***"),
    # Slack webhooks
    (re.compile(r'(https://hooks\.slack\.com/services/)[A-Za-z0-9/]+'), r'\1***MASKED***'),
    # Basic Auth passwords in URLs
    (re.compile(r'(https?://[^:]+:)[^@]+(@)'), r'\1***@\2'),
    # Jira API token patterns
    (re.compile(r'(jira.*token\s*[=:]\s*)["\']?([A-Za-z0-9\-_]{10,})["\']?', re.IGNORECASE), r'\1***MASKED***'),
]

# ── Dictionary keys whose values should always be masked ───────────────────
_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "private_key", "jwt",
    "authorization", "auth_token", "github_token", "jira_token",
    "slack_webhook", "openai_api_key", "azure_api_key", "client_secret"
}


def mask_string(text: str) -> str:
    """Apply all secret masking patterns to a string."""
    if not isinstance(text, str):
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_dict(data: Dict[str, Any], depth: int = 5) -> Dict[str, Any]:
    """
    Recursively mask sensitive values in a dictionary.
    Keys matching _SENSITIVE_KEYS will have their values replaced with ***MASKED***.
    """
    if depth <= 0 or not isinstance(data, dict):
        return data
    
    masked = {}
    for key, value in data.items():
        if key.lower() in _SENSITIVE_KEYS:
            masked[key] = "***MASKED***"
        elif isinstance(value, dict):
            masked[key] = mask_dict(value, depth - 1)
        elif isinstance(value, list):
            masked[key] = [
                mask_dict(item, depth - 1) if isinstance(item, dict)
                else mask_string(item) if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            masked[key] = mask_string(value)
        else:
            masked[key] = value
    return masked


def safe_env() -> Dict[str, str]:
    """
    Returns os.environ with all sensitive values masked.
    Use this when logging environment variables.
    """
    return {k: mask_string(v) if k.upper() in {s.upper() for s in _SENSITIVE_KEYS} else v
            for k, v in os.environ.items()}


class SecretFilter:
    """
    A logging filter that applies mask_string to all log messages.
    Install it on a logger to automatically redact secrets from logs.
    """
    import logging

    def filter(self, record):
        import logging
        if isinstance(record.msg, str):
            record.msg = mask_string(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: mask_string(v) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    mask_string(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def install_secret_filter(logger_name: str = ""):
    """Redact secrets from logs, including those from named child loggers.

    A filter on a Logger only sees records logged through THAT logger — records
    propagating up from children (logging.getLogger("builds"), "api", "deploy",
    which is how every module here logs) bypass it entirely. Handlers see every
    propagated record, so the filter has to go on the handlers to be worth
    anything.

    ponytail: covers handlers present at install time. install_secret_filter()
    again if a later component (e.g. a new log file) adds its own handler.
    """
    import logging
    f = SecretFilter()
    log = logging.getLogger(logger_name)
    log.addFilter(f)                       # direct records to this logger
    for h in log.handlers:                 # everything propagated from children
        if not any(isinstance(x, SecretFilter) for x in h.filters):
            h.addFilter(f)
    return f
