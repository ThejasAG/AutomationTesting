import pytest
from automation.ai.providers import get_provider, OpenAIProvider, GeminiProvider, ClaudeProvider, OllamaProvider, AzureOpenAIProvider

def test_provider_factory():
    assert isinstance(get_provider("openai"), OpenAIProvider)
    assert isinstance(get_provider("gemini"), GeminiProvider)
    assert isinstance(get_provider("claude"), ClaudeProvider)
    assert isinstance(get_provider("ollama"), OllamaProvider)
    assert isinstance(get_provider("azure"), AzureOpenAIProvider)
    # Default fallback
    assert isinstance(get_provider("unknown"), OpenAIProvider)

def test_mock_provider_generation():
    provider = get_provider("openai")
    res = provider.generate("Hello world")
    assert "Response to: Hello world" in res
    assert "[OpenAIProvider]" in res

def test_mock_provider_embeddings():
    provider = get_provider("openai")
    embeddings = provider.generate_embeddings("Test string")
    assert len(embeddings) == 1536
    assert all(e == 0.0 for e in embeddings)
