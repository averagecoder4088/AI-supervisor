# Order Supervisor — Final Walkthrough Script

This is the script for the assignment video. It runs about 11 minutes and is recorded live on the real stack: real Temporal, real worker, real Gemini (`gemini-3.1-flash-lite`) and real PostgreSQL. Everything on camera happens in the browser; you never touch a terminal while recording. Two orders are used: `DEMO-2001` runs the whole lifecycle and `DEMO-2002` is terminated at the end.

**Two browser tabs:** the app at http://localhost:3000 and the Temporal UI at http://localhost:8233.

**Rules while recording**
- Say what the screen shows. Don't claim the AI will pick a particular tool; if it picks something else, describe what it actually did.
- The AI decides the tools, so your wording may differ from the lines below. That is fine.
- Pause about 2 seconds on each important result so viewers can read it.

---

## Before you record (not on camera)

**1. Start the four terminals, in this order,** waiting for each to be ready.

```
Terminal 1 — Temporal (wait for "Temporal UI: http://localhost:8233")
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR"
source .venv/bin/activate
python backend/scripts/runtime_validation.py server

Terminal 2 — Worker (prints nothing; that is normal)
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR/backend"
source ../.venv/bin/activate
python -m app.temporal.worker

Terminal 3 — API (wait for "Application startup complete")
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR/backend"
source ../.venv/bin/activate
uvicorn app.main:app --port 8000

Terminal 4 — Frontend (wait for "Ready")
cd "/Users/sanyamvasan/Desktop/PROJECT SUPERVISOR/frontend"
npm run dev
```

The API connects to Temporal only once, at startup, so start Temporal first. Terminal 1 downloads a Temporal binary the first time it runs.

**2. Check Gemini.** `backend/.env` needs `LLM_PROVIDER=gemini` and `GEMINI_API_KEY`. Leave `LLM_MODEL` and `LLM_REASONING_EFFORT` unset, so the model is `gemini-3.1-flash-lite` and no reasoning effort is sent. Run `python backend/scripts/llm_smoke.py` once.

**3. Seed the mock data the tools read** (once, in any terminal). Without these rows every tool call fails with `Order not found`.

```
psql order_supervisor <<'SQL'
INSERT INTO mock_orders (id, order_id, status, customer_id) VALUES
  (gen_random_uuid(), 'DEMO-2001', 'shipped', 'CUSTOMER-2001'),
  (gen_random_uuid(), 'DEMO-2002', 'shipped', 'CUSTOMER-2002');
INSERT INTO mock_shipments (id, order_id, shipment_id, status, tracking_number) VALUES
  (gen_random_uuid(), 'DEMO-2001', 'SHIP-2001', 'in_transit', 'TRACK-2001'),
  (gen_random_uuid(), 'DEMO-2002', 'SHIP-2002', 'in_transit', 'TRACK-2002');
SQL
```

**4. Check the dashboard** shows no active runs.

**5. Rehearse once** with other order IDs (for example `TEST-1` and `TEST-2`, seeded the same way). To clean up a rehearsal order, stop its run in the UI first, then:

```
psql order_supervisor -c "DELETE FROM runs WHERE order_id IN ('TEST-1','TEST-2'); DELETE FROM mock_orders WHERE order_id IN ('TEST-1','TEST-2');"
```

---

## Timeline at a glance

| # | Section | Time | Length |
|---|---|---|---|
| 0 | Opening | 0:00 to 0:20 | 20 s |
| 1 | Create the supervisor | 0:20 to 1:20 | 60 s |
| 2 | Start the order run | 1:20 to 2:20 | 60 s |
| 3 | The agent goes to sleep | 2:20 to 2:50 | 30 s |
| 4 | Send events into the workflow | 2:50 to 3:40 | 50 s |
| 5 | The agent wakes up on its timer | 3:40 to 4:30 | 50 s |
| 6 | Add an instruction to the live run | 4:30 to 5:15 | 45 s |
| 7 | Important event and tool execution | 5:15 to 6:15 | 60 s |
| 8 | Customer message, second tool | 6:15 to 7:00 | 45 s |
| 9 | Look inside Temporal (optional) | 7:00 to 7:30 | 30 s |
| 10 | Pause, resume, interrupt | 7:30 to 8:30 | 60 s |
| 11 | Finish the order | 8:30 to 9:00 | 30 s |
| 12 | Final summary, learnings, feedback | 9:00 to 10:00 | 60 s |
| 13 | Terminate a second run | 10:00 to 10:40 | 40 s |
| 14 | Closing | 10:40 to 11:10 | 30 s |

Times are targets for a clean single take. The wait for the scheduled wake in section 5 can be cut when editing.

---

## 0. Opening — 0:00 to 0:20

**Show:** the dashboard, with no active runs.

**Say:**

"This is my Order Supervisor. A supervisor monitors an order through a long-running Temporal workflow. Events and timers can wake the workflow for another reasoning cycle, while tools perform actions and the system keeps a history of what happened."

**Tip:** don't explain every technology yet.

---

## 1. Create the supervisor — 0:20 to 1:20

**Show:** Create supervisor, then fill in:

| Field | Value |
|---|---|
| Name | `Shipment Supervisor` (it already exists, so this becomes **v3**) |
| Description | `Monitors shipment progress and handles delivery issues.` |
| Base instruction | `Monitor the order until it is delivered. If the shipment is delayed, escalate it with high priority. Keep the customer informed about important shipment issues.` |
| Available tools | leave all four ticked |
| Events that wake the supervisor immediately | keep `shipment_delayed`, `payment_failed`, `refund_requested`, `order_cancelled` ticked, and **also tick `customer_message_received`** |
| Minimum / default / maximum wake (min) | `1` / `1` / `2` |
| Terminal order statuses | `delivered, cancelled` |
| Advanced: status set by each event | leave the defaults |

Click **Create supervisor**. You land on Start run with it selected.

**Say:**

"A supervisor configuration defines how the order is monitored: the base instruction, which tools are available, which events wake it immediately, how long it may sleep, and which order statuses end the run. I set the sleep to one or two minutes only so you can see a scheduled wake-up in this video. In production it would be hours. Supervisors are versioned and immutable, so creating one with an existing name makes a new version."

**Why these values:** a maximum wake of 2 minutes makes sure the scheduled wake happens on camera; the default of 60 would not. `customer_message_received` must be ticked or the customer message in section 8 will not wake the AI.

---

## 2. Start the order run — 1:20 to 2:20

**Show:** on Start run:
- Order ID `DEMO-2001`
- Supervisor: `Shipment Supervisor` (the new version)
- Run-specific instructions: `Prioritize resolving shipment issues quickly and keep the customer informed.`

Click **Start run**. On the run page point at, in order: Workflow status, Reasoning cycles, Memory, Timeline, Tool executions. Then show the Temporal UI tab: `order-DEMO-2001` is Running.

**Say:**

"Starting the run creates a long-running Temporal workflow for this order, named `order-DEMO-2001`. The workflow begins with an initial reasoning cycle."

**Expect:** cycle count 1, last wake reason `workflow_start`. The AI usually calls a read tool such as `get_order_status`, shown as `success` under Tool executions.

---

## 3. The agent goes to sleep — 2:20 to 2:50

**Show:** wait for the first reasoning cycle to finish. Point at the state changing to `sleeping` and **Next scheduled wake** showing a time about a minute away. Pause 3 seconds.

**Say:**

"After the reasoning cycle, the supervisor doesn't keep calling the model. It goes to sleep and waits for its next wake trigger. This scheduled wake is a durable Temporal timer, so nothing is holding a thread while it waits."

---

## 4. Send events into the workflow — 2:50 to 3:40

**Show:** in **Inject an event**, send these three, one at a time, and show each on the Timeline:

1. `order_created` with `{"customer_id": "CUSTOMER-2001"}`
2. `payment_confirmed` with `{"amount": 49.99, "currency": "USD"}`
3. `shipment_created` with `{"shipment_id": "SHIP-2001", "tracking_number": "TRACK-2001"}`

Point out that the reasoning cycle count did not change.

**Say:**

"Events enter the running workflow as Temporal signals. Every event is recorded on the timeline, but only the event types I marked as important wake the supervisor immediately. These are routine updates, so the supervisor keeps sleeping."

---

## 5. The agent wakes up on its timer — 3:40 to 4:30

**Show:** wait for the scheduled wake, about a minute after the first cycle. While you wait, point at the Memory and Timeline panels. When the timer fires, point at the reasoning cycle count going up, **Last wake reason** `scheduled_wakeup`, and the Memory and Timeline updating.

**Say (while waiting):** "Memory is a small summary the AI rewrites each cycle. The timeline is the full history."

**Say (when it wakes):** "The timer fired, so the workflow woke and ran another reasoning cycle. The workflow was alive the whole time, but the model only runs when there is a reason to wake it."

**Tip:** cut the waiting when you edit.

---

## 6. Add an instruction to the live run — 4:30 to 5:15

**Show:** find **Add an instruction for this run** and type:

`For future shipment delays, prioritize escalation and keep the customer updated.`

Click **Add instruction**. Show it under Instructions, then the new reasoning cycle if one appears (last wake reason `instruction_added`).

**Say:**

"I can also guide a run that is already live. The instruction becomes part of the run's context and wakes the supervisor so it reconsiders the situation."

---

## 7. Important event and tool execution — 5:15 to 6:15

**Show:** inject `shipment_delayed` with `{"delay_reason": "Carrier capacity shortage"}`. Show it on the Timeline. The supervisor should wake at once (last wake reason `important_event`). Open **Tool executions** and point at the new row: the tool name, its input and its result.

**Say:**

"Shipment delayed is an important event for this supervisor, so it wakes the workflow immediately. The model returns a structured decision, and the tool itself runs separately as a Temporal Activity." Pointing at the tool row: "The result is saved as part of the run history."

**Expect:** usually `escalate_shipment` with status `success`.

**Important:** if the AI chose a different tool, or none, say what it actually did, for example "Here the supervisor decided to ...". Don't say it was guaranteed. Injected events don't change the mock tables, so don't claim the shipment record changed.

---

## 8. Customer message, second tool — 6:15 to 7:00

**Show:** inject `customer_message_received` with `{"message": "Where is my order?"}`. Show the event on the Timeline, the new reasoning cycle, and Tool executions.

**Say:**

"The same workflow reacts to another important event. Here the customer wrote in, so the supervisor can use the customer-update tool to reply."

**Expect:** usually `send_customer_update` with status `success`. If it chose something else, describe what the screen shows.

---

## 9. Look inside Temporal — 7:00 to 7:30 (optional)

**Show:** the Temporal UI tab. Open `order-DEMO-2001` and its event history. Point at the signals, the timers and the activities.

**Say:**

"This is the same run inside Temporal. My events arrived as signals, every sleep is a timer, and each model call, tool call and database write is an activity. The workflow itself stays deterministic."

**Tip:** skip this section if you are running long.

---

## 10. Pause, resume, interrupt — 7:30 to 8:30

**Show:** back in the app, on the run page:

1. Click **Pause**. Point at the state showing `paused`.
2. Inject `no_update_for_n_hours` with `{"hours": 24}`. Show it on the Timeline and that the cycle count does not change.
3. Click **Resume**. Show a new cycle with last wake reason `resume`.
4. Click **Interrupt**. Show the interrupt count going up.

**Say:**

"Pause keeps the workflow alive but stops reasoning. The event is still recorded but wakes nothing. Resume makes the supervisor re-evaluate everything that arrived meanwhile. Interrupt drops a reasoning cycle in progress without ending the run."

**Tip:** Interrupt only has something to drop while a cycle is running, so the count may just go up. Say that plainly if it happens.

---

## 11. Finish the order — 8:30 to 9:00

**Show:** inject `delivered` with `{"shipment_id": "SHIP-2001"}`. Show the run reaching its terminal state (workflow state `terminal`, run status `completed`).

**Say:**

"The order has reached a configured terminal status, delivered. That comes from the supervisor's event mapping, not from the model. The workflow stops its normal reasoning loop and generates the final output for the run."

---

## 12. Final summary, learnings and feedback — 9:00 to 10:00

**Show:** scroll to **Final output** and slowly point at the Summary, Key actions, Key learnings and Recommendations (the feedback). Check that the source line says the LLM wrote it. Go to the dashboard and show `DEMO-2001` under "Completed and ended runs". Open it again to show the Timeline, Actions and Tool executions. Pause 3 seconds on the final output.

**Say:**

"The run is completed and produced a final report: a summary, the important actions taken, key learnings and recommendations, which is the feedback. The run also keeps its timeline, memory, actions and tool executions, so I can inspect what the supervisor did afterwards."

**If the source says `fallback`:** the model call failed and this is not a real-model result. Retake from section 11, or the whole run.

---

## 13. Terminate a second run — 10:00 to 10:40

**Show:**
1. Go to Start run: order ID `DEMO-2002`, the same supervisor. Click **Start run**.
2. On the run page click **Terminate…** and confirm.
3. Show the run as terminated, with no controls and an empty Final output.
4. Optional: in the Temporal UI tab, show `order-DEMO-2002` as Terminated.

**Say:**

"Terminate is a hard stop for the Temporal workflow. Unlike pause or interrupt, the workflow does not continue after it, and no final report is produced."

---

## 14. Closing — 10:40 to 11:10

**Show:** the dashboard with the completed run and the terminated run.

**Say:**

"This shows the full supervisor lifecycle: creating a supervisor, starting an order run, sending events, sleeping and waking through Temporal, executing tools, guiding a live run with instructions, controlling the workflow, and finally producing a summary with learnings and feedback. The tools are mocked over PostgreSQL and the wake policy is rule-based, and the README lists these limitations."

---

## If something goes wrong

| Problem | Fix |
|---|---|
| Timeline says "Reasoning failed ... HTTP 503 or 429" | Gemini is overloaded or out of quota. Nothing is broken. Wait a minute, send another important event, or retake. |
| "Reasoning failed" with no HTTP code | Wrong or empty `GEMINI_API_KEY`. Fix `backend/.env` and restart the worker (terminal 2). |
| Tool executions show `Order not found` | The mock rows are missing. Run the seed SQL and use a fresh order ID. |
| The AI didn't use the tool you expected | Describe what it did, or repeat the event. |
| "Workflow status unavailable" or `TEMPORAL_UNAVAILABLE` | The API started before Temporal. Start Temporal, then restart terminal 3. |
| The scheduled wake never happens | Check the supervisor's wake settings were 1 / 1 / 2 and that the worker (terminal 2) is running. |
| "Order ID already exists" | One run per order ID. Use a new ID. |

---

## Checklist: everything the assignment asks the video to show

- [ ] Creating a supervisor config — section 1
- [ ] Starting an order run — section 2
- [ ] Sending events into the workflow — sections 4, 7, 8, 10, 11
- [ ] The agent going to sleep — section 3
- [ ] The agent waking up — sections 5, 6, 7
- [ ] Tool execution — sections 2, 7, 8
- [ ] Adding extra instructions to a live run — section 6
- [ ] Interrupting or terminating a run — sections 10 and 13
- [ ] Final summary, learnings and feedback — section 12
