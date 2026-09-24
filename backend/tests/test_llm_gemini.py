"""Gemini through Google's OpenAI-compatible endpoint (offline).

The real ``openai`` SDK runs against an httpx mock transport: no network, no real key. These
tests pin the exact request the Gemini branch sends (Chat Completions, because that endpoint has
no Responses API), the unchanged structured-output schema, retry ownership, error mapping and
key hygiene, and that both LLM Activities use the Gemini-backed client.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List

import httpx
import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.config import Settings
from app.llm.client import (
    GEMINI_BASE_URL,
    GEMINI_MODEL,
    GEMINI_REASONING_EFFORT,
    LLMAuthenticationError,
    LLMNotConfiguredError,
    LLMProviderError,
    OpenAILLMClient,
)
from app.llm.fake import make_decision_json, make_final_output_json
from app.llm.schemas import (
    FINAL_OUTPUT_SCHEMA_NAME,
    REASONING_SCHEMA_NAME,
    InvalidLLMOutputError,
    final_output_json_schema,
    parse_final_output,
    parse_reasoning_decision,
    reasoning_decision_json_schema,
)
from app.temporal.activities.reasoning import ReasoningActivities
from app.temporal.contracts import FinalOutputInput, ReasoningInput

KEY = "AIzaSyFAKE-not-a-real-key-0123456789abcd"
CALL = dict(system_prompt="system text", user_prompt="user text")


def _completion(text, finish_reason: str = "stop", **message_extra) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": GEMINI_MODEL,
        "choices": [
            {"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": text, **message_extra}}
        ],
    }


def _error(message: str, status: str = "INVALID_ARGUMENT") -> list:
    return [{"error": {"code": 400, "message": message, "status": status}}]


class _Server:
    def __init__(self, handler) -> None:
        self.requests: List[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _replying(status: int, body) -> _Server:
    return _Server(lambda request: httpx.Response(status, json=body))


def _raising(factory) -> _Server:
    def handler(request):
        raise factory(request)

    return _Server(handler)


def _gemini(server: _Server, **kwargs) -> OpenAILLMClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    defaults = dict(
        api_key=KEY,
        model=GEMINI_MODEL,
        provider="gemini",
        base_url=GEMINI_BASE_URL,
        reasoning_effort=GEMINI_REASONING_EFFORT,
        http_client=http_client,
    )
    defaults.update(kwargs)
    return OpenAILLMClient(**defaults)


async def _generate(client: OpenAILLMClient, name=REASONING_SCHEMA_NAME, schema=None) -> str:
    return await client.generate_json(
        schema_name=name, json_schema=schema if schema is not None else reasoning_decision_json_schema(), **CALL
    )


# ------------------------------------------------------- the exact request


@pytest.mark.asyncio
async def test_gemini_request_is_chat_completions_with_the_exact_approved_shape():
    raw = make_decision_json(assessment="ok")
    server = _replying(200, _completion(raw))
    schema = reasoning_decision_json_schema()

    assert await _generate(_gemini(server), schema=schema) == raw

    assert len(server.requests) == 1
    request = server.requests[0]
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gemini-3.6-flash"
    assert body["reasoning_effort"] == "low"
    assert body["messages"] == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "user text"},
    ]
    # the application's schema, byte for byte: not sanitised, not rewritten
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": REASONING_SCHEMA_NAME, "schema": schema, "strict": True},
    }
    # nothing else: no temperature, no Responses-API fields, no output cap
    assert set(body) == {"model", "messages", "response_format", "reasoning_effort"}
    assert parse_reasoning_decision(raw).assessment == "ok"  # local strict validation unchanged


@pytest.mark.asyncio
async def test_final_output_uses_the_same_endpoint_and_its_own_unchanged_schema():
    raw = make_final_output_json(summary="Delivered.")
    server = _replying(200, _completion(raw))
    text = await _generate(_gemini(server), FINAL_OUTPUT_SCHEMA_NAME, final_output_json_schema())
    assert parse_final_output(text).summary == "Delivered."
    body = json.loads(server.requests[0].content)
    assert body["response_format"]["json_schema"]["name"] == FINAL_OUTPUT_SCHEMA_NAME
    assert body["response_format"]["json_schema"]["schema"] == final_output_json_schema()
    assert body["reasoning_effort"] == "low" and body["model"] == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_the_sdk_client_keeps_retries_off_and_the_gemini_base_url():
    server = _replying(200, _completion(make_decision_json()))
    client = _gemini(server)
    await _generate(client)
    assert client._sdk_client.max_retries == 0  # Temporal owns retries
    assert str(client._sdk_client.base_url) == GEMINI_BASE_URL
    assert client._sdk_client.timeout == 45.0
    assert repr(client) == "OpenAILLMClient(provider='gemini', model='gemini-3.6-flash', configured=True)"


@pytest.mark.parametrize(
    "server",
    [
        _replying(429, _error("Resource has been exhausted (quota).", "RESOURCE_EXHAUSTED")),
        _replying(500, _error("internal", "INTERNAL")),
        _replying(503, _error("The model is overloaded.", "UNAVAILABLE")),
        _raising(lambda r: httpx.ReadTimeout("t", request=r)),
        _raising(lambda r: httpx.ConnectError("c", request=r)),
    ],
    ids=["429-quota", "500", "503", "timeout", "connection"],
)
@pytest.mark.asyncio
async def test_no_nested_retries_a_failing_call_makes_exactly_one_request(server):
    with pytest.raises(LLMProviderError):
        await _generate(_gemini(server))
    assert len(server.requests) == 1


# -------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, LLMAuthenticationError),
        (403, LLMAuthenticationError),
        (429, LLMProviderError),
        (500, LLMProviderError),
        (503, LLMProviderError),
        (400, LLMNotConfiguredError),  # Gemini answers 400 for a bad key, a bad schema and an unknown model
        (404, LLMNotConfiguredError),
    ],
)
@pytest.mark.asyncio
async def test_gemini_http_errors_map_onto_the_existing_error_types(status, expected):
    with pytest.raises(expected):
        await _generate(_gemini(_replying(status, _error("nope"))))


@pytest.mark.parametrize(
    "body",
    [
        _completion(""),
        _completion(None, refusal="I can't do that."),
        _completion('{"assessment": "cut off', finish_reason="length"),
        {"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": []},
    ],
    ids=["empty", "refusal", "truncated", "no-choices"],
)
@pytest.mark.asyncio
async def test_unusable_output_fails_strict_parsing_so_the_activity_retries(body):
    text = await _generate(_gemini(_replying(200, body)))
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(text)


# ----------------------------------------- both Activities use this client


def _reasoning_context() -> ReasoningInput:
    return ReasoningInput(
        order_id="1",
        order_status=None,
        wake_reason="workflow_start",
        supervisor_instructions="",
        supervisor_version=1,
        run_instructions=[],
        enabled_tools=["get_order_status"],
        memory={},
        new_events=[],
        recent_events=[],
        last_action_result=None,
        default_wake_interval_minutes=60,
        min_wake_interval_minutes=1,
        max_wake_interval_minutes=1440,
        now=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )


def _final_context() -> FinalOutputInput:
    return FinalOutputInput(
        order_id="1",
        final_order_status="delivered",
        supervisor_instructions="",
        run_instructions=[],
        memory={},
        recent_events=[],
        action_log=[],
        events_received=1,
        reasoning_count=1,
    )


@pytest.mark.asyncio
async def test_reasoning_and_final_output_activities_both_call_gemini():
    def handler(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        raw = make_final_output_json(summary="Done.") if name == FINAL_OUTPUT_SCHEMA_NAME else make_decision_json()
        return httpx.Response(200, json=_completion(raw))

    server = _Server(handler)
    activities = ReasoningActivities(_gemini(server))
    decision = await ActivityEnvironment().run(activities.generate_reasoning_decision, _reasoning_context())
    output = await ActivityEnvironment().run(activities.generate_final_output, _final_context())

    assert decision.tool is None and output.summary == "Done."
    schemas = [json.loads(r.content)["response_format"]["json_schema"]["name"] for r in server.requests]
    assert schemas == [REASONING_SCHEMA_NAME, FINAL_OUTPUT_SCHEMA_NAME]
    assert {str(r.url) for r in server.requests} == {
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    }


@pytest.mark.parametrize(
    "server, error_type, non_retryable",
    [
        (_replying(401, _error("bad key")), "LLMAuthenticationError", True),
        (_replying(429, _error("quota", "RESOURCE_EXHAUSTED")), "LLMProviderError", False),
        (_replying(503, _error("overloaded", "UNAVAILABLE")), "LLMProviderError", False),
        (_raising(lambda r: httpx.ReadTimeout("t", request=r)), "LLMProviderError", False),
        (_replying(400, _error("schema rejected")), "LLMNotConfigured", True),
        (_replying(200, _completion("not json")), "InvalidLLMOutput", False),
    ],
    ids=["401", "429", "503", "timeout", "400", "invalid-output"],
)
@pytest.mark.asyncio
async def test_activity_retry_classification_is_unchanged_for_gemini(server, error_type, non_retryable):
    with pytest.raises(ApplicationError) as exc:
        await ActivityEnvironment().run(
            ReasoningActivities(_gemini(server)).generate_reasoning_decision, _reasoning_context()
        )
    assert (exc.value.type, exc.value.non_retryable) == (error_type, non_retryable)


# ------------------------------------------------------------ key hygiene

KEY_ECHO = _error(f"API key not valid: {KEY}. Please pass a valid API key.", "INVALID_ARGUMENT")


@pytest.mark.asyncio
async def test_gemini_reports_a_bad_key_as_http_400_and_it_is_classified_as_authentication():
    """Observed against the real endpoint: 400 INVALID_ARGUMENT "Please pass a valid API key"."""
    body = _error("Please pass a valid API key")
    with pytest.raises(LLMAuthenticationError) as exc:
        await _generate(_gemini(_replying(400, body)))
    assert str(exc.value) == "LLM provider rejected the credentials (HTTP 400)."
    with pytest.raises(ApplicationError) as activity_error:
        await ActivityEnvironment().run(
            ReasoningActivities(_gemini(_replying(400, KEY_ECHO))).generate_reasoning_decision, _reasoning_context()
        )
    assert (activity_error.value.type, activity_error.value.non_retryable) == ("LLMAuthenticationError", True)
    assert KEY not in activity_error.value.message


@pytest.mark.asyncio
async def test_a_google_key_in_a_provider_error_is_redacted():
    body = _error(f"Invalid JSON payload near {KEY}: unknown field.")
    with pytest.raises(LLMNotConfiguredError) as exc:
        await _generate(_gemini(_replying(400, body)))
    assert "HTTP 400" in str(exc.value) and "***" in str(exc.value)
    assert KEY not in str(exc.value) and "FAKE-not-a-real-key" not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.asyncio
async def test_a_different_google_style_key_echoed_by_the_provider_is_also_redacted():
    other = "AIzaSyOTHER-key-fragment-9876543210zz"  # not the configured key: only the pattern can catch it
    with pytest.raises(LLMNotConfiguredError) as exc:
        await _generate(_gemini(_replying(400, _error(f"Invalid key {other} in request"))))
    assert other not in str(exc.value) and "OTHER-key-fragment" not in str(exc.value)
    assert "***" in str(exc.value)


@pytest.mark.asyncio
async def test_a_google_key_in_an_auth_error_never_reaches_the_temporal_failure_chain():
    with pytest.raises(ApplicationError) as exc:
        await ActivityEnvironment().run(
            ReasoningActivities(_gemini(_replying(401, KEY_ECHO))).generate_reasoning_decision, _reasoning_context()
        )
    failure = Failure()
    DefaultFailureConverter().to_failure(exc.value, DefaultPayloadConverter(), failure)
    assert KEY not in str(failure) and "FAKE-not-a-real-key" not in str(failure)


@pytest.mark.asyncio
async def test_the_key_is_not_in_repr_or_logs(caplog):
    client = _gemini(_replying(401, KEY_ECHO))
    assert KEY not in repr(client)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(LLMAuthenticationError):
        await _generate(client)
    assert KEY not in caplog.text


@pytest.mark.asyncio
async def test_unconfigured_gemini_client_fails_without_any_http():
    server = _replying(200, _completion(make_decision_json()))
    with pytest.raises(LLMNotConfiguredError) as exc:
        await _gemini(server, api_key=None).generate_json(schema_name="x", json_schema={}, **CALL)
    assert "GEMINI_API_KEY" in str(exc.value)
    assert server.requests == []


# ------------------------------------------------------------- settings


def _settings(monkeypatch, **env) -> Settings:
    for name in (
        "LLM_PROVIDER", "LLM_API_KEY", "GEMINI_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
        "LLM_REASONING_EFFORT", "LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_provider_defaults_to_openai_so_nothing_changes_unless_gemini_is_selected(monkeypatch):
    assert _settings(monkeypatch).llm_provider == "openai"


def test_gemini_settings_supply_the_exact_defaults_and_read_the_key_from_gemini_api_key(monkeypatch):
    settings = _settings(monkeypatch, LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY, LLM_API_KEY="ignored-openai-key")
    client = OpenAILLMClient.from_settings(settings)
    assert client._provider == "gemini"
    assert client._api_key == KEY  # GEMINI_API_KEY, not LLM_API_KEY
    assert client._model == "gemini-3.6-flash"
    assert client._base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert client._reasoning_effort == "low"
    assert client._timeout_seconds == 45.0
    assert KEY not in repr(settings) and KEY not in settings.model_dump_json()


def test_gemini_settings_can_be_overridden_explicitly(monkeypatch):
    settings = _settings(
        monkeypatch, LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY, LLM_MODEL="other-model",
        LLM_BASE_URL="http://127.0.0.1:9/v1/", LLM_REASONING_EFFORT="high", LLM_TIMEOUT_SECONDS="20",
    )
    client = OpenAILLMClient.from_settings(settings)
    assert (client._model, client._base_url, client._reasoning_effort, client._timeout_seconds) == (
        "other-model", "http://127.0.0.1:9/v1/", "high", 20.0,
    )


@pytest.mark.asyncio
async def test_gemini_without_gemini_api_key_is_not_configured_even_if_llm_api_key_is_set(monkeypatch):
    client = OpenAILLMClient.from_settings(_settings(monkeypatch, LLM_PROVIDER="gemini", LLM_API_KEY=KEY))
    assert "configured=False" in repr(client)
    with pytest.raises(LLMNotConfiguredError):
        await client.generate_json(schema_name="x", json_schema={}, **CALL)


def test_an_unknown_provider_is_rejected(monkeypatch):
    with pytest.raises(Exception):
        _settings(monkeypatch, LLM_PROVIDER="fake")


def test_the_worker_default_client_is_gemini_when_configured(monkeypatch):
    """The production worker (create_worker without an injected client) gets the Gemini client;
    FakeLLMClient is only ever injected explicitly by tests."""
    from app.temporal import worker

    captured = {}
    monkeypatch.setattr(worker, "Worker", lambda client, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        worker, "get_settings", lambda: _settings(monkeypatch, LLM_PROVIDER="gemini", GEMINI_API_KEY=KEY)
    )
    worker.create_worker(object(), session_factory=object())

    activities = {a.__name__: a for a in captured["activities"]}
    reasoning_llm = activities["generate_reasoning_decision"].__self__._llm
    final_llm = activities["generate_final_output"].__self__._llm
    assert reasoning_llm is final_llm  # one client serves BOTH Activities
    assert isinstance(reasoning_llm, OpenAILLMClient)
    assert reasoning_llm._provider == "gemini" and reasoning_llm._model == "gemini-3.6-flash"
    assert type(reasoning_llm).__name__ != "FakeLLMClient"
