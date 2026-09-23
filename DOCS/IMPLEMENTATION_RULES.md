# Order Supervisor — Implementation Rules & Guardrails

This document is the implementation contract for this project. It serves two purposes:

- **For the developer:** the reference used to avoid drifting from the agreed architecture during implementation.
- **For Antigravity (or any implementation agent):** the rules included in every implementation prompt, defining what it is allowed to change and what it must not change.

---

## 1. Source of Truth

There are three primary sources of truth, in this order:

### 1. Problem Statement
The assignment requirements are mandatory. Anything explicitly required by the problem statement must not be removed, replaced, or weakened because of implementation convenience.

### 2. Frozen Architecture
The agreed architecture defines how those requirements are implemented. The architecture should remain stable throughout implementation.

### 3. Implementation Decisions
Code-level decisions can be adjusted when necessary, provided they do not violate the problem statement or architecture.

---

## 2. No Unnecessary Architecture Changes

The architecture is considered frozen for implementation.

Do not redesign components simply because another approach appears:
- simpler
- newer
- more popular
- easier to code
- more convenient for a particular library

An architectural change is allowed only when implementation reveals a genuine major technical problem.

If such a problem appears:

```
Stop → explain the problem → discuss it → explicitly decide whether to change
the architecture → update the architecture reference → continue.
```

**Never silently change architecture.**

---

## 3. Problem-Statement Requirements Are Mandatory

The implementation must preserve all explicitly required assignment capabilities, including among others:

- Next.js App Router
- Tailwind CSS
- Python FastAPI
- Temporal Python SDK (`temporalio`)
- PostgreSQL/Supabase
- one long-running Temporal workflow per order
- Temporal Signals for order events
- scheduled wake-ups
- AI reasoning on the defined wake conditions
- mocked/simulated tools
- event simulation
- supervisor configuration
- run inspection
- timeline/memory/action visibility
- run-specific instructions
- interrupt/pause/resume/terminate controls
- terminal-state handling
- final output generation

The implementation must not remove required functionality merely to simplify the code.

---

## 4. Fixed Technology Stack

Unless explicitly decided otherwise:

| Layer | Technology |
|---|---|
| Frontend | Next.js App Router, Tailwind CSS |
| Backend | Python, FastAPI |
| Workflow | Temporal, Temporal Python SDK (`temporalio`) |
| Database | PostgreSQL / Supabase |
| ORM | SQLAlchemy / SQLModel |
| LLM | Direct SDK-based structured LLM calls |
| Orchestration | Temporal |
| Wake Policy | Rule-based / lightweight classifier |

Do not introduce another major framework or orchestration system without discussion.

---

## 5. Architectural Responsibilities Must Remain Separate

These boundaries are important.

### FastAPI
Responsible for:
- receiving API requests
- validation
- interacting with PostgreSQL where appropriate
- starting Temporal workflows
- sending Temporal Signals
- exposing run/configuration data to the frontend

FastAPI does not become the workflow engine.

### Temporal
Responsible for:
- durable workflow execution
- workflow lifecycle
- waiting
- timers
- Signals
- workflow state
- pause/resume/interrupt/termination behavior
- coordinating Activities

Temporal is the orchestration layer.

### PostgreSQL
Responsible for:
- application data
- event history
- timeline
- actions
- tool execution records
- memory snapshots
- final outputs
- supervisor/run configuration

PostgreSQL is the application/history database. Do not turn Temporal into the application's historical database.

### Workflow
Responsible for:
- determining when the supervisor should reason
- maintaining compact operational state
- receiving Signals
- applying wake policy
- coordinating Activities
- waiting for events/timers
- controlling the lifecycle of the order supervisor

The workflow should remain deterministic.

### Activities
Responsible for non-deterministic/external operations such as:
- LLM calls
- PostgreSQL operations requiring external I/O
- tool execution
- final-output generation
- other external services

External I/O should not be placed directly inside workflow code.

### LLM
Responsible for reasoning and deciding what should happen. The LLM does not directly perform external side effects — it produces structured decisions.

### Tools
Responsible for actually performing the operation requested by the AI. Tools are executed through Temporal Activities.

### Wake Policy
Responsible for deciding whether an incoming event is important enough to trigger immediate reasoning. The wake policy does not perform the reasoning itself.

---

## 6. One Workflow Per Order

The core model remains:

```
1 Order
   ↓
1 Temporal Workflow
```

Example: `order-12345` → `OrderWorkflow`

We must not accidentally create:
- one workflow for all orders
- one workflow per event
- one workflow per reasoning cycle

---

## 7. Events and Signals

The conceptual flow remains:

```
Event
  ↓
FastAPI
  ↓
Temporal Signal
  ↓
OrderWorkflow
  ↓
Wake Policy
  ↓
Reasoning if important
```

Every event should be recorded. Not every event should wake the LLM. Temporal itself does not decide event importance — the wake policy does.

---

## 8. No Continuous LLM Loop

The supervisor must not continuously run:

```
reason → reason → reason → reason → ...
```

Instead:

```
Wake
 ↓
Reason
 ↓
Optional action
 ↓
Update state
 ↓
Sleep
 ↓
Wake later
```

The three primary wake mechanisms are:
- workflow start
- important incoming event
- scheduled wake-up

---

## 9. LLM Reasoning Must Be Structured

The LLM should return structured output rather than arbitrary text used as control logic.

Conceptually:

```json
{
  "action": "escalate_shipment",
  "arguments": {
    "priority": "high"
  },
  "next_wake_in_minutes": 60
}
```

The workflow validates the decision before executing anything.

---

## 10. LLM Does Not Directly Execute Tools

The flow must remain:

```
LLM
 ↓
Structured decision
 ↓
Workflow validation
 ↓
Temporal Activity
 ↓
Tool
 ↓
Tool result
 ↓
Persist result
 ↓
Update memory/timeline
```

Never: `LLM → arbitrary external API`

---

## 11. One Tool Maximum Per Reasoning Cycle

For the POC:

```
One reasoning cycle
        ↓
At most one tool execution
```

If further action is needed, the supervisor can wake and reason again. This keeps the implementation understandable and prevents unnecessary agentic complexity.

---

## 12. Fixed Mock Tools

The initial tool set is:
- `get_order_status`
- `get_shipment_status`
- `escalate_shipment`
- `send_customer_update`

These are mocked/simulated for the POC. Do not introduce real commerce, messaging, shipping, or payment integrations unless explicitly requested later.

---

## 13. Memory vs Timeline

These must remain separate concepts.

### Timeline
Historical record: *What happened?* Stored in PostgreSQL.

### Memory
Compact evolving reasoning context: *What does the supervisor currently understand?* Memory can evolve after reasoning cycles. The complete historical timeline remains available in PostgreSQL.

The LLM should normally receive:
- compact memory
- + current order state
- + new/relevant events
- + pending instructions
- + relevant recent context

It should **not** receive the entire historical timeline on every reasoning cycle.

---

## 14. PostgreSQL Data Model

The core tables remain:
- `supervisors`
- `runs`
- `events`
- `timeline_entries`
- `actions`
- `tool_executions`
- `memory_snapshots`
- `final_outputs`

Their previously defined responsibilities should be preserved. Do not create duplicate competing sources of truth for the same information without discussion.

---

## 15. Supervisor vs Run

A **Supervisor** is the reusable configuration. A **Run** is one actual order being supervised.

```
Supervisor
   ↓
configuration/template

Run
   ↓
actual order execution
```

Active-run configuration should be treated as stable/versioned. Run-specific instructions are dynamic.

---

## 16. Controls

The controls retain their defined meanings.

- **Pause** — stops future supervisor activity until resumed.
- **Resume** — releases the pause and allows the supervisor to re-evaluate.
- **Interrupt** — stops/cancels the current reasoning/action cycle while keeping the workflow alive.
- **Terminate** — hard-stops the workflow.

These concepts must not be conflated.

---

## 17. Terminal State

When the order reaches a configured terminal state:

```
Terminal state
      ↓
Final-output generation
      ↓
Persist final output
      ↓
Mark run completed
      ↓
Workflow ends
```

Final output contains:
- summary
- key actions
- key learnings
- recommendations

---

## 18. Error Handling

Errors should be explicit. Do not hide failures with:

```python
try:
    ...
except:
    pass
```

Transient external failures should use appropriate Temporal Activity retry behavior where applicable. Permanent failures should be recorded and surfaced. A failed LLM/tool call must not silently appear as a successful action.

---

## 19. Determinism

Temporal workflow code must remain deterministic. Do not put arbitrary:
- database calls
- HTTP requests
- LLM calls
- random behavior
- filesystem operations
- external API calls

directly inside workflow execution. Use Activities where required.

---

## 20. No Overengineering

This is a POC/internship assignment, not a production SaaS platform.

Do not unnecessarily add:
- authentication systems
- multi-tenancy
- complex RBAC
- Kubernetes
- microservices
- event brokers
- vector databases
- advanced RAG
- multiple cooperating agents
- complex agent frameworks
- production-grade distributed infrastructure

unless the requirement or implementation genuinely demands it.

---

## 21. Code Quality

Code should be:
- readable
- typed where practical
- modular
- reasonably tested
- easy to explain during a walkthrough
- consistent with the architecture

Prefer simple explicit code over clever abstractions.

---

## 22. Antigravity Must Not Guess

This is particularly important.

If Antigravity encounters missing information, it should not invent a new architectural decision. It should:
1. inspect the provided architecture/problem statement;
2. use the existing decisions;
3. make reasonable low-level implementation choices where they don't affect architecture;
4. flag genuine ambiguity or architectural conflict.

---

## 23. Antigravity Must Work Incrementally

Antigravity should implement only the requested step. It should not decide:

> "While I'm here, I'll build the entire frontend/backend/workflow."

unless explicitly asked. Each prompt will define the current scope.

---

## 24. No Silent Scope Expansion

If the current task is:

> Implement database models.

then it should not also redesign the Temporal workflow, LLM architecture, UI, or API structure — unless those changes are necessary for the requested implementation and explicitly discussed.

---

## 25. Verification Before Moving Forward

After every implementation step, review:
- Does it run?
- Does it match the architecture?
- Does it satisfy the requirement?
- Are the boundaries correct?
- Are there unintended side effects?
- Are tests passing?

Only then proceed to the next step.

---

## 26. Architecture Change Protocol

If a major problem is discovered:

```
STOP
 ↓
Explain the problem
 ↓
Identify which requirement/decision is affected
 ↓
Discuss alternatives
 ↓
Choose explicitly
 ↓
Update architecture if necessary
 ↓
Update implementation plan
 ↓
Continue
```

No silent architecture changes.

---

## 27. Our Implementation Loop

This is the overall development process:

```
             ┌───────────────┐
             │   DISCUSS     │
             └───────┬───────┘
                     ↓
             ┌───────────────┐
             │    DESIGN     │
             └───────┬───────┘
                     ↓
             ┌───────────────┐
             │ WRITE PROMPT  │
             └───────┬───────┘
                     ↓
             ┌───────────────┐
             │ ANTIGRAVITY   │
             │    BUILDS     │
             └───────┬───────┘
                     ↓
             ┌───────────────┐
             │    REVIEW     │
             └───────┬───────┘
                     ↓
             ┌───────────────┐
             │    VERIFY     │
             └───────┬───────┘
                     ↓
               NEXT STEP
```

And if something goes wrong:

```
             Implementation
                  ↓
             Problem found
                  ↓
          ┌───────┴────────┐
          ↓                ↓
    Implementation     Major architectural
       problem              problem
          ↓                ↓
       Fix it          STOP + DISCUSS
                           ↓
                     Explicit decision
```

---

## The One Rule Above Everything Else

**Never let the implementation agent silently make an architectural decision on our behalf.**

Antigravity's job is to implement the design. Our job here is to understand, design, review, and make architectural decisions.
