"""S2 - Delayed shipment: the scenario's supervisor configuration and scripted LLM.

Scenario data only. The world's operational changes live in ``world.py``; the
memory values below are the SCRIPTED reasoning output, never written by the
simulator. The supervisor's status mapping is written out here (not derived from
the simulator's table) so a test can catch the two drifting apart.
"""

from typing import Any, Dict, List

from app.llm.fake import make_decision_json, make_final_output_json
from app.simulation.s1_smooth_delivery import s1_supervisor_body

S2_ORDER_STATUS_BY_EVENT: Dict[str, str] = {
    "order_created": "created",
    "payment_confirmed": "payment_confirmed",
    "shipment_created": "shipped",
    "shipment_delayed": "delayed",
    "delivered": "delivered",
}

# The human/operator instruction, sent through the existing instructions API.
S2_INSTRUCTION = "If shipment is delayed, escalate immediately."

S2_DELAY_REASON = "Carrier capacity shortage"
S2_CUSTOMER_QUESTION = "Where is my order?"
S2_ESCALATION_REASON = "Shipment delayed: carrier capacity shortage"
S2_CUSTOMER_UPDATE = (
    "Your order is delayed because of carrier capacity. "
    "We have escalated it to our operations team and will keep you updated."
)

# Requested by the scripted decision of cycle 4 (the default is 60).
S2_REQUESTED_WAKE_MINUTES = 45

# Scripted memory, in cycle order (asserted against the persisted snapshots).
S2_MEMORY_SUMMARIES: List[str] = [
    "Order placed; awaiting payment",
    "Shipment created and in progress; standing instruction: escalate immediately if delayed",
    "Shipment delayed by carrier capacity shortage; escalated to operations",
    "Shipment delayed and escalated; customer informed of the delay",
    "Shipment still delayed but escalated; customer informed; monitoring until delivery",
]


def s2_supervisor_body(name: str) -> Dict[str, Any]:
    """Request body for ``POST /api/supervisors``: S1's configuration plus ``shipment_delayed``.

    ``shipment_delayed`` and ``customer_message_received`` are important events (they wake
    the supervisor); the S1 body already lists both. S1's own mapping is not modified.
    """
    body = s1_supervisor_body(name)
    body["description"] = "Supervises a delayed shipment."
    body["order_status_by_event"] = dict(S2_ORDER_STATUS_BY_EVENT)
    return body


def s2_llm_script() -> List[str]:
    """Exactly six LLM calls, in order: five reasoning cycles and the final output."""
    return [
        # 1. workflow_start
        make_decision_json(
            assessment="Order just placed; nothing to inspect yet.",
            tool=None,
            situation_summary=S2_MEMORY_SUMMARIES[0],
            open_concerns=["Payment not yet confirmed"],
            next_wake_in_minutes=60,
        ),
        # 2. instruction_added
        make_decision_json(
            assessment="A run instruction was added: escalate immediately if the shipment is delayed.",
            tool=None,
            situation_summary=S2_MEMORY_SUMMARIES[1],
            open_concerns=[],
            next_wake_in_minutes=60,
        ),
        # 3. important_event: shipment_delayed
        make_decision_json(
            assessment="The shipment is delayed; the run instruction says to escalate immediately.",
            tool="escalate_shipment",
            reason=S2_ESCALATION_REASON,
            priority="high",
            situation_summary=S2_MEMORY_SUMMARIES[2],
            open_concerns=["New delivery date unknown", "Customer has not been informed"],
            next_wake_in_minutes=60,
        ),
        # 4. important_event: customer_message_received
        make_decision_json(
            assessment="The customer is asking about the delayed order; reply with the current status.",
            tool="send_customer_update",
            message=S2_CUSTOMER_UPDATE,
            situation_summary=S2_MEMORY_SUMMARIES[3],
            open_concerns=["New delivery date unknown"],
            next_wake_in_minutes=S2_REQUESTED_WAKE_MINUTES,
        ),
        # 5. scheduled_wakeup
        make_decision_json(
            assessment="Scheduled review: confirming the shipment state after escalation.",
            tool="get_shipment_status",
            situation_summary=S2_MEMORY_SUMMARIES[4],
            open_concerns=["New delivery date unknown"],
            next_wake_in_minutes=60,
        ),
        # 6. final output
        make_final_output_json(
            summary="The shipment was delayed by a carrier capacity shortage; it was escalated, the customer was informed, and the order was delivered.",
            key_actions=[
                "Escalated the delayed shipment to operations",
                "Sent the customer an update about the delay",
                "Checked the shipment status at the scheduled review",
            ],
            key_learnings=[
                "The run instruction let the supervisor escalate immediately when the delay was reported",
                "Customer questions during a delay are answered from the current shipment state",
            ],
            recommendations=["Notify customers proactively when a shipment is delayed"],
        ),
    ]
