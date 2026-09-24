"""S5 - Payment failure and cancellation: scenario configuration and scripted LLM.

The world fails the payment and later cancels the order (mock operational state only); the
supervisor can only INSPECT the order (``get_order_status``). ``order_cancelled`` is both an
important event and a terminal status, so terminal precedence applies: it is recorded and
the run goes straight to final output without another reasoning cycle.

Scenario data only. The world's operational changes live in ``world.py``. The supervisor's
status mapping is written out here (not derived from the simulator's table) so a test can
catch the two drifting apart. The three scripted LLM calls all succeed; the memory values
are SCRIPTED reasoning output, never written by the simulator.
"""

from typing import Any, Dict, List

from app.llm.fake import make_decision_json, make_final_output_json
from app.simulation.s2_delayed_shipment import s2_supervisor_body

S5_ORDER_STATUS_BY_EVENT: Dict[str, str] = {
    "order_created": "created",
    "payment_confirmed": "payment_confirmed",
    "shipment_created": "shipped",
    "shipment_delayed": "delayed",
    "delivered": "delivered",
    "payment_failed": "payment_failed",
    "order_cancelled": "cancelled",
}

S5_PAYMENT_FAILURE_REASON = "card_declined"
S5_CANCELLATION_REASON = "Payment failed and was not retried"

# Scripted memory of the two reasoning cycles (asserted against the persisted snapshots).
S5_MEMORY_SUMMARIES: List[str] = [
    "Order placed; awaiting payment",
    "Payment failed; order awaiting customer action or cancellation",
]


def s5_supervisor_body(name: str) -> Dict[str, Any]:
    """Request body for ``POST /api/supervisors``: S2's configuration plus the two new event mappings.

    S2's wake policy already lists ``payment_failed`` and ``order_cancelled`` as important, and
    its terminal statuses are ``delivered`` and ``cancelled``.
    """
    body = s2_supervisor_body(name)
    body["description"] = "Supervises an order whose payment fails."
    body["order_status_by_event"] = dict(S5_ORDER_STATUS_BY_EVENT)
    return body


def s5_llm_script() -> List[str]:
    """Exactly three LLM calls, in order: start, payment_failed wake, final output. All succeed."""
    return [
        # 1. workflow_start
        make_decision_json(
            assessment="Order just placed; awaiting payment.",
            tool=None,
            situation_summary=S5_MEMORY_SUMMARIES[0],
            open_concerns=["Payment not yet confirmed"],
            next_wake_in_minutes=60,
        ),
        # 2. important_event: payment_failed
        make_decision_json(
            assessment="Payment failed; checking the order's current status.",
            tool="get_order_status",
            situation_summary=S5_MEMORY_SUMMARIES[1],
            open_concerns=["Payment failed", "Order may be cancelled"],
            next_wake_in_minutes=60,
        ),
        # 3. final output (after the terminal order_cancelled; no reasoning cycle in between)
        make_final_output_json(
            summary="The order's payment failed and the order was cancelled.",
            key_actions=["Checked the order status after the payment failure"],
            key_learnings=["A payment failure was detected immediately through an important event"],
            recommendations=["Follow up with customers after a payment failure before the order is cancelled"],
        ),
    ]
