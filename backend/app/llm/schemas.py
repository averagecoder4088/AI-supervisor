"""Structured-output schemas for the LLM, validated with Pydantic.

The JSON schemas generated here are strict: every field is required (missing
values are ``null``) and no extra properties are allowed, which matches
strict structured-output modes of LLM providers.

Validation is strict too: no type coercion (``"30"`` is not an int), unknown
fields are rejected, ``tool`` is a single name (a list is invalid), and the
chosen tool's required inputs must be present.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.temporal.contracts import FinalOutputContent, ReasoningDecision
from app.tools.registry import missing_required_inputs

REASONING_SCHEMA_NAME = "reasoning_decision"
FINAL_OUTPUT_SCHEMA_NAME = "final_output"

ToolName = Literal["get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"]


class InvalidLLMOutputError(ValueError):
    """The model's output is not valid JSON for the expected schema."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolInputModel(_Strict):
    reason: Optional[str]
    priority: Optional[Literal["low", "medium", "high"]]
    message: Optional[str]


class MemoryUpdateModel(_Strict):
    situation_summary: str
    open_concerns: List[str]


class ReasoningDecisionModel(_Strict):
    assessment: str = Field(min_length=1)
    tool: Optional[ToolName]
    tool_input: ToolInputModel
    next_wake_in_minutes: Optional[int]
    memory_update: MemoryUpdateModel

    @model_validator(mode="after")
    def _tool_inputs_present(self) -> "ReasoningDecisionModel":
        if self.tool is not None:
            missing = missing_required_inputs(self.tool, self.tool_input.model_dump())
            if missing:
                raise ValueError(f"tool {self.tool!r} requires non-empty {missing}")
        return self

    def to_contract(self) -> ReasoningDecision:
        return ReasoningDecision(
            assessment=self.assessment,
            tool=self.tool,
            tool_input={k: v for k, v in self.tool_input.model_dump().items() if v is not None},
            next_wake_in_minutes=self.next_wake_in_minutes,
            situation_summary=self.memory_update.situation_summary,
            open_concerns=list(self.memory_update.open_concerns),
        )


class FinalOutputModel(_Strict):
    summary: str = Field(min_length=1)
    key_actions: List[str]
    key_learnings: List[str]
    recommendations: List[str]

    def to_contract(self) -> FinalOutputContent:
        return FinalOutputContent(
            summary=self.summary,
            key_actions=list(self.key_actions),
            key_learnings=list(self.key_learnings),
            recommendations=list(self.recommendations),
        )


def _parse(model: type, raw: str):
    try:
        return model.model_validate_json(raw)
    except ValidationError as err:
        raise InvalidLLMOutputError(f"{model.__name__} validation failed: {err}") from err


def parse_reasoning_decision(raw: str) -> ReasoningDecision:
    return _parse(ReasoningDecisionModel, raw).to_contract()


def parse_final_output(raw: str) -> FinalOutputContent:
    return _parse(FinalOutputModel, raw).to_contract()


def reasoning_decision_json_schema() -> dict:
    return ReasoningDecisionModel.model_json_schema()


def final_output_json_schema() -> dict:
    return FinalOutputModel.model_json_schema()
