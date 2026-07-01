import logging
from typing import Dict, Any

from automation.ai.provider import llm_provider

logger = logging.getLogger(__name__)

class AIChatAssistant:
    def __init__(self):
        self.system_prompt = """
        You are an AI Test Intelligence Assistant for a Mobile Automation Platform.
        Your goal is to help QA engineers and developers understand test failures, 
        flaky tests, module stability, and execution history.
        Provide concise, actionable insights.
        """
        
    def ask(self, query: str, context: Dict[str, Any] = None) -> str:
        """Processes a natural language query with context."""
        ctx_str = f"Context: {context}" if context else "No specific context provided."
        
        prompt = f"{self.system_prompt}\n\n{ctx_str}\n\nUser Question: {query}"
        
        try:
            return llm_provider.generate(prompt)
        except Exception as e:
            logger.error(f"ChatAssistant failed: {e}")
            return "I'm currently unable to process your request due to a backend error."

chat_assistant = AIChatAssistant()
