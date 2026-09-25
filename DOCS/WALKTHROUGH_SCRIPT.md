# Order Supervisor — Final Walkthrough Script

This is the script for the assignment video. It runs about 10 to 11 minutes and is recorded live on the real stack: real Temporal, real worker, real Gemini (`gemini-3.1-flash-lite`) and real PostgreSQL. Two orders are used: `DEMO-2001` runs the whole lifecycle and `DEMO-2002` is terminated at the end.

## How the demo is driven

- **You work in the UI.** It creates the supervisor, starts runs, injects events, adds instructions and uses the human controls.
- **The simulator is the outside world.** `backend/scripts/simulate.py` changes the mock tables (a shipment appears, is delayed, is delivered) and then sends the matching event to the run, so the tools always read the same state the event describes.
- **Two sources of events, on purpose.** `pay`, `ship`, `delay` and `deliver` come from the simulator because they change the world. `order_created`, `customer_message_received` and `no_update_for_n_hours` are sent from the UI's event panel.
- **The AI is never scripted.** It decides what to do, when to sleep and which tool to call, so wording and tool choices vary from take to take.

---

## Before you hit record (not on camera)

**1. Clear the old test runs.** Two ghost runs from earlier testing (`DEMO-1001`, `DEMO-1002`) are still marked `running`. They would show as active on the dashboard.

```bash
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR" && source .venv/bin/activate
python backend/scripts/simulate.py reset DEMO-1001 --yes
python backend/scripts/simulate.py reset DEMO-1002 --yes
```

**2. Open five terminals, and start 1 to 4 in this order.**

```
Terminal 1 — Temporal
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR"
source .venv/bin/activate
python backend/scripts/runtime_validation.py server

Terminal 2 — Worker
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR/backend"
source ../.venv/bin/activate
python -m app.temporal.worker

Terminal 3 — API
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR/backend"
source ../.venv/bin/activate
uvicorn app.main:app --port 8000

Terminal 4 — Frontend
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR/frontend"
npm run dev

Terminal 5 — Simulator (used from section 2 on)
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR"
source .venv/bin/activate
```

Wait for terminal 1 to be up before starting the API. The API connects to Temporal only once, at startup. Terminal 1 downloads a Temporal binary the first time it runs, so run it once before recording.

**3. Check Gemini.** `backend/.env` needs `LLM_PROVIDER=gemini` and `GEMINI_API_KEY`. Leave `LLM_MODEL` and `LLM_REASONING_EFFORT` unset, so the model is `gemini-3.1-flash-lite` and no reasoning effort is sent. Run `python backend/scripts/llm_smoke.py` once. Gemini has returned 503 and 429 errors before.

**4. Rehearse once** with different order IDs, then reset them with `simulate.py reset ORDER --yes`.

**5. Arrange the screen.**
- Browser tab 1: the app, http://localhost:3000.
- Browser tab 2: the Temporal UI, http://localhost:8233.
- Browser tab 3: the system-architecture diagram in the README on GitHub.

---

## 0. Opening — 20 seconds

**Show:** the dashboard, with no active runs.

**Say:**

"This is my Order Supervisor. The system uses one long-running Temporal workflow per order. A supervisor defines the monitoring behavior and the tools it may use, and events, timers and human instructions can wake the workflow for another reasoning cycle."

**Tip:** don't explain every technology yet. The architecture diagram is optional here; if you want it, show README tab 3 for 10 seconds.

---

## 1. Create the Supervisor — 60 seconds

**Show:** Create supervisor, then fill in:

| Field | Value |
|---|---|
| Name | `Shipment Supervisor` (it already exists, so this creates **v3**) |
| Base instruction | `Monitor the order until it is delivered. Escalate serious shipment delays and keep the customer informed.` |
| Available tools | all four ticked |
| Events that wake the supervisor immediately | keep `shipment_delayed`, `payment_failed`, `refund_requested`, `order_cancelled`, and **also tick `customer_message_received`** |
| Minimum / default / maximum wake (min) | `1` / `1` / `2` |
| Terminal order statuses | `delivered, cancelled` |
| Advanced: status set by each event | leave the defaults (`delivered` sets `delivered`) |

Click **Create supervisor**. You land on Start run with it selected.

**Say:**

"A supervisor is a reusable template: the base instruction, the tools it may use, which events wake it immediately, how long it may sleep, and which order statuses end a run. Those wake settings control how aggressively the agent wakes up. I set the sleep to one or two minutes only so you can see a scheduled wake-up in this video. In production it would be hours. Supervisors are versioned and immutable: creating one with an existing name makes the next version, so a run's configuration can never change underneath it."

**Tip:** don't reuse the existing `Shipment Supervisor` v2. It has a maximum sleep of 1440 minutes, so the AI could ask to sleep for an hour and you would never see a scheduled wake. It also doesn't list `customer_message_received` as an important event.

---

## 2. Start the Run — 60 seconds

**Show:**
1. In terminal 5: `python backend/scripts/simulate.py place DEMO-2001` (the customer places the order: a mock order row with status `created`, no event).
2. In the app, Start run: order ID `DEMO-2001`, supervisor `Shipment Supervisor` (v3), run-specific instruction `Prioritize speed over cost.` Click **Start run**.
3. On the run page, point at Workflow status and Tool executions.
4. Switch to the Temporal UI, open `order-DEMO-2001`, point at the timer.

**Say:**

"One run per order, and one Temporal workflow named `order-DEMO-2001`. The first reasoning cycle fires on workflow start. The supervisor looks at the order, may call a read tool, then goes to sleep. That sleep is a real Temporal timer. Nothing holds a thread while it waits."

**Expect:** cycle count 1, last wake reason `workflow_start`. The supervisor usually calls `get_order_status`, shown as `success`. Then the state becomes `sleeping` and **Next scheduled wake** shows a time about a minute away.

---

## 3. Events That Do Not Wake the AI, Then a Scheduled Wake — 90 seconds

**Show:**
1. In the UI's **Inject an event** panel, send `order_created` with `{"customer_id": "CUSTOMER-2001"}`.
2. In terminal 5: `python backend/scripts/simulate.py pay DEMO-2001`.
3. In terminal 5: `python backend/scripts/simulate.py ship DEMO-2001`.
4. Wait, talking over the Memory and Timeline panels, until the timer fires.

**Say:**

"Every incoming event is recorded, but not every event wakes the AI. The wake policy is a simple rule: only the event types I marked important wake it. These are routine, so they go on the timeline and the supervisor keeps sleeping. Memory is a small summary the AI rewrites each cycle, and the timeline is the full history. Nothing happens until the durable timer fires… and there it is."

**Expect:** the three events on the Timeline with the cycle count still at 1. Then a new cycle with last wake reason `scheduled_wakeup`, and the Memory panel updates.

**Tip:** the wait is about a minute. Fill it by explaining memory versus timeline, or cut it when editing.

---

## 4. Add an Instruction to the Live Run — 45 seconds

**Show:** **Add an instruction for this run**, type:

`If the shipment is delayed, escalate it immediately with priority high. If the customer writes in, reply with a short update using send_customer_update.`

Click **Add instruction**.

**Say:**

"I can steer a run that is already live. The instruction becomes part of the run's context and wakes the supervisor to take it into account."

**Expect:** the instruction listed under Instructions, then a new cycle with last wake reason `instruction_added`.

---

## 5. An Important Event and Tool Execution: Escalation — 60 seconds

**Show:** in terminal 5: `python backend/scripts/simulate.py delay DEMO-2001`. Then, once the cycle finishes: `python backend/scripts/simulate.py show DEMO-2001`.

**Say:**

"The carrier reports a delay. The simulator sets the shipment and the order to delayed and then sends the `shipment_delayed` event. This one is important, so the supervisor wakes immediately. The AI only proposes a decision. The workflow validates it and runs the tool as a Temporal Activity, and the result is recorded."

**Expect:** last wake reason `important_event`. Tool executions shows `escalate_shipment` as `success`. `show` prints the shipment as `delayed` with `escalated=True`.

---

## 6. A Customer Message: Second Tool — 45 seconds

**Show:** in the UI's event panel, send `customer_message_received` with `{"message": "Where is my order?"}`. Then run `python backend/scripts/simulate.py show DEMO-2001`.

**Say:**

"The customer writes in. That is also an important event, and my instruction told the supervisor to reply. Tools that change the world get exactly one attempt, so a retry can never send the customer two messages."

**Expect:** an important-event wake, then `send_customer_update` as `success`. `show` lists one outbound message.

---

## 7. Inside Temporal — 30 seconds

**Show:** the Temporal UI, `order-DEMO-2001`, event history. Point at the Signals, the timers and the Activities.

**Say:**

"This is the whole run in Temporal. My events arrived as Signals, every sleep is a timer, and every LLM call, tool call and database write is an Activity. The workflow itself stays deterministic."

---

## 8. Human Controls: Pause, Resume, Interrupt — 60 seconds

**Show:**
1. **Pause**. In the event panel, send `no_update_for_n_hours` with `{"hours": 24}`.
2. **Resume**.
3. **Interrupt**.

**Say:**

"Pause keeps the workflow alive but stops reasoning. The event is still recorded, but it wakes nothing. Resume makes the supervisor re-evaluate everything that arrived while it was paused. Interrupt drops the reasoning cycle in progress without ending the run."

**Expect:** while paused the state is `paused`, the cycle count does not change and the event is on the timeline. After Resume, a cycle with last wake reason `resume`. After Interrupt the interrupt count goes up.

**Tip:** Interrupt has no cycle to drop unless one is running, so it will probably just increase the count. Say so rather than hiding it. Interrupt has not been checked with a real LLM.

---

## 9. Finish the Order — 30 seconds

**Show:** in terminal 5: `python backend/scripts/simulate.py deliver DEMO-2001`.

**Say:**

"The order is delivered. A run ends when the order reaches a terminal status, and that comes from the supervisor's event mapping, not from the LLM. The workflow stops reasoning and writes the final report."

---

## 10. Final Summary, Learnings and Feedback — 60 seconds

**Show:** the run page after it completes. Scroll to **Final output**. Then go to the dashboard and show `DEMO-2001` under completed runs, and open it again to show the Timeline, Actions and Tool executions.

**Say:**

"The run is completed. The final report has a summary, the important actions taken, key learnings and recommendations, which is the feedback. It was written by the LLM from the recorded history. Everything the supervisor did is here: the memory, the timeline, every action and every tool call."

**Expect:** workflow state `terminal`, run status `completed`, and a Final output panel with summary, key actions, key learnings and recommendations.

**Check:** the source line must say the **LLM** wrote it. If it says `fallback`, the LLM call failed and this is not a real-LLM result. Retake it.

---

## 11. Terminate a Second Run — 40 seconds

**Show:**
1. In terminal 5: `python backend/scripts/simulate.py place DEMO-2002`.
2. Start a run for `DEMO-2002` with the same supervisor.
3. Click **Terminate…** and confirm.
4. Show the Temporal UI: `order-DEMO-2002` is terminated.

**Say:**

"Terminate is Temporal's hard stop. It is not Pause and not Interrupt. The workflow does not continue, and no final report is produced."

**Expect:** the run shows terminated, the controls are gone, and Final output is empty.

---

## 12. Closing — 30 seconds

**Show:** the dashboard with the completed and the terminated run.

**Say:**

"One Temporal workflow per order, events as Signals, a wake policy that decides when the AI runs, tools through Activities, a compact memory plus a full timeline, and a final report. The tools are mocked over PostgreSQL and the wake policy is rule-based, and the README lists those limitations. The backend has 313 tests, and the code and documentation are on GitHub."

---

## If something goes wrong

| Problem | Fix |
|---|---|
| Timeline says "Reasoning failed ... HTTP 503 or 429" | Gemini is overloaded or out of quota. Nothing is broken. Wait a minute and inject an important event to retry, or retake. |
| "Reasoning failed" with no HTTP code | Wrong or empty `GEMINI_API_KEY`. Fix `backend/.env` and restart the worker (terminal 2). |
| Tool executions show `Order not found` | The run was started before `simulate.py place ORDER`. Place the order, or use a fresh order ID. |
| The AI did not pick `escalate_shipment` or `send_customer_update` | The LLM chooses the tools. Repeat the step, or reword the run instruction to name the tool. |
| `refused: cannot ...` from the simulator | Steps must go in order: `place`, `pay`, `ship`, then `delay` and `deliver`. `simulate.py show ORDER` shows where the order is. |
| `no run exists for ORDER` | Start the run in the UI first. |
| `could not reach the API` | The API (terminal 3) is not running on port 8000. |
| "Workflow status unavailable" or `TEMPORAL_UNAVAILABLE` | The API started before Temporal. Start Temporal, then restart the API. |
| No scheduled wake in section 3 | Check the supervisor's wake interval is 1 to 2 minutes and that the worker (terminal 2) is running. |
| Order ID refused as already existing | One run per order ID. Use a new ID. |

---

## Checklist: everything the assignment asks the video to show

- [ ] Creating a supervisor config — section 1
- [ ] Starting an order run — section 2
- [ ] Sending events into the workflow — sections 3, 5, 6, 8, 9
- [ ] The agent going to sleep and waking up — sections 2 and 3 (the timer, `scheduled_wakeup`), 5 (`important_event`), 8 (`resume`)
- [ ] Tool execution — sections 2, 5, 6
- [ ] Adding extra instructions to a live run — section 4
- [ ] Interrupting or terminating a run — sections 8 (interrupt) and 11 (terminate)
- [ ] Final summary, learnings and feedback — section 10
