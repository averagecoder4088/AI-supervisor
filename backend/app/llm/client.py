"""LLM client interface and the OpenAI adapter.

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
    """OpenAI Responses API adapter behind the ``LLMClient`` protocol (decision B7).

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
    ) -> None:
        self._api_key = (api_key or "").strip() or None
        self._model = (model or "").strip() or None
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client  # tests inject an httpx client with a mock transport
        self._sdk_client: Optional[Any] = None

    @classmethod
    def from_settings(cls, settings: "Settings") -> "OpenAILLMClient":
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else None
        return cls(api_key=key, model=settings.llm_model, timeout_seconds=settings.llm_timeout_seconds)

    def __repr__(self) -> str:
        return f"OpenAILLMClient(model={self._model!r}, configured={self._configured})"

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
            raise LLMNotConfiguredError("OpenAI LLM is not configured: set LLM_API_KEY and LLM_MODEL.")
        try:
            import openai  # lazy: an unconfigured or test environment never imports the SDK
        except ImportError:
            raise LLMNotConfiguredError("The 'openai' package is not installed.") from None

        try:
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


_KEY_LIKE = re.compile(r"sk-[A-Za-z0-9_\-*.]+")


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
