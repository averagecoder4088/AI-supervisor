"""S3 - LLM unavailable during an incident: scripted LLM and scenario constants.

The LLM is an unreliable dependency. In S3 the scripted client SUCCEEDS at workflow
start, FAILS when the shipment delay wakes the supervisor, SUCCEEDS again at the next
scheduled wake (recovery), and FAILS at the final-output call (deterministic fallback).

Scenario data only. The world's operational changes live in ``world.py`` and the
supervisor configuration is S2's (same five events and status mapping). Failures are
NON-retryable on purpose (``LLMAuthenticationError``): they fail on the first attempt,
so there is no retry backoff and the four scripted responses map one-to-one to the
four LLM calls. Transient-error retries are covered by the Step 4 unit tests.
"""

from typing import Any, Dict, List

from app.llm.client import LLMAuthenticationError
from app.llm.fake import make_decision_json
from app.simulation.s2_delayed_shipment import S2_ESCALATION_REASON, s2_supervisor_body

S3_OUTAGE_MESSAGE = "LLM credentials rejected (simulated outage)"

# Requested by the start cycle. After the failed attempt the workflow must use the
# configured DEFAULT interval (60), not this request.
S3_START_REQUESTED_WAKE_MINUTES = 45

# Scripted memory of the two SUCCESSFUL cycles (the failed attempt writes none).
S3_MEMORY_SUMMARIES: List[str] = [
    "Order placed; awaiting payment",
    "Shipment delayed; escalated once reasoning recovered",
]


def s3_supervisor_body(name: str) -> Dict[str, Any]:
    """Request body for ``POST /api/supervisors``: S2's configuration."""
    body = s2_supervisor_body(name)
    body["description"] = "Supervises a delayed shipment while the LLM is unreliable."
    return body


def s3_llm_script() -> List[Any]:
    """Exactly four LLM calls, in order: start (ok), delay wake (fails), scheduled wake (ok), final output (fails)."""
    return [
        # 1. workflow_start: succeeds
        make_decision_json(
            assessment="Order just placed; nothing to inspect yet.",
            tool=None,
            situation_summary=S3_MEMORY_SUMMARIES[0],
            open_concerns=["Payment not yet confirmed"],
            next_wake_in_minutes=S3_START_REQUESTED_WAKE_MINUTES,
        ),
        # 2. important_event (shipment_delayed): the LLM call fails
        LLMAuthenticationError(S3_OUTAGE_MESSAGE),
        # 3. scheduled_wakeup: recovery
        make_decision_json(
            assessment="The shipment is delayed; escalating it now.",
            tool="escalate_shipment",
            reason=S2_ESCALATION_REASON,
            priority="high",
            situation_summary=S3_MEMORY_SUMMARIES[1],
            open_concerns=["New delivery date unknown"],
            next_wake_in_minutes=60,
        ),
        # 4. final output: the LLM call fails, so the workflow stores its deterministic fallback
        LLMAuthenticationError(S3_OUTAGE_MESSAGE),
    ]
