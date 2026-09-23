"""Shared Temporal constants and naming conventions."""

# Single task queue served by the Order Supervisor worker.
TASK_QUEUE = "order-supervisor"

# Number of most-recent events kept in workflow memory. Full event history
# lives in PostgreSQL (persisted by Activities in a later step), not in Temporal.
RECENT_EVENTS_LIMIT = 20


def order_workflow_id(order_id: str) -> str:
    """Return the workflow ID for an order: exactly one workflow per order."""
    return f"order-{order_id}"


# Frozen event vocabulary (problem statement + ``order_cancelled`` from the
# architecture's important-event examples). Used to validate supervisor config.
EVENT_TYPES = frozenset(
    {
        "order_created",
        "payment_confirmed",
        "payment_failed",
        "shipment_created",
        "shipment_delayed",
        "delivered",
        "refund_requested",
        "customer_message_received",
        "no_update_for_n_hours",
        "order_cancelled",
    }
)

# Deterministic caps that keep supervisor memory compact.
MEMORY_SUMMARY_MAX_CHARS = 1000
MEMORY_MAX_OPEN_CONCERNS = 5
MEMORY_CONCERN_MAX_CHARS = 200

# Bounded log of tool outcomes kept in workflow state (for final output).
ACTION_LOG_LIMIT = 20
