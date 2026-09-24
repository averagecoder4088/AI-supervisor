"""LLM configuration (decision B7): env loading, secrets, and safe defaults.

None of these tests needs the network, an API key or a real ``backend/.env``:
``Settings`` is always built with ``_env_file=None`` and explicit environment values.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.client import DEFAULT_TIMEOUT_SECONDS, LLMNotConfiguredError, OpenAILLMClient

KEY = "sk-test-SECRETSECRET-1234567890"
BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
CALL = dict(system_prompt="s", user_prompt="u", schema_name="x", json_schema={})


def _settings(monkeypatch, **env) -> Settings:
    for name in ("LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_llm_settings_default_to_unset(monkeypatch):
    settings = _settings(monkeypatch)
    assert settings.llm_api_key is None
    assert settings.llm_model is None
    assert settings.llm_timeout_seconds == DEFAULT_TIMEOUT_SECONDS == 45.0


def test_llm_settings_load_from_the_environment_and_the_key_is_a_secret(monkeypatch):
    settings = _settings(monkeypatch, LLM_API_KEY=KEY, LLM_MODEL="some-model", LLM_TIMEOUT_SECONDS="30")
    assert settings.llm_api_key.get_secret_value() == KEY
    assert settings.llm_model == "some-model"
    assert settings.llm_timeout_seconds == 30.0
    assert KEY not in repr(settings) and "SECRET" not in repr(settings)
    assert KEY not in str(settings) and KEY not in settings.model_dump_json()


def test_timeout_must_be_positive(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, LLM_TIMEOUT_SECONDS="0")


def test_from_settings_builds_a_configured_client(monkeypatch):
    client = OpenAILLMClient.from_settings(
        _settings(monkeypatch, LLM_API_KEY=KEY, LLM_MODEL="some-model", LLM_TIMEOUT_SECONDS="30")
    )
    assert "configured=True" in repr(client) and "some-model" in repr(client)
    assert KEY not in repr(client)
    assert client._timeout_seconds == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [{}, {"LLM_API_KEY": ""}, {"LLM_MODEL": "some-model"}, {"LLM_API_KEY": KEY, "LLM_MODEL": ""}])
async def test_from_settings_without_a_key_or_model_is_not_configured(monkeypatch, env):
    client = OpenAILLMClient.from_settings(_settings(monkeypatch, **env))
    assert "configured=False" in repr(client)
    with pytest.raises(LLMNotConfiguredError):
        await client.generate_json(**CALL)


@pytest.mark.asyncio
async def test_a_bare_client_ignores_the_environment(monkeypatch):
    """A key in the environment or backend/.env must never make a test reach the network."""
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    client = OpenAILLMClient()
    assert "configured=False" in repr(client)
    with pytest.raises(LLMNotConfiguredError):
        await client.generate_json(**CALL)


def test_an_unconfigured_environment_never_imports_the_sdk():
    """Fresh interpreter, so other tests importing the SDK cannot mask the result."""
    script = (
        "import asyncio, sys\n"
        "import app.llm.client, app.temporal.worker\n"
        "from app.llm.client import OpenAILLMClient, LLMNotConfiguredError\n"
        "async def main():\n"
        "    try:\n"
        "        await OpenAILLMClient().generate_json(system_prompt='s', user_prompt='u', schema_name='x', json_schema={})\n"
        "    except LLMNotConfiguredError:\n"
        "        return\n"
        "    raise SystemExit('expected LLMNotConfiguredError')\n"
        "asyncio.run(main())\n"
        "assert 'openai' not in sys.modules, 'the SDK was imported'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(BACKEND), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_worker_default_client_comes_from_settings(monkeypatch):
    """create_worker without an injected client builds the adapter from configuration."""
    from app.temporal import worker

    captured = {}
    monkeypatch.setattr(worker, "Worker", lambda client, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: _settings(monkeypatch, LLM_API_KEY=KEY, LLM_MODEL="some-model", LLM_TIMEOUT_SECONDS="20"),
    )
    worker.create_worker(object(), session_factory=object())

    reasoning = next(a for a in captured["activities"] if a.__name__ == "generate_reasoning_decision")
    llm = reasoning.__self__._llm
    assert isinstance(llm, OpenAILLMClient)
    assert "some-model" in repr(llm) and "configured=True" in repr(llm)
    assert llm._timeout_seconds == 20.0


def test_the_env_examples_carry_names_only_and_stay_identical():
    root_example = (ROOT / ".env.example").read_text()
    backend_example = (BACKEND / ".env.example").read_text()
    assert root_example == backend_example
    lines = dict(line.split("=", 1) for line in backend_example.splitlines() if "=" in line and not line.startswith("#"))
    assert lines["LLM_API_KEY"] == ""  # a name, never a value
    assert lines["LLM_MODEL"] == ""
