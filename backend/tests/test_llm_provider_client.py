"""OpenAI adapter (decision B7) against mocked HTTP: no network, no real key.

The real ``openai`` SDK runs, but its HTTP client is an ``httpx`` client with a mock
transport, so request building, response parsing and the SDK's exception types are
all genuine while nothing leaves the process.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Callable, List

import httpx
import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.llm.client import (
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

pytestmark = pytest.mark.asyncio

KEY = "sk-test-SECRETSECRET-1234567890"
CALL = dict(system_prompt="system text", user_prompt="user text")


def _response_body(text: str, **extra) -> dict:
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }
    body.update(extra)
    return body


def _error_body(message: str, code: str = "some_code", kind: str = "some_error") -> dict:
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


class _Server:
    """A mock provider: records every request and answers with ``handler``."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: List[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _client(server: _Server, **kwargs) -> OpenAILLMClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return OpenAILLMClient(api_key=KEY, model="test-model", http_client=http_client, **kwargs)


def _replying(status: int, body: dict) -> _Server:
    return _Server(lambda request: httpx.Response(status, json=body))


def _raising(exc_factory) -> _Server:
    def handler(request):
        raise exc_factory(request)

    return _Server(handler)


async def _generate(client: OpenAILLMClient, schema_name=REASONING_SCHEMA_NAME, schema=None) -> str:
    return await client.generate_json(
        schema_name=schema_name,
        json_schema=schema if schema is not None else reasoning_decision_json_schema(),
        **CALL,
    )


# ------------------------------------------------------------------ success


async def test_success_returns_the_model_json_and_sends_a_strict_schema_request():
    raw = make_decision_json(tool="escalate_shipment", reason="8h delay", priority="high")
    server = _replying(200, _response_body(raw))
    schema = reasoning_decision_json_schema()

    assert await _generate(_client(server), schema=schema) == raw

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.url.path.endswith("/responses")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["instructions"] == "system text"
    assert body["input"] == "user text"
    assert body["store"] is False
    assert body["max_output_tokens"] == 4096  # the literal, so a silent change to the cap is caught
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": REASONING_SCHEMA_NAME,
        # sent exactly as the application generated it: no sanitising or rewriting
        "schema": schema,
        "strict": True,
    }
    # the raw text still goes through the unchanged strict local validation
    assert parse_reasoning_decision(raw).tool == "escalate_shipment"


async def test_the_request_carries_exactly_the_approved_parameters_and_nothing_else():
    """Pins the whole request shape (openai 2.48.0): a stray temperature, tools, verbosity
    or response-storage flag would show up here."""
    server = _replying(200, _response_body(make_decision_json()))
    await _generate(_client(server))
    body = json.loads(server.requests[0].content)
    assert set(body) == {"model", "instructions", "input", "text", "max_output_tokens", "store"}
    assert set(body["text"]) == {"format"}
    assert set(body["text"]["format"]) == {"type", "name", "schema", "strict"}
    assert body["store"] is False and body["max_output_tokens"] == 4096
    assert body["text"]["format"]["strict"] is True


async def test_the_instantiated_sdk_client_has_retries_off_and_the_configured_timeout():
    """Direct evidence on the real ``openai.AsyncOpenAI`` object the adapter builds
    (its defaults are max_retries=2 and a 600 s read timeout)."""
    server = _replying(200, _response_body(make_decision_json()))

    default = _client(server)  # no explicit timeout: the 45 s default
    await _generate(default)
    assert default._sdk_client.max_retries == 0
    assert default._sdk_client.timeout == 45.0
    assert server.requests[0].extensions["timeout"]["read"] == 45.0

    configured = _client(server, timeout_seconds=30.0)
    await _generate(configured)
    assert configured._sdk_client.max_retries == 0
    assert configured._sdk_client.timeout == 30.0


async def test_final_output_schema_and_text_round_trip():
    raw = make_final_output_json(summary="Delivered.")
    server = _replying(200, _response_body(raw))
    text = await _generate(
        _client(server), schema_name=FINAL_OUTPUT_SCHEMA_NAME, schema=final_output_json_schema()
    )
    assert parse_final_output(text).summary == "Delivered."
    body = json.loads(server.requests[0].content)
    assert body["text"]["format"]["name"] == FINAL_OUTPUT_SCHEMA_NAME
    assert body["text"]["format"]["schema"] == final_output_json_schema()


@pytest.mark.parametrize(
    "response_body",
    [
        # a refusal instead of JSON
        _response_body("", output=[{"type": "message", "id": "m", "status": "completed", "role": "assistant",
                                    "content": [{"type": "refusal", "refusal": "I can't help with that."}]}]),
        # nothing at all
        _response_body(""),
        # cut off by the output cap: truncated JSON
        _response_body('{"assessment": "partial', status="incomplete",
                       incomplete_details={"reason": "max_output_tokens"}),
    ],
    ids=["refusal", "empty", "truncated"],
)
async def test_unusable_model_output_comes_back_as_text_that_fails_strict_parsing(response_body):
    text = await _generate(_client(_replying(200, response_body)))
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(text)


# ---------------------------------------------------- error mapping (client)


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, LLMAuthenticationError),
        (403, LLMAuthenticationError),
        (408, LLMProviderError),
        (409, LLMProviderError),
        (429, LLMProviderError),
        (500, LLMProviderError),
        (503, LLMProviderError),
        (400, LLMNotConfiguredError),
        (404, LLMNotConfiguredError),
        (422, LLMNotConfiguredError),
    ],
)
async def test_http_status_maps_onto_the_existing_error_types(status, expected):
    server = _replying(status, _error_body("provider says no"))
    with pytest.raises(expected):
        await _generate(_client(server))


async def test_timeout_and_connection_failures_are_transient():
    for exc_factory in (
        lambda request: httpx.ReadTimeout("read timed out", request=request),
        lambda request: httpx.ConnectTimeout("connect timed out", request=request),
        lambda request: httpx.ConnectError("connection refused", request=request),
    ):
        with pytest.raises(LLMProviderError):
            await _generate(_client(_raising(exc_factory)))


async def test_configuration_error_carries_the_provider_code_and_message_for_diagnosis():
    server = _replying(400, _error_body("Invalid schema: 'minLength' is not permitted.", "invalid_json_schema"))
    with pytest.raises(LLMNotConfiguredError) as exc:
        await _generate(_client(server))
    assert "HTTP 400" in str(exc.value)
    assert "invalid_json_schema" in str(exc.value)
    assert "minLength" in str(exc.value)


# ------------------------------------------------- no nested retries (RC1)


@pytest.mark.parametrize(
    "server",
    [
        _replying(429, _error_body("slow down", "rate_limit_exceeded")),
        _replying(500, _error_body("boom")),
        _replying(503, _error_body("unavailable")),
        _raising(lambda request: httpx.ReadTimeout("read timed out", request=request)),
        _raising(lambda request: httpx.ConnectError("connection refused", request=request)),
    ],
    ids=["429", "500", "503", "timeout", "connection"],
)
async def test_the_sdk_does_not_retry_so_temporal_is_the_only_retry_owner(server):
    with pytest.raises(LLMProviderError):
        await _generate(_client(server))
    # the SDK default is max_retries=2 (three requests); exactly one means it is switched off
    assert len(server.requests) == 1


async def test_the_client_timeout_is_explicit_and_configurable():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=_response_body(make_decision_json()))

    await _generate(_client(_Server(handler), timeout_seconds=12.5))
    assert seen["timeout"]["read"] == 12.5  # not the SDK's 600 s default


# -------------------------------------------- Activity-level classification


def _reasoning_context() -> ReasoningInput:
    return ReasoningInput(
        order_id="12345",
        order_status="delayed",
        wake_reason="important_event",
        supervisor_instructions="",
        supervisor_version=1,
        run_instructions=[],
        enabled_tools=["get_order_status"],
        memory={},
        new_events=[],
        recent_events=[],
        last_action_result=None,
        default_wake_interval_minutes=60,
        min_wake_interval_minutes=5,
        max_wake_interval_minutes=1440,
        now=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
    )


async def _run_reasoning(server: _Server):
    activities = ReasoningActivities(_client(server))
    return await ActivityEnvironment().run(activities.generate_reasoning_decision, _reasoning_context())


async def test_activity_returns_a_validated_decision_through_the_real_adapter():
    server = _replying(200, _response_body(make_decision_json(assessment="All quiet.")))
    decision = await _run_reasoning(server)
    assert decision.assessment == "All quiet."
    assert decision.tool is None


@pytest.mark.parametrize(
    "server, error_type, non_retryable",
    [
        (_replying(401, _error_body("bad key")), "LLMAuthenticationError", True),
        (_replying(403, _error_body("forbidden")), "LLMAuthenticationError", True),
        (_replying(429, _error_body("slow down")), "LLMProviderError", False),
        (_replying(500, _error_body("boom")), "LLMProviderError", False),
        (_raising(lambda r: httpx.ReadTimeout("t", request=r)), "LLMProviderError", False),
        (_raising(lambda r: httpx.ConnectError("c", request=r)), "LLMProviderError", False),
        (_replying(400, _error_body("bad request")), "LLMNotConfigured", True),
        (_replying(404, _error_body("no such model", "model_not_found")), "LLMNotConfigured", True),
        (_replying(200, _response_body("not json")), "InvalidLLMOutput", False),
    ],
    ids=["401", "403", "429", "500", "timeout", "connection", "400", "404", "invalid-output"],
)
async def test_activity_retry_classification_is_unchanged_with_the_real_adapter(server, error_type, non_retryable):
    with pytest.raises(ApplicationError) as exc:
        await _run_reasoning(server)
    assert exc.value.type == error_type
    assert exc.value.non_retryable is non_retryable


async def test_final_output_activity_through_the_real_adapter():
    server = _replying(200, _response_body(make_final_output_json(summary="Delivered.")))
    activities = ReasoningActivities(_client(server))
    context = FinalOutputInput(
        order_id="12345",
        final_order_status="delivered",
        supervisor_instructions="",
        run_instructions=[],
        memory={},
        recent_events=[],
        action_log=[],
        events_received=1,
        reasoning_count=1,
    )
    content = await ActivityEnvironment().run(activities.generate_final_output, context)
    assert content.summary == "Delivered."


# ------------------------------------------------- unconfigured / SDK absent


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"api_key": KEY}, {"model": "test-model"}, {"api_key": "  ", "model": "test-model"}, {"api_key": KEY, "model": ""}],
    ids=["nothing", "no-model", "no-key", "blank-key", "blank-model"],
)
async def test_unconfigured_client_fails_non_retryably_without_any_http(kwargs):
    server = _replying(200, _response_body(make_decision_json()))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    with pytest.raises(LLMNotConfiguredError):
        await OpenAILLMClient(http_client=http_client, **kwargs).generate_json(
            schema_name="x", json_schema={}, **CALL
        )
    assert server.requests == []


async def test_missing_sdk_is_a_non_retryable_configuration_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)  # makes `import openai` raise ImportError
    with pytest.raises(LLMNotConfiguredError) as exc:
        await OpenAILLMClient(api_key=KEY, model="test-model").generate_json(
            schema_name="x", json_schema={}, **CALL
        )
    assert "not installed" in str(exc.value)


# ------------------------------------------------------------ secret hygiene

ECHOING_AUTH_BODY = _error_body(f"Incorrect API key provided: {KEY}. You can find your key at ...", "invalid_api_key")


async def test_authentication_error_never_repeats_the_provider_message():
    with pytest.raises(LLMAuthenticationError) as exc:
        await _generate(_client(_replying(401, ECHOING_AUTH_BODY)))
    assert str(exc.value) == "LLM provider rejected the credentials (HTTP 401)."
    assert "SECRET" not in str(exc.value) and KEY not in str(exc.value)
    assert exc.value.__cause__ is None  # nothing chained that Temporal could serialise


async def test_configuration_error_redacts_the_key_from_provider_text():
    body = _error_body(f"Bad request for key {KEY}, also sk-other-KEYMATERIAL99 was rejected", "bad")
    with pytest.raises(LLMNotConfiguredError) as exc:
        await _generate(_client(_replying(400, body)))
    text = str(exc.value)
    assert "SECRET" not in text and "KEYMATERIAL" not in text and KEY not in text
    assert "***" in text


@pytest.mark.parametrize(
    "server",
    [_replying(401, ECHOING_AUTH_BODY), _replying(400, _error_body(f"echo {KEY}"))],
    ids=["401", "400"],
)
async def test_key_does_not_reach_the_serialised_temporal_failure_chain(server):
    """The workflow copies the deepest failure message into the timeline (and so the
    database and API); convert the failure exactly as Temporal does and check it all."""
    with pytest.raises(ApplicationError) as exc:
        await _run_reasoning(server)
    failure = Failure()
    DefaultFailureConverter().to_failure(exc.value, DefaultPayloadConverter(), failure)
    assert "SECRET" not in str(failure) and KEY not in str(failure)
    cause = failure
    while cause.HasField("cause"):  # walk to the deepest cause, like the workflow does
        cause = cause.cause
    assert "SECRET" not in cause.message


async def test_key_does_not_appear_in_repr_or_logs(caplog):
    client = _client(_replying(401, ECHOING_AUTH_BODY))
    assert KEY not in repr(client) and "SECRET" not in repr(client)
    assert "configured=True" in repr(client)

    caplog.set_level(logging.DEBUG)  # the SDK and httpx log request details at DEBUG/INFO
    with pytest.raises(LLMAuthenticationError):
        await _generate(client)
    assert "SECRET" not in caplog.text and KEY not in caplog.text
