from typing import List
from pydantic import BaseModel
from automation.ai.providers import get_provider

class Action(BaseModel):
    action_type: str
    target: str
    value: str = ""

class AILocatorOptimizer:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def optimize(self, broken_locator: str, dom_context: str) -> str:
        prompt = f"Optimize this broken locator: {broken_locator}\nDOM:\n{dom_context[:2000]}"
        return self.provider.generate(prompt)

class AISelfHealingEngine:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def heal(self, error: str, screen_state: str) -> Action:
        prompt = f"Self-heal this error: {error}\nScreen:\n{screen_state[:2000]}"
        return self.provider.generate_json(prompt, schema=Action)

class AIExploratoryAgent:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def explore(self, app_state: str, goal: str) -> Action:
        prompt = f"Goal: {goal}\nState: {app_state[:2000]}\nNext action?"
        return self.provider.generate_json(prompt, schema=Action)
