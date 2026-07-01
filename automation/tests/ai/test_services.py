import pytest
from automation.ai.services.generator import AITestGenerator, AIPageObjectGenerator
from automation.ai.services.execution import AILocatorOptimizer, AISelfHealingEngine, AIExploratoryAgent, Action
from automation.ai.services.intelligence import AIGitImpactAnalyzer, AITestRecommendationEngine, AIFlakyTestDetector, AITestCoverageAnalyzer, AIFailureClusteringEngine, ImpactReport
from automation.ai.services.reporting import AIBugReportGenerator, AIPRReviewer, BugReport
from automation.ai.services.chat import AIChatAssistant, Message
from automation.ai.services.knowledge import AIKnowledgeBase, AILearningEngine

def test_generator_services():
    test_gen = AITestGenerator()
    assert "pytest" in test_gen.generate_test("Login page")
    
    po_gen = AIPageObjectGenerator()
    assert "appium" in po_gen.generate_page_object("<html></html>")

def test_execution_services():
    optimizer = AILocatorOptimizer()
    assert "Response to: Optimize this broken locator: " in optimizer.optimize("id=wrong", "<html/>")
    
    healer = AISelfHealingEngine()
    action = healer.heal("ElementNotFound", "<html/>")
    # Our mock BaseModel construct uses default values for str
    assert isinstance(action, Action)

def test_intelligence_services():
    analyzer = AIGitImpactAnalyzer()
    report = analyzer.analyze("diff --git")
    assert isinstance(report, ImpactReport)
    
    recommender = AITestRecommendationEngine()
    rec = recommender.recommend(report)
    assert isinstance(rec, list)

def test_reporting_services():
    bug_gen = AIBugReportGenerator()
    bug = bug_gen.generate({"error": "test failed"}, {"logs": "..."})
    assert isinstance(bug, BugReport)
    
def test_chat_services():
    chat = AIChatAssistant()
    msg = chat.chat([Message(role="user", content="Hi")], "No context")
    assert msg.role == "assistant"
    assert "Response to: Context: No context" in msg.content

def test_knowledge_services():
    kb = AIKnowledgeBase()
    assert "Response to: Query knowledge base: What is" in kb.query("What is Appium?")
    
    learning = AILearningEngine()
    assert learning.ingest_resolution("Error XYZ", "Fix XYZ") is True
