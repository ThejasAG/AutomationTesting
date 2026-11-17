from automation.ai.services.reporting import AIBugReportGenerator, BugReport

def test_reporting_services():
    bug_gen = AIBugReportGenerator()
    bug = bug_gen.generate({"error": "test failed"}, {"logs": "..."})
    assert isinstance(bug, BugReport)
