"""S4 - Human controls end to end: scripted LLM, scenario constants and the gated tool.

The operator pauses the supervisor, the world keeps moving while it is paused, the
operator resumes it, interrupts it WHILE a tool is executing, and finally pauses it
again so the order completes while paused.

Scenario data and test infrastructure only. The world's operational changes live in
``world.py`` and the supervisor configuration is S2's (same five events and status
mapping). The three scripted LLM calls all succeed. The memory values are the SCRIPTED
reasoning output, never written by the simulator.

``GatedTool`` wraps the REAL database-backed handler of one tool: it announces that it
started, waits for the test to open a gate, and only then calls the real handler. That
lets the test land an interrupt while the tool is genuinely in flight and then prove the
tool was not cancelled (its real mock-DB side effect still happens).
"""

import asyncio
from typing import Any, Dict, List

from app.llm.fake import make_decision_json, make_final_output_json
from app.simulation.s2_delayed_shipment import S2_ESCALATION_REASON, s2_supervisor_body
from app.temporal.contracts import ToolRequest, ToolResult
from app.tools.registry import ToolHandler

S4_TOOL = "escalate_shipment"

# Scripted memory of the two reasoning cycles (asserted against the persisted snapshots).
S4_MEMORY_SUMMARIES: List[str] = [
    "Order placed; awaiting payment",
    "Shipment delayed; escalated after the operator resumed the supervisor",
]

# Timeline (control) entries written by the workflow for the operator's controls.
CONTROL_PAUSED = "Supervisor paused"
CONTROL_RESUMED = "Supervisor resumed; re-evaluating"
CONTROL_INTERRUPT_DURING_TOOL = "Interrupt received while a tool was executing; the tool is not cancelled"


def s4_supervisor_body(name: str) -> Dict[str, Any]:
    """Request body for ``POST /api/supervisors``: S2's configuration."""
    body = s2_supervisor_body(name)
    body["description"] = "Supervises a delayed shipment under operator control."
    return body


def s4_llm_script() -> List[str]:
    """Exactly three LLM calls, in order: start, resume, final output. All succeed."""
    return [
        # 1. workflow_start
        make_decision_json(
            assessment="Order just placed; awaiting payment.",
            tool=None,
            situation_summary=S4_MEMORY_SUMMARIES[0],
            open_concerns=["Payment not yet confirmed"],
            next_wake_in_minutes=60,
        ),
        # 2. resume: sees the whole backlog recorded while paused
        make_decision_json(
            assessment="The shipment was delayed while I was paused; escalating it now.",
            tool=S4_TOOL,
            reason=S2_ESCALATION_REASON,
            priority="high",
            situation_summary=S4_MEMORY_SUMMARIES[1],
            open_concerns=["New delivery date unknown"],
            next_wake_in_minutes=60,
        ),
        # 3. final output
        make_final_output_json(
            summary="The shipment was delayed while the supervisor was paused; after resume it was escalated and the order was delivered.",
            key_actions=["Escalated the delayed shipment after the operator resumed the supervisor"],
            key_learnings=[
                "Events recorded during a pause reached one reasoning cycle after resume",
                "An interrupt during a running tool did not cancel it",
            ],
            recommendations=["Review shipments that are delayed while the supervisor is paused"],
        ),
    ]


class GatedTool:
    """Wraps a REAL tool handler: signal start, wait for the gate, then call the real handler."""

    def __init__(self, real_handler: ToolHandler) -> None:
        self._real = real_handler
        self.started = asyncio.Event()  # set as soon as the tool Activity is executing
        self.release = asyncio.Event()  # the test opens the gate
        self.calls = 0

    async def __call__(self, request: ToolRequest) -> ToolResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return await self._real(request)
