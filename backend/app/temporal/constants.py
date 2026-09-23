"""Shared Temporal constants and naming conventions."""

# Single task queue served by the Order Supervisor worker.
TASK_QUEUE = "order-supervisor"

# Number of most-recent events kept in workflow memory. Full event history
# lives in PostgreSQL (persisted by Activities in a later step), not in Temporal.
RECENT_EVENTS_LIMIT = 20


def order_workflow_id(order_id: str) -> str:
    """Return the workflow ID for an order: exactly one workflow per order."""
    return f"order-{order_id}"
