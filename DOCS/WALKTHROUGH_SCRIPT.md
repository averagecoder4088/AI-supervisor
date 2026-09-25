# Order Supervisor: walkthrough video script

Target length: about 11 minutes, recorded live on the real stack (real Temporal, real worker, real Gemini, real PostgreSQL). Two orders: `DEMO-2001` runs the full lifecycle, `DEMO-2002` is terminated at the end.

## How the demo is driven

- **The UI is where the operator works.** It creates the supervisor, starts runs, injects events, adds instructions and uses the human controls.
- **The simulator is the outside world.** `backend/scripts/simulate.py` changes the mock tables (a shipment appears, is delayed, is delivered) and then sends the matching event to the run. The tools read those tables, so the state and the event always agree.
- **Two sources of events, on purpose.** Events that change the mock world (`payment_confirmed`, `shipment_created`, `shipment_delayed`, `delivered`) come from the simulator. Events that need no world change (`order_created`, `customer_message_received`, `no_update_for_n_hours`) are sent from the UI's event panel, to show the operator flow the assignment asks for.
- **The AI is never scripted.** The LLM decides what to do, when to sleep and which tool to call, so wording and tool choices vary between takes.

## Requirement map

Every item of the assignment's walkthrough list, and where it appears:

| Required in the video | Scene |
|---|---|
| Creating a supervisor config | 3 |
| Starting an order run | 4 |
| Sending events into the workflow | 5, 7, 8, 9, 11 |
| The agent going to sleep and waking up | 4, 5 (timer and `scheduled_wakeup`), 7 (`important_event`), 9 (`resume`) |
| Tool execution | 4, 7, 8 |
| Adding extra instructions to a live run | 6 |
| Interrupting or terminating a run | 9 (interrupt), 12 (terminate) |
| Final summary, learnings and feedback | 11 |

Also shown, because the evaluation looks at them: one workflow per order and Signals in the Temporal UI (Scene 8), the compact memory and full timeline (Scenes 5 and 8), and the active and completed runs lists (Scenes 11 and 13).

---

## Part A. Before recording (not on camera)

### A1. Clean the dashboard

An earlier test left two ghost runs (`DEMO-1001`, `DEMO-1002`), marked `running` although their workflows are gone. They would show under "Active runs". Remove them and their mock rows:

```bash
python backend/scripts/simulate.py reset DEMO-1001 --yes
python backend/scripts/simulate.py reset DEMO-1002 --yes
```

If you rehearse first, reset the rehearsal orders the same way and use fresh order IDs for the recording.

### A2. Check the LLM

- `backend/.env` needs `LLM_PROVIDER=gemini` and `GEMINI_API_KEY`. Restart the worker after any change.
- Run `python backend/scripts/llm_smoke.py`. Gemini has returned 503 and 429 (overload, quota) before. If it fails, wait and retry before recording.
- **Model for the recording: `gemini-3.1-flash-lite`**, the code default. Leave `LLM_MODEL` and `LLM_REASONING_EFFORT` unset in `backend/.env`: the model is chosen by the default and no reasoning effort is sent. The full real end-to-end run recorded in the docs used `gemini-3.6-flash`, not this model, so a full dry run with `gemini-3.1-flash-lite` before recording is essential.

### A3. Start the four processes, in this order

1. Temporal. The `temporal` CLI is not installed on this machine: use `python backend/scripts/runtime_validation.py server`, or `brew install temporal` and `temporal server start-dev`. Web UI: http://localhost:8233.
2. Worker: `cd backend && python -m app.temporal.worker`
3. API: `cd backend && uvicorn app.main:app --port 8000`
4. Frontend: `cd frontend && npm run dev`, then open http://localhost:3000.

Start Temporal before the API. The API only connects once, at startup.

### A4. Screen layout

- Browser tab 1: the app, http://localhost:3000.
- Browser tab 2: the Temporal UI, http://localhost:8233.
- Browser tab 3: the system-architecture diagram in the README on GitHub.
- A terminal in the repo root with the virtualenv active, for the simulator.
- `python backend/scripts/simulate.py show DEMO-2001` prints the run and the mock rows (order status, shipment status, `escalated`, customer messages). Use it to prove that a tool changed the world.

---

## Part B. The script

Each scene has ON SCREEN (what you do), SAY (narration) and LOOK FOR (what should appear).

### Scene 1. What it is (0:00 to 0:45)

**ON SCREEN:** README system-architecture diagram.

**SAY:** "This is Order Supervisor. Every order gets one long-running Temporal workflow. Events reach it as Signals. An LLM decides when to act, when to sleep and when to wake up. Tools do the work, and when the order ends it writes a final report. The browser talks to Next.js, Next.js talks to FastAPI, FastAPI starts and signals the Temporal workflow, and Activities call the LLM, run tools and write to PostgreSQL. The workflow itself stays deterministic."

### Scene 2. The running stack (0:45 to 1:15)

**ON SCREEN:** flash the four terminals, then the Temporal UI (tab 2) with no workflows.

**SAY:** "Four processes: the Temporal server, the worker that hosts the workflow, the API and the UI. Temporal is empty right now."

### Scene 3. Create a supervisor config (1:15 to 2:15)

**ON SCREEN:** app, then Create supervisor. Fill in:

| Field | Value |
|---|---|
| Name | `Demo Supervisor` |
| Base instruction | `Monitor the order until it is delivered. Escalate serious shipment delays and keep the customer informed.` |
| Available tools | tick all four |
| Events that wake the supervisor immediately | keep `shipment_delayed`, `payment_failed`, `refund_requested`, `order_cancelled` ticked, and **also tick `customer_message_received`** |
| Minimum / default / maximum wake (min) | `1` / `1` / `2` |
| Terminal order statuses | `delivered, cancelled` (default) |
| Advanced: order status set by each event | leave the defaults (`delivered` sets `delivered`) |

Click **Create supervisor**. You land on Start run with it selected.

**SAY:** "A supervisor is a reusable template: the base instruction, the tools it may use, which events wake it immediately, how long it may sleep, and which order statuses end a run. Those wake settings are how aggressively the agent wakes up. I set the interval to one or two minutes only so this video shows a scheduled wake-up; in production it would be hours. The event-to-status mapping is how a run reaches a terminal status."

**Why the interval matters:** the UI default is 60 minutes, so a scheduled wake would never happen on camera.

### Scene 4. Start a run and the first reasoning cycle (2:15 to 3:15)

**ON SCREEN:** first, in the terminal: `python backend/scripts/simulate.py place DEMO-2001` (the customer places the order: a mock order row with status `created`, no event). Then in the app: Start run. Order ID `DEMO-2001`, supervisor `Demo Supervisor`, run-specific instructions `Prioritize speed over cost.` Click **Start run**. The run page opens.

**SAY:** "One run per order, and one Temporal workflow named `order-DEMO-2001`. The first reasoning cycle fires on workflow start."

**LOOK FOR:** Workflow status shows a cycle count of 1 and last wake reason `workflow_start`. The supervisor usually calls a read tool such as `get_order_status`, which appears under Tool executions as `success`. Then the state becomes **sleeping** and **Next scheduled wake** shows a time about one minute out.

**ON SCREEN (10 seconds):** switch to the Temporal UI, open `order-DEMO-2001`, point at the timer in the event history.

**SAY:** "The sleep is a real Temporal timer. Nothing holds a thread while it waits."

### Scene 5. Events that do not wake the AI, then a scheduled wake (3:15 to 4:45)

**ON SCREEN:**

1. In the UI's **Inject an event** panel, send `order_created` with `{"customer_id": "CUSTOMER-2001"}`.
2. In the terminal: `python backend/scripts/simulate.py pay DEMO-2001` (order to `payment_confirmed`, then the event).
3. In the terminal: `python backend/scripts/simulate.py ship DEMO-2001` (a shipment appears and the order becomes `shipped`, then the event).

**SAY:** "Every incoming event is recorded, but not every event wakes the AI. The wake policy is a simple rule: only the event types I marked important wake it. These are routine, so they are recorded and the supervisor keeps sleeping."

**LOOK FOR:** the three events on the Timeline. The cycle count stays at 1.

**ON SCREEN:** now stop and wait, talking over the Memory and Timeline panels, until the scheduled time passes (about a minute after the first cycle).

**SAY:** "Memory is a small summary the AI rewrites each cycle. The timeline is the full history. Nothing happens until the durable timer fires. There it is."

**LOOK FOR:** a new cycle with last wake reason `scheduled_wakeup`. The Memory panel updates and the Timeline shows the decision.

### Scene 6. Extra instruction on a live run (4:45 to 5:30)

**ON SCREEN:** **Add an instruction for this run**, type: `If the shipment is delayed, escalate it immediately with priority high. If the customer writes in, reply with a short update using send_customer_update.` Click **Add instruction**.

**SAY:** "I can steer a run that is already live. The instruction becomes part of the run context and wakes the supervisor."

**LOOK FOR:** the instruction under Instructions, then a new cycle with last wake reason `instruction_added`.

### Scene 7. An important event and tool execution: escalate (5:30 to 6:30)

**ON SCREEN:** in the terminal: `python backend/scripts/simulate.py delay DEMO-2001`

**SAY:** "The carrier reports a delay. The simulator sets the shipment and the order to delayed, then sends `shipment_delayed`. The tools read that same state."

**LOOK FOR:** the run wakes at once with last wake reason `important_event`. Under Tool executions, `escalate_shipment` shows `success`. Then run `python backend/scripts/simulate.py show DEMO-2001`: the shipment is `delayed` with `escalated=True`.

**SAY:** "The LLM only proposes a decision. The workflow validates it, runs the tool as a Temporal Activity, and records the action and its outcome."

### Scene 8. Customer message, and a look inside Temporal (6:30 to 7:30)

**ON SCREEN:** in the UI's **Inject an event** panel, send `customer_message_received` with `{"message": "Where is my order?"}`.

**LOOK FOR:** an important-event wake. The supervisor is expected to call `send_customer_update` (the instruction asked for it). Run `simulate.py show DEMO-2001`: the messages list now has an outbound reply.

**SAY:** "Side-effecting tools get exactly one attempt, so a retry can never send the customer two messages."

**ON SCREEN (20 seconds):** switch to the Temporal UI, `order-DEMO-2001`, event history. Point at the Signals, the timers and the Activities (the LLM calls, the tool calls, the database writes).

**SAY:** "This is the whole run in Temporal: my events as Signals, every sleep as a timer, and every LLM call, tool call and database write as an Activity."

### Scene 9. Human controls: pause, resume, interrupt (7:30 to 8:30)

**ON SCREEN:**

1. **Pause**. The state shows `paused`. In the event panel, send `no_update_for_n_hours` with `{"hours": 24}`.
2. **Resume**.
3. **Interrupt**.

**SAY:** "Pause keeps the workflow alive but stops reasoning. The event is still recorded on the timeline, but nothing wakes the AI. Resume makes it re-evaluate everything that arrived meanwhile. Interrupt drops the current reasoning cycle without ending the run."

**LOOK FOR:** while paused the cycle count does not change and the event is on the timeline. After Resume a cycle runs with last wake reason `resume`. After Interrupt the interrupt count increases.

**Note:** an interrupt only has a cycle to drop while one is running, so it will probably just increment the count. Say so. Interrupt with real Gemini has not been verified.

### Scene 10. Finish the order (8:30 to 9:00)

**ON SCREEN:** in the terminal: `python backend/scripts/simulate.py deliver DEMO-2001`

**SAY:** "A run ends when the order reaches a terminal status. That comes from the event mapping, not from the LLM. The workflow stops reasoning and writes the final report."

### Scene 11. The final summary, learnings and feedback (9:00 to 10:00)

**LOOK FOR:** the workflow state becomes `terminal` and the run status becomes `completed`. Scroll to **Final output**: the summary, key actions, key learnings and recommendations (the feedback), and the source line.

**Check:** the source must say the **LLM** wrote it. If it says fallback, the LLM call failed and this is not a real-LLM result, so re-record.

**ON SCREEN:** the dashboard: `DEMO-2001` moves to "Completed and ended runs". Open the run again to show the full Timeline, Actions and Tool executions that were recorded.

### Scene 12. Terminate a second run (10:00 to 10:40)

**ON SCREEN:** `python backend/scripts/simulate.py place DEMO-2002`, then Start run for `DEMO-2002` with the same supervisor. On the run page click **Terminate…**, then confirm.

**SAY:** "Terminate is Temporal's hard stop. It is not Pause and not Interrupt. The workflow does not continue and no final report is produced."

**LOOK FOR:** the run shows terminated, the controls are gone, Final output is empty. In the Temporal UI, `order-DEMO-2002` shows Terminated.

### Scene 13. Wrap-up (10:40 to 11:10)

**ON SCREEN:** the dashboard with the completed and terminated runs, then the repo.

**SAY:** "One Temporal workflow per order, events as Signals, a wake policy that decides when the AI runs, tools through Activities, a compact memory plus a full timeline, and a final report. The tools are mocked over PostgreSQL and the wake policy is rule-based, which the README lists under limitations. The backend has 313 tests, and the code and README are on GitHub."

---

## Part C. Risks and fixes while recording

| Problem | Fix |
|---|---|
| Timeline says "Reasoning failed ... HTTP 503 or 429" | Gemini overload or quota. Nothing is broken. Wait a minute, inject an important event to retry, or re-take. |
| Timeline says "Reasoning failed" with no HTTP code | Wrong or empty `GEMINI_API_KEY`. Fix `backend/.env` and restart the worker. |
| Tool executions show `Order not found` | The run was started before `simulate.py place ORDER`. Place the order, or start a new run with a fresh order ID. |
| Supervisor did not choose `escalate_shipment` or `send_customer_update` | The LLM chooses the tools. Repeat the step, for example send another customer message, or reword the run instruction to name the tool. |
| `refused: cannot ...` from the simulator | Steps must go in order: `place`, `pay`, `ship`, then `delay` and `deliver`. Use `simulate.py show ORDER` to see where the order is. |
| `no run exists for ORDER` | Start the run in the UI first, then use the simulator. |
| `could not reach the API` | The API is not running on port 8000. Start it, or pass `--api URL`. |
| "Workflow status unavailable" or `TEMPORAL_UNAVAILABLE` | The API started before Temporal. Start Temporal, then restart the API. |
| The scheduled wake does not show up in Scene 5 | Check that the supervisor's wake interval was 1 to 2 minutes and that the worker is running. |
| Order ID refused as already existing | One run per order ID. Use a new ID. |

**Backup, and how to be honest about it.** `python backend/scripts/gemini_e2e.py --mock-llm` runs the full pipeline against a local stub whose decisions are scripted. It is fine for rehearsing the pipeline, but it is not a real-LLM demo. If you ever use it in the video, say so.
