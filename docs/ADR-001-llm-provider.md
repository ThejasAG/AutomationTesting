# ADR: LLM Provider Abstraction & GDPR Data Flow

## Status
Accepted

## Context
The platform requires LLM integration for root cause analysis, but must support multiple deployment scenarios:
- Cloud OpenAI API (default)
- Azure OpenAI (EU data residency)
- On-prem/self-hosted models (Ollama, vLLM)

Additionally, GDPR compliance requires careful handling of test data containing potential PII.

## Decision

### LLM Provider Abstraction

**Chose**: Interface-based abstraction with factory pattern

```
LLMProvider (ABC)
├── AzureOpenAIAdapter
├── OpenAIAdapter
├── OllamaAdapter
└── VLLMAdapter
```

**Rationale**: 
- Single interface allows uniform testing with mock providers
- Configuration-driven provider selection enables runtime switching
- Each adapter handles provider-specific authentication and API formatting
- Versioning prompts separately from provider logic ensures reproducibility

### GDPR Data Flow

**Chose**: Multi-layer privacy protection with opt-out for sensitive customers

1. **PII Scrubbing Layer** (pre-LLM):
   - Regex-based scrubbing for emails, phone numbers, addresses
   - SDK-based scrubbing for custom PII patterns
   - Configurable sensitivity levels (strict/relaxed)

2. **Data Minimization**:
   - Logs truncated to relevant error sections only
   - Large logs summarized before LLM submission
   - Configurable max context size per provider

3. **Deployment Options**:
   - `provider: azure` with EU region for GDPR compliance
   - `provider: ollama` for fully on-prem
   - `provider: openai` for non-sensitive workloads

4. **Audit Trail**:
   - All LLM calls logged with timestamp, provider, scrubbed payload
   - No raw sensitive data in logs

## Consequences

### Positive
- Single codebase supports both GDPR-compliant and standard deployments
- Testing can use mock provider without external dependencies
- Large logs handled gracefully via summarization

### Negative
- Complexity in PII scrubbing patterns (requires maintenance)
- On-prem providers may have different output formats

## Compliance Decision Points (Product/Legal)

1. **What constitutes PII in test logs?** - Legal must define acceptable scrub patterns
2. **Data retention period?** - Configurable default 90 days, but customer-specific policies needed
3. **EU-only deployment?** - May require separate Azure deployment in EU region
4. **Audit log retention?** - Must comply with SOX/GDPR requirements