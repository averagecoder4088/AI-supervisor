"""Deterministic wake policy: should an incoming event wake the supervisor now?

Signal = "something happened"; wake policy = "should the LLM wake now?".
Temporal does not understand event importance, so the workflow applies this
configured, rule-based policy. Every event is recorded regardless of the answer.
"""

from typing import Iterable

# Defaults taken from the architecture's examples of important events.
DEFAULT_IMPORTANT_EVENT_TYPES = (
    "shipment_delayed",
    "refund_requested",
    "payment_failed",
    "order_cancelled",
)


def is_important_event(event_type: str, important_event_types: Iterable[str]) -> bool:
    """Return True if the event type should trigger immediate reasoning."""
    return event_type in important_event_types
