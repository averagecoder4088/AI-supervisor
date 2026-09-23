"""Small provider-agnostic LLM client interface.

An LLM client does one thing: given prompts and a JSON schema, return the
model's raw JSON text. Validation happens in ``app.llm.schemas``; retry
classification happens in the reasoning Activity via the error types below.
"""

from typing import Any, Dict, Optional, Protocol


class LLMError(Exception):
    """Base class for LLM client failures."""


class LLMProviderError(LLMError):
    """Transient provider failure (timeout, rate limit, 5xx). Retryable."""


class LLMAuthenticationError(LLMError):
    """Bad or missing credentials. Retrying cannot help."""


class LLMNotConfiguredError(LLMError):
    """No usable provider is configured. Retrying cannot help."""


class LLMClient(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: Dict[str, Any],
    ) -> str:
        """Return the model's raw JSON text for the given schema."""
        ...


class OpenAILLMClient:
    """Placeholder for the OpenAI Responses API client (decision B7: deferred).

    The ``openai`` SDK is deliberately NOT a dependency yet: Python 3.9
    compatibility and the exact model ID must be verified first. Until then
    every call fails with a non-retryable ``LLMNotConfiguredError``, which the
    workflow records as a failed reasoning cycle (it stays alive).
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: Dict[str, Any],
    ) -> str:
        raise LLMNotConfiguredError(
            "OpenAI integration is deferred (Step 4, B7): the SDK is not installed until "
            "Python 3.9 compatibility and the model ID are verified."
        )
