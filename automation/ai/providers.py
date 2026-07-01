from abc import ABC, abstractmethod
from typing import Optional, Any, List, Type
from pydantic import BaseModel

class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> str:
        pass
    
    @abstractmethod
    def generate_json(self, prompt: str, schema: Type[BaseModel], system_prompt: Optional[str] = None, **kwargs) -> BaseModel:
        pass
    
    @abstractmethod
    def generate_embeddings(self, text: str) -> List[float]:
        pass

class MockProviderBase(LLMProvider):
    def generate(self, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> str:
        return f"[{self.__class__.__name__}] Response to: {prompt[:30]}..."
        
    def generate_json(self, prompt: str, schema: Type[BaseModel], system_prompt: Optional[str] = None, **kwargs) -> BaseModel:
        # Returns a dummy instantiation of the BaseModel
        return schema.model_construct()
        
    def generate_embeddings(self, text: str) -> List[float]:
        return [0.0] * 1536

class OpenAIProvider(MockProviderBase):
    pass

class GeminiProvider(MockProviderBase):
    pass

class ClaudeProvider(MockProviderBase):
    pass

class OllamaProvider(MockProviderBase):
    pass

class AzureOpenAIProvider(MockProviderBase):
    pass

def get_provider(name: str = "openai") -> LLMProvider:
    providers = {
        "openai": OpenAIProvider,
        "gemini": GeminiProvider,
        "claude": ClaudeProvider,
        "ollama": OllamaProvider,
        "azure": AzureOpenAIProvider
    }
    return providers.get(name.lower(), OpenAIProvider)()
