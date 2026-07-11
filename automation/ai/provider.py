"""LLM Provider Abstraction Layer"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional
import json
import httpx


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: dict[str, int]
    provider: str


@dataclass
class RCAAnalysis:
    root_cause: str
    failure_category: str
    affected_modules: list[str]
    confidence: float
    possible_reason: str
    impact: str
    suggested_fix: str
    priority: str
    severity: str
    responsible_module: str
    summary: str
    evidence_used: dict[str, Any]


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[dict] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        """Generate response from LLM"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier"""
        pass


class AzureOpenAIProvider(LLMProvider):
    """Azure OpenAI provider for GDPR-compliant deployments"""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment_name: str,
        max_retries: int = 3,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._api_version = api_version
        self._deployment_name = deployment_name
        self._max_retries = max_retries
        self._client = httpx.Client(
            timeout=120.0,
            headers={
                "api-key": api_key,
                "Content-Type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return "azure-openai"

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[dict] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": json_schema,
            }

        response = self._client.post(
            f"{self._endpoint}/openai/deployments/{self._deployment_name}/chat/completions?api-version={self._api_version}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            model=data["model"],
            usage=data.get("usage", {}),
            provider=self.name,
        )

    def close(self):
        self._client.close()


class OpenAIProvider(LLMProvider):
    """OpenAI API provider"""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self._api_key = api_key
        self._model = model
        self._client = httpx.Client(
            timeout=120.0,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return "openai"

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[dict] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if json_schema:
            payload["response_format"] = {"type": "json_object"}

        response = self._client.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            model=data["model"],
            usage=data.get("usage", {}),
            provider=self.name,
        )

    def close(self):
        self._client.close()


class OllamaProvider(LLMProvider):
    """Ollama local model provider"""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3"):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.Client(timeout=120.0)

    @property
    def name(self) -> str:
        return "ollama"

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[dict] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        payload = {
            "model": self._model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        response = self._client.post(f"{self._base_url}/api/generate", json=payload)
        response.raise_for_status()
        data = response.json()

        return LLMResponse(
            content=data["response"],
            model=self._model,
            usage={"total_tokens": data.get("eval_count", 0)},
            provider=self.name,
        )

    def close(self):
        self._client.close()


class MockLLMProvider(LLMProvider):
    """Mock provider for testing"""

    def __init__(self, response: Optional[RCAAnalysis] = None):
        self._response = response or self._default_rca()

    @property
    def name(self) -> str:
        return "mock"

    def _default_rca(self) -> RCAAnalysis:
        return RCAAnalysis(
            root_cause="Element not found due to incorrect selector",
            failure_category="Locator Issue",
            affected_modules=["login_screen.py"],
            confidence=0.85,
            possible_reason="The locator 'button-login' was changed in the recent commit.",
            impact="Users cannot log in to the application.",
            suggested_fix="Update selector from 'button-login' to 'btn-primary-login'",
            priority="P1",
            severity="Critical",
            responsible_module="Authentication",
            summary="Test failed because the login button selector was incorrect. The element with ID 'button-login' was not found on the screen.",
            evidence_used={},
        )

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[dict] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        return LLMResponse(
            content=json.dumps({
                "root_cause": self._response.root_cause,
                "failure_category": self._response.failure_category,
                "affected_modules": self._response.affected_modules,
                "confidence": self._response.confidence,
                "possible_reason": self._response.possible_reason,
                "impact": self._response.impact,
                "suggested_fix": self._response.suggested_fix,
                "priority": self._response.priority,
                "severity": self._response.severity,
                "responsible_module": self._response.responsible_module,
                "summary": self._response.summary,
            }),
            model="mock-model",
            usage={"total_tokens": 100},
            provider=self.name,
        )


def create_provider(config: dict[str, Any]) -> LLMProvider:
    """Factory function to create LLM provider from config"""
    provider_type = config.get("provider") or config.get("type") or "mock"
    model = config.get("model") or config.get("model_name")

    if provider_type == "azure":
        return AzureOpenAIProvider(
            endpoint=config["endpoint"],
            api_key=config["api_key"],
            api_version=config.get("api_version", "2024-02-01"),
            deployment_name=config["deployment_name"],
        )
    elif provider_type == "openai":
        return OpenAIProvider(
            api_key=config["api_key"],
            model=model or "gpt-4o-mini",
        )
    elif provider_type == "ollama":
        return OllamaProvider(
            base_url=config.get("base_url", "http://localhost:11434"),
            model=model or "llama3",
        )
    elif provider_type == "vllm":
        return OpenAIProvider(
            api_key=config.get("api_key", "not-required"),
            model=model or "mistral",
        )
    else:
        return MockLLMProvider(config.get("mock_response"))

import os
default_config = {
    "provider": os.getenv("LLM_PROVIDER_TYPE", "mock"),
    "type": os.getenv("LLM_PROVIDER_TYPE", "mock"),
    "api_key": os.getenv("OPENAI_API_KEY", "dummy"),
    "model_name": os.getenv("LLM_MODEL_NAME", "gpt-4-turbo"),
    "model": os.getenv("LLM_MODEL_NAME", "gpt-4-turbo"),
    "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2023-05-15")
}
llm_provider = create_provider(default_config)
