"""Prompt construction for the reasoning and final-output LLM calls.

The user prompt is the structured context serialized as JSON: compact memory,
new and recent events, instructions, enabled tools and wake bounds. The full
historical timeline is never included.
"""

import dataclasses
import json
from datetime import datetime
from typing import Any

from app.temporal.contracts import FinalOutputInput, ReasoningInput
from app.tools.registry import TOOL_SPECS

REASONING_SYSTEM_PROMPT = """\
You are an order supervisor overseeing ONE customer order. You are woken only
when the order starts, when an important event arrives, when a human adds an
instruction, or when a scheduled review is due.

Each time you wake you must return ONE JSON decision matching the schema:
- assessment: a short summary of your reasoning.
- tool: at most ONE tool from enabled_tools, or null if no action is needed.
- tool_input: inputs for that tool (null for fields it does not need).
- next_wake_in_minutes: when to review again, or null for the default.
- memory_update: a compact rewritten situation_summary and up to 5 open_concerns.

Rules: follow the supervisor instructions and every run instruction. Never
invent facts that are not in the context. You cannot act except through one
tool; the system executes it, not you.
"""

FINAL_OUTPUT_SYSTEM_PROMPT = """\
The order you supervised has reached a terminal status. Using ONLY the facts
in the context, return a JSON final report: summary, key_actions,
key_learnings, recommendations. Do not invent anything unsupported by the
context.
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def build_reasoning_user_prompt(context: ReasoningInput) -> str:
    payload = dataclasses.asdict(context)
    payload["enabled_tools"] = [
        {
            "name": name,
            "description": TOOL_SPECS[name].description,
            "required_inputs": list(TOOL_SPECS[name].required_inputs),
        }
        for name in context.enabled_tools
        if name in TOOL_SPECS
    ]
    return json.dumps(payload, default=_json_default, indent=2)


def build_final_output_user_prompt(context: FinalOutputInput) -> str:
    return json.dumps(dataclasses.asdict(context), default=_json_default, indent=2)
