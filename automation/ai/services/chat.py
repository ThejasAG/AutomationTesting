from typing import List, Dict, Any
from pydantic import BaseModel
from automation.ai.providers import get_provider

class Message(BaseModel):
    role: str
    content: str

class AIChatAssistant:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def chat(self, history: List[Message], context: str) -> Message:
        prompt = f"Context: {context}\nHistory: {[m.model_dump() for m in history]}"
        reply = self.provider.generate(prompt)
        return Message(role="assistant", content=reply)
