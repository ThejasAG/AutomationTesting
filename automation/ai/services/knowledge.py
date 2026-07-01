from automation.ai.providers import get_provider
import json

class AIKnowledgeBase:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def query(self, question: str) -> str:
        prompt = f"Query knowledge base: {question}"
        return self.provider.generate(prompt)

class AILearningEngine:
    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)
        
    def ingest_resolution(self, error: str, fix: str) -> bool:
        # In a real system, this would store embeddings in a vector DB
        embedding = self.provider.generate_embeddings(error)
        return len(embedding) > 0
