"""LLM Activities: reasoning decision and final output.

Each Activity builds the prompt, calls the injected LLM client, validates the
structured output and returns a typed contract. The LLM has no other powers.

Failure classification (drives Temporal retries):
- LLMAuthenticationError / LLMNotConfiguredError -> non-retryable
- LLMProviderError (timeout, rate limit, 5xx)    -> retryable
- InvalidLLMOutput                                -> retryable (a retry re-asks the model)
"""

from typing import Any, Dict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.llm.client import (
    LLMAuthenticationError,
    LLMClient,
    LLMNotConfiguredError,
    LLMProviderError,
)
from app.llm.prompts import (
    FINAL_OUTPUT_SYSTEM_PROMPT,
    REASONING_SYSTEM_PROMPT,
    build_final_output_user_prompt,
    build_reasoning_user_prompt,
)
from app.llm.schemas import (
    FINAL_OUTPUT_SCHEMA_NAME,
    REASONING_SCHEMA_NAME,
    InvalidLLMOutputError,
    final_output_json_schema,
    parse_final_output,
    parse_reasoning_decision,
    reasoning_decision_json_schema,
)
from app.temporal.contracts import (
    GENERATE_FINAL_OUTPUT,
    GENERATE_REASONING_DECISION,
    FinalOutputContent,
    FinalOutputInput,
    ReasoningDecision,
    ReasoningInput,
)


class ReasoningActivities:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def _call(self, *, system_prompt: str, user_prompt: str, schema_name: str, schema: Dict[str, Any]) -> str:
        try:
            return await self._llm.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name=schema_name,
                json_schema=schema,
            )
        except LLMAuthenticationError as err:
            raise ApplicationError(str(err), type="LLMAuthenticationError", non_retryable=True) from err
        except LLMNotConfiguredError as err:
            raise ApplicationError(str(err), type="LLMNotConfigured", non_retryable=True) from err
        except LLMProviderError as err:
            raise ApplicationError(str(err), type="LLMProviderError") from err

    @activity.defn(name=GENERATE_REASONING_DECISION)
    async def generate_reasoning_decision(self, context: ReasoningInput) -> ReasoningDecision:
        raw = await self._call(
            system_prompt=REASONING_SYSTEM_PROMPT,
            user_prompt=build_reasoning_user_prompt(context),
            schema_name=REASONING_SCHEMA_NAME,
            schema=reasoning_decision_json_schema(),
        )
        try:
            return parse_reasoning_decision(raw)
        except InvalidLLMOutputError as err:
            raise ApplicationError(str(err), type="InvalidLLMOutput") from err

    @activity.defn(name=GENERATE_FINAL_OUTPUT)
    async def generate_final_output(self, context: FinalOutputInput) -> FinalOutputContent:
        raw = await self._call(
            system_prompt=FINAL_OUTPUT_SYSTEM_PROMPT,
            user_prompt=build_final_output_user_prompt(context),
            schema_name=FINAL_OUTPUT_SCHEMA_NAME,
            schema=final_output_json_schema(),
        )
        try:
            return parse_final_output(raw)
        except InvalidLLMOutputError as err:
            raise ApplicationError(str(err), type="InvalidLLMOutput") from err
