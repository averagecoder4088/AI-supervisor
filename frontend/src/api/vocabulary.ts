// The fixed vocabularies of the backend (architecture-frozen): the four supervisor tools and the
// ten event types. Mirrors backend/app/tools/registry.py and backend/app/temporal/constants.py.
// The backend validates these authoritatively; this list only drives the form controls.

export interface ToolInfo {
  name: string;
  description: string;
  /** Side-effecting tools change the (mock) world; read-only tools only look. */
  actsOnTheWorld: boolean;
}

export const TOOLS: ToolInfo[] = [
  { name: "get_order_status", description: "Read the current status of the order.", actsOnTheWorld: false },
  {
    name: "get_shipment_status",
    description: "Read the current shipment / tracking status of the order.",
    actsOnTheWorld: false,
  },
  {
    name: "escalate_shipment",
    description: "Escalate a shipment problem to the operations team (needs a reason and a priority).",
    actsOnTheWorld: true,
  },
  { name: "send_customer_update", description: "Send a message to the customer.", actsOnTheWorld: true },
];

export const EVENT_TYPES = [
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
] as const;

/** Events that wake the supervisor immediately (the architecture's examples). */
export const DEFAULT_IMPORTANT_EVENTS: string[] = [
  "shipment_delayed",
  "payment_failed",
  "refund_requested",
  "order_cancelled",
];

/**
 * Which order status an event sets. Without a mapping no run can ever reach a terminal status
 * through events, so the form pre-fills the mapping used by the sealed scenarios.
 */
export const DEFAULT_STATUS_BY_EVENT: Record<string, string> = {
  order_created: "created",
  payment_confirmed: "payment_confirmed",
  payment_failed: "payment_failed",
  shipment_created: "shipped",
  shipment_delayed: "delayed",
  delivered: "delivered",
  order_cancelled: "cancelled",
};

export const DEFAULT_TERMINAL_STATUSES = "delivered, cancelled";
export const DEFAULT_WAKE_MINUTES = { min: 1, default: 60, max: 1440 };

/**
 * Example payloads (the shapes the sealed simulator sends). The backend accepts any JSON object as
 * the payload, so these are suggestions only and never required.
 */
export const EVENT_PAYLOAD_EXAMPLES: Record<string, string> = {
  order_created: '{"customer_id": "CUST-1"}',
  payment_confirmed: '{"amount": 49.99, "currency": "USD"}',
  payment_failed: '{"reason": "card_declined"}',
  shipment_created: '{"shipment_id": "SHIP-1", "tracking_number": "TRK-1"}',
  shipment_delayed: '{"delay_reason": "Carrier capacity shortage"}',
  delivered: '{"shipment_id": "SHIP-1"}',
  refund_requested: '{"reason": "Changed mind"}',
  customer_message_received: '{"message": "Where is my order?"}',
  no_update_for_n_hours: '{"hours": 24}',
  order_cancelled: '{"reason": "Customer request"}',
};
