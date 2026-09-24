"""S1 - Smooth delivery: the scenario's supervisor configuration and scripted LLM.

Scenario data only. The world's operational changes live in ``world.py``; the
memory values below are the SCRIPTED reasoning output, never written by the
simulator.
"""

from typing import Any, Dict, List

from app.llm.fake import make_decision_json, make_final_output_json

# The supervisor's event -> order-status mapping for S1. Written out on purpose (not
# derived from the simulator's table) so a test can catch the two drifting apart.
S1_ORDER_STATUS_BY_EVENT: Dict[str, str] = {
    "order_created": "created",
    "payment_confirmed": "payment_confirmed",
    "shipment_created": "shipped",
    "delivered": "delivered",
}

S1_WAKE_MINUTES = 60


def s1_supervisor_body(name: str) -> Dict[str, Any]:
    """Request body for ``POST /api/supervisors``. None of the four S1 events is important."""
    return {
        "name": name,
        "description": "Supervises order shipment issues.",
        "instructions": "Monitor the order until it is delivered and respond to problems.",
        "wake_policy": {
            "important_event_types": [
                "shipment_delayed",
                "payment_failed",
                "refund_requested",
                "order_cancelled",
                "customer_message_received",
            ]
        },
        "enabled_tools": ["get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"],
        "default_wake_interval_minutes": 60,
        "min_wake_interval_minutes": 1,
        "max_wake_interval_minutes": 1440,
        "terminal_order_statuses": ["delivered", "cancelled"],
        "order_status_by_event": dict(S1_ORDER_STATUS_BY_EVENT),
    }


def s1_llm_script() -> List[str]:
    """Exactly three LLM calls, in order: start, scheduled wake, final output."""
    return [
        make_decision_json(
            assessment="Order just placed; checking its current state.",
            tool="get_order_status",
            situation_summary="Order placed; awaiting payment",
            open_concerns=["Payment not yet confirmed"],
            next_wake_in_minutes=S1_WAKE_MINUTES,
        ),
        make_decision_json(
            assessment="Scheduled review; confirming the shipment is healthy.",
            tool="get_shipment_status",
            situation_summary="Payment confirmed and shipment created; no delay observed",
            open_concerns=[],
            next_wake_in_minutes=S1_WAKE_MINUTES,
        ),
        make_final_output_json(
            summary="Order delivered without any intervention.",
            key_actions=[
                "Checked the order status at workflow start",
                "Checked the shipment status at the scheduled review",
            ],
            key_learnings=["Routine events were recorded without waking the supervisor"],
            recommendations=["No changes needed for a smooth delivery"],
        ),
    ]
