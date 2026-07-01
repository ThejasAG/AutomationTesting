"""LLM Prompt Templates for Root Cause Analysis"""

RCA_SYSTEM_PROMPT = """You are an expert mobile test automation engineer. Your task is to analyze test failures and provide a comprehensive root cause analysis.

Analyze the evidence provided and return a structured JSON response with:
- root_cause: A concise description of the most likely root cause (max 200 chars).
- failure_category: Select ONLY from [Locator Changed, Timeout, API 500, Network Timeout, App Crash, UI Regression, Synchronization].
- affected_modules: List of source files or modules most likely causing the failure.
- confidence: Confidence score between 0.0 and 1.0 (e.g. 0.97).
- possible_reason: Detailed possible reason based on the provided logs and diffs. Include flaky detection logic if historical runs are provided.
- impact: Business or technical impact of this failure.
- suggested_fix: A detailed string containing the Recommended Fix, a Code Example (where appropriate), and Best Practices. Use bullet points and newlines.
- priority: High, Medium, or Low.
- severity: Critical, Major, Minor.
- responsible_module: High-level component or team responsible for the failure.
- summary: Plain English explanation for developers/test engineers.

Focus on:
1. UI element locators and timing/synchronization issues.
2. Git diffs connecting recent code changes to the failure.
3. Historical failures (if provided, correlate this failure with past patterns).
4. App state management and navigation flows.
5. Device/emulator specific behaviors.

CRITICAL INSTRUCTION FOR GIT ANALYSIS:
If a git diff or commit is provided in the evidence, you MUST correlate the failure to the code changes. For example, your possible_reason or root_cause should explicitly state: "This failure started after commit <hash>. The latest change modified <filename>, making it likely that..." Do not just give a generic Appium error if a code change explains it.

CRITICAL INSTRUCTION FOR FIX:
Format suggested_fix exactly like this:
Recommended fix:
<your fix here>

Code example:
```python
<code here>
```

Best practice:
- <best practice 1>
- <best practice 2>

Do NOT include PII or sensitive data in your response."""

RCA_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "rca_analysis",
        "schema": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string", "maxLength": 200},
                "failure_category": {"type": "string"},
                "affected_modules": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "possible_reason": {"type": "string"},
                "impact": {"type": "string"},
                "suggested_fix": {"type": "string"},
                "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                "severity": {"type": "string", "enum": ["Critical", "Major", "Minor"]},
                "responsible_module": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["root_cause", "failure_category", "affected_modules", "confidence", "possible_reason", "impact", "suggested_fix", "priority", "severity", "responsible_module", "summary"],
        },
    },
}

def build_rca_prompt(evidence: dict) -> str:
    """Build user prompt from evidence bundle"""
    return f"""Analyze this Appium test failure:

Test Name: {evidence.get("test_name", "unknown")}
Error: {evidence.get("error_message", "")}
Failed Locator: {evidence.get("failed_locator", "unknown")}

Git Metadata:
Commit: {evidence.get("git_commit", "unknown")}
Author: {evidence.get("git_author", "unknown")}
Branch: {evidence.get("git_branch", "unknown")}

Git Changes (since last passing run):
{evidence.get("git_diff", "No changes detected")}

Historical Flakiness Context:
{evidence.get("historical_failures", "No historical failures found.")}

Appium Log Excerpts:
{chr(10).join(str(log) for log in evidence.get("appium_logs", []))}

Device Logs Excerpt:
{evidence.get("device_logs", "No device logs")[:1000]}

XML Page Source Excerpt:
{str(evidence.get("xml_page_source", ""))[:2000]}

Screenshot Summary:
{evidence.get("screenshot_summary", "No screenshot available")}
"""