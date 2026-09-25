"""LLM client interface and the OpenAI-SDK adapter (OpenAI Responses API and Gemini).

An LLM client does one thing: given prompts and a JSON schema, return the
model's raw JSON text. Validation happens in ``app.llm.schemas``; retry
classification happens in the reasoning Activity via the error types below.
"""

import re
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

if TYPE_CHECKING:
    from app.config import Settings

# Client-side request timeout: shorter than the 60 s / 90 s Activity timeouts so the
# client fails first and Temporal's retry policy (not the SDK) decides what happens next.
DEFAULT_TIMEOUT_SECONDS = 45.0
# Internal cap, not a tuning knob: reasoning models spend output tokens on thinking too.
MAX_OUTPUT_TOKENS = 4096
MAX_DETAIL_CHARS = 300

# Gemini through Google's OpenAI-compatible endpoint. That endpoint serves Chat Completions but
# NOT the Responses API (POST .../openai/responses answers 404), so provider "gemini" uses
# chat.completions with the same strict JSON schema; everything else is shared.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.1-flash-lite"
# Not sent by default: gemma-4-31b-it answers HTTP 400 "Thinking level is not supported for this
# model" when ``reasoning_effort`` is present. Set LLM_REASONING_EFFORT to send it to a model that
# supports it (the previously validated gemini-3.6-flash was run with "low").
GEMINI_REASONING_EFFORT: Optional[str] = None


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
    """OpenAI-SDK adapter behind the ``LLMClient`` protocol (decision B7).

    provider="openai" (default): the OpenAI Responses API. provider="gemini": Google's
    OpenAI-compatible Chat Completions endpoint (``base_url``), model gemma-4-31b-it by default,
    ``reasoning_effort`` only when configured, selected by ``LLM_PROVIDER=gemini``.

    It returns the model's raw JSON text and translates provider failures into
    the three error types above; validation stays in ``app.llm.schemas`` and
    retries stay with Temporal, so:

    - the SDK's own automatic retries are switched off (``max_retries=0``);
    - the request timeout is explicit and shorter than the Activity timeouts;
    - the JSON schema is sent unchanged in strict structured-output mode.

    Constructed with no arguments it is "unconfigured": every call raises the
    non-retryable ``LLMNotConfiguredError`` without importing the SDK or touching
    the network. Configuration is only read by ``from_settings`` (the worker).
    The API key is never logged, repr'd or copied into an exception message.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: Optional[Any] = None,
        provider: str = "openai",
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self._api_key = (api_key or "").strip() or None
        self._model = (model or "").strip() or None
        self._timeout_seconds = timeout_seconds
        self._provider = provider
        self._base_url = (base_url or "").strip() or None
        self._reasoning_effort = (reasoning_effort or "").strip() or None
        self._http_client = http_client  # tests inject an httpx client with a mock transport
        self._sdk_client: Optional[Any] = None

    @classmethod
    def from_settings(cls, settings: "Settings") -> "OpenAILLMClient":
        def secret(value: Any) -> Optional[str]:
            return value.get_secret_value() if value is not None else None

        if settings.llm_provider == "gemini":
            return cls(
                api_key=secret(settings.gemini_api_key),
                model=settings.llm_model or GEMINI_MODEL,
                timeout_seconds=settings.llm_timeout_seconds,
                provider="gemini",
                base_url=settings.llm_base_url or GEMINI_BASE_URL,
                reasoning_effort=settings.llm_reasoning_effort or GEMINI_REASONING_EFFORT,
            )
        return cls(
            api_key=secret(settings.llm_api_key),
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            base_url=settings.llm_base_url,
        )

    def __repr__(self) -> str:
        provider = f"provider={self._provider!r}, " if self._provider != "openai" else ""
        return f"OpenAILLMClient({provider}model={self._model!r}, configured={self._configured})"

    @property
    def _configured(self) -> bool:
        return self._api_key is not None and self._model is not None

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: Dict[str, Any],
    ) -> str:
        if not self._configured:
            needed = "GEMINI_API_KEY" if self._provider == "gemini" else "LLM_API_KEY and LLM_MODEL"
            raise LLMNotConfiguredError(f"LLM is not configured (provider {self._provider}): set {needed}.")
        try:
            import openai  # lazy: an unconfigured or test environment never imports the SDK
        except ImportError:
            raise LLMNotConfiguredError("The 'openai' package is not installed.") from None

        try:
            if self._provider == "gemini":
                # Only sent when configured: some models (gemma-4-31b-it) reject the parameter.
                extra = {"reasoning_effort": self._reasoning_effort} if self._reasoning_effort else {}
                response = await self._sdk(openai).chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
                    },
                    **extra,
                )
                return _chat_text(response)
            response = await self._sdk(openai).responses.create(
                model=self._model,
                instructions=system_prompt,
                input=user_prompt,
                text={"format": {"type": "json_schema", "name": schema_name, "schema": json_schema, "strict": True}},
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,  # do not retain order data on the provider side
            )
        except openai.OpenAIError as err:
            # ``from None``: Temporal converts ``__cause__`` chains into the failure that
            # reaches the timeline, and the SDK's own message must not travel with it.
            raise self._translate(openai, err) from None
        return _response_text(response)

    def _sdk(self, openai: Any) -> Any:
        if self._sdk_client is None:
            self._sdk_client = openai.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                max_retries=0,  # Temporal owns retries; the SDK default of 2 would nest them
                http_client=self._http_client,
            )
        return self._sdk_client

    def _translate(self, openai: Any, err: Exception) -> LLMError:
        if isinstance(err, (openai.AuthenticationError, openai.PermissionDeniedError)):
            # Fixed text: provider auth messages can echo a masked fragment of the key.
            return LLMAuthenticationError(f"LLM provider rejected the credentials (HTTP {err.status_code}).")
        if isinstance(err, openai.APITimeoutError):
            return LLMProviderError("LLM provider request timed out.")
        if isinstance(err, openai.APIConnectionError):
            return LLMProviderError("Could not connect to the LLM provider.")
        if isinstance(err, openai.APIStatusError):
            status = err.status_code
            if status == 400 and "api key" in str(err).lower():
                # Gemini reports a missing/invalid key as HTTP 400 INVALID_ARGUMENT, not 401.
                return LLMAuthenticationError("LLM provider rejected the credentials (HTTP 400).")
            if status in (408, 409, 429) or status >= 500:
                return LLMProviderError(f"LLM provider temporarily failed (HTTP {status}).")
            # Any other 4xx (bad request, unknown model, rejected schema): retrying cannot help.
            return LLMNotConfiguredError(f"LLM provider rejected the request (HTTP {status}): {self._detail(err)}")
        return LLMProviderError(f"Unexpected LLM provider error ({type(err).__name__}).")

    def _detail(self, err: Any) -> str:
        parts = [str(part) for part in (getattr(err, "code", None), getattr(err, "type", None)) if part]
        message = getattr(err, "message", None)
        if isinstance(message, str) and message:
            parts.append(message)
        return _redact(" - ".join(parts) or "no detail", self._api_key)[:MAX_DETAIL_CHARS]


_KEY_LIKE = re.compile(r"sk-[A-Za-z0-9_\-*.]+|AIza[0-9A-Za-z_\-]{20,}")


def _redact(text: str, api_key: Optional[str]) -> str:
    if api_key:
        text = text.replace(api_key, "***")
    return _KEY_LIKE.sub("***", text)


def _response_text(response: Any) -> str:
    """The model's JSON text. A refusal (or nothing) comes back as text that fails
    strict parsing, which the Activity turns into a retryable InvalidLLMOutput."""
    text = getattr(response, "output_text", "") or ""
    if text:
        return text
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            refusal = getattr(part, "refusal", None)
            if isinstance(refusal, str) and refusal:
                return refusal
    return ""


def _chat_text(response: Any) -> str:
    """Chat Completions counterpart of ``_response_text``: the first choice's JSON text, else its
    refusal, else nothing. A truncated answer (finish_reason "length") is invalid JSON and fails
    strict parsing like every other unusable output."""
    for choice in getattr(response, "choices", None) or []:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            return content
        refusal = getattr(message, "refusal", None)
        if isinstance(refusal, str) and refusal:
            return refusal
        break
    return ""
