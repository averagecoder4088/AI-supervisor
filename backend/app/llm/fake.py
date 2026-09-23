"""Deterministic in-process LLM client for tests (no network, no API key).

Scripted responses are consumed in order. Each response may be:
- a ``str``: returned as the raw model output (valid JSON or deliberately not);
- an ``Exception`` instance: raised (e.g. ``LLMAuthenticationError``);
- an async callable ``(**call) -> str``: awaited (e.g. to block until released).

With no script left, it returns a valid default for the requested schema:
a no-tool reasoning decision, or a minimal final output.
"""

import json
from typing import Any, Deque, Dict, List, Optional, Sequence
from collections import deque

from app.llm.schemas import FINAL_OUTPUT_SCHEMA_NAME


def make_decision_json(
    *,
    assessment: str = "No action needed right now.",
    tool: Optional[str] = None,
    reason: Optional[str] = None,
    priority: Optional[str] = None,
    message: Optional[str] = None,
    next_wake_in_minutes: Optional[int] = None,
    situation_summary: str = "Order is progressing normally.",
    open_concerns: Optional[List[str]] = None,
) -> str:
    return json.dumps(
        {
            "assessment": assessment,
            "tool": tool,
            "tool_input": {"reason": reason, "priority": priority, "message": message},
            "next_wake_in_minutes": next_wake_in_minutes,
            "memory_update": {
                "situation_summary": situation_summary,
                "open_concerns": open_concerns or [],
            },
        }
    )


def make_final_output_json(
    *,
    summary: str = "Order supervised to completion.",
    key_actions: Optional[List[str]] = None,
    key_learnings: Optional[List[str]] = None,
    recommendations: Optional[List[str]] = None,
) -> str:
    return json.dumps(
        {
            "summary": summary,
            "key_actions": key_actions or [],
            "key_learnings": key_learnings or [],
            "recommendations": recommendations or [],
        }
    )


class FakeLLMClient:
    def __init__(self, responses: Optional[Sequence[Any]] = None) -> None:
        self._responses: Deque[Any] = deque(responses or [])
        self.calls: List[Dict[str, Any]] = []

    def add_response(self, response: Any) -> None:
        self._responses.append(response)

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: Dict[str, Any],
    ) -> str:
        call = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "schema_name": schema_name,
            "json_schema": json_schema,
        }
        self.calls.append(call)
        if not self._responses:
            if schema_name == FINAL_OUTPUT_SCHEMA_NAME:
                return make_final_output_json()
            return make_decision_json()
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return await response(**call)
        return response
