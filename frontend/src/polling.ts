// Polling settings for the run page. Frontend only; no backend contract is involved.

/**
 * How often an ACTIVE run's page refreshes itself. About 3 seconds is deliberate: this is observation
 * freshness for a POC, not realtime infrastructure (no WebSocket / SSE / push). Do not go below 1 s.
 */
export const POLL_INTERVAL_MS = 3000;

/**
 * When the workflow query reports the workflow CLOSED (409) while the run row still says active, that is
 * normally a moment of lag between the two records, so the page is asked again before polling gives up:
 * polling stops once the workflow has been reported closed on this many consecutive renders.
 */
export const CLOSED_CONFIRMATIONS = 2;

/**
 * How long a page render waits for the workflow status (GET /status, a Temporal query). The backend itself waits
 * up to 5 s for Temporal, so with Temporal down a render took 6-10 s; and because polling renders the page every
 * few seconds, and the router runs a form's Server Action in the same queue as a refresh, slow renders made forms
 * wait behind them. Bounding this read keeps every render short: a dead Temporal shows "Workflow status
 * unavailable" after this long, and everything else (PostgreSQL) is unaffected.
 */
export const STATUS_READ_TIMEOUT_MS = 3000;
