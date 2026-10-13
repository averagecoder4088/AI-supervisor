# System Limitations & Future Architectural Roadmap

This document outlines the current technical boundaries of the **AI-supervisor** proof of concept, analyzes key architectural trade-offs, and details the roadmap for evolving this system into a full-scale **Autonomous Enterprise AI Employee**.

---

## 1. Current System Limitations

### A. Architectural & Execution Limitations
1. **Event-Driven Supervisor Scope vs. Interactive Desktop/Browser Automation:**
   - **Current State:** The supervisor is designed as a long-running, backend-driven business process orchestrator. It reacts to business events (via Temporal signals) and executes discrete operational tools against internal systems.
   - **Limitation:** It does not currently operate arbitrary desktop applications, interactive browser windows (DOM manipulation / visual computer use), or parse raw unstructured office documents (e.g., PDF invoices, Excel sheets).
2. **Single Tool Execution Per Reasoning Cycle:**
   - **Current State:** Each reasoning cycle strictly allows the LLM to choose at most one tool before recording progress, updating memory, and scheduling a sleep timer.
   - **Limitation:** Complex multi-step atomic subtasks (e.g., `Fetch data -> Verify against policy -> Update CRM -> Notify team`) must occur across multiple wake cycles rather than looping autonomously through chained steps in a single active session.
3. **Absence of an Explicit Verification & Evidence Phase:**
   - **Current State:** Order completion is triggered when an external signal moves the order into a terminal status (`delivered`, `cancelled`), followed by an end-of-run summary generation activity.
   - **Limitation:** The agent does not execute an autonomous post-action verification step (e.g., querying read-models to assert that an escalation ticket was successfully persisted, or gathering visual/cryptographic proof of work).
4. **Isolated Run Memory vs. Cross-Run Organizational Memory:**
   - **Current State:** State compaction and rolling memory are isolated to the lifecycle of an individual workflow run (`compact_memory`).
   - **Limitation:** The supervisor lacks a global, persistent organization-level knowledge store (e.g., enterprise vector database, company SOP repository, or vendor guidelines) to learn from historical runs across different orders.

### B. Infrastructure & Environment Limitations
1. **Mocked Operational Tables:**
   - **Current State:** Operational read/write actions target simulated PostgreSQL relational tables (`mock_orders`, `mock_shipments`, `mock_notifications`).
   - **Limitation:** Third-party integrations (FedEx/UPS APIs, Stripe, Shopify, Zendesk, Slack) are simulated rather than live authenticated REST/GraphQL connectors.
2. **Rule-Based Wake Policy:**
   - **Current State:** Event filtering uses a deterministic list of important event types per supervisor config to decide whether to wake the LLM.
   - **Limitation:** Incoming events are not evaluated semantically by a lightweight LLM classifier or dynamic policy engine to assess contextual urgency.
3. **No `continue_as_new` Roll-over:**
   - **Current State:** Temporal workflows retain event history within the same execution instance.
   - **Limitation:** While compact memory prevents context explosion in LLM prompts, very long-running workflows spanning hundreds of activations would eventually exceed Temporal's recommended history event thresholds without `continue_as_new`.

---

## 2. Quick-Win Architectural Enhancements

To bridge the gap between a business process supervisor and an enterprise AI employee, three foundational modules can be integrated directly into the existing Temporal topology:

### 1. Explicit Outcome Verification & Evidence Capture Activity
- **Design:** Introduce a dedicated `verify_outcome` Activity executed after tool invocation and prior to memory updates.
- **Mechanism:**
  - After taking an action (e.g., `escalate_shipment`), the workflow dispatches a verification activity that re-queries the underlying system state.
  - The verification activity returns a structured proof payload (`verified: bool`, `evidence: dict`, `delta: dict`).
  - If verification fails, the supervisor attempts automatic remediation or flags the run for human review before proceeding.

### 2. Pluggable Connectors (Browser, Document OCR, Desktop)
- **Design:** Extend [`backend/app/tools/registry.py`](../backend/app/tools/registry.py) to register external worker queues.
- **Mechanism:**
  - Add browser automation activities powered by Playwright to navigate web portals, download attachments, and extract table data.
  - Add document parsing activities (PDF parser / Vision LLM) to process invoices, purchase orders, and contracts.
  - Because Temporal separates activities from workflow orchestration, browser activities can execute on dedicated containerized worker nodes without affecting workflow determinism.

### 3. Cross-Run Organizational Memory (Company Knowledge Store)
- **Design:** Integrate a shared vector store / graph database (e.g., pgvector, Qdrant) alongside PostgreSQL.
- **Mechanism:**
  - Index company operating procedures, escalation hierarchies, and supplier contracts.
  - Query relevant company policies dynamically during the Reasoning activity based on current order context.
  - Store post-run learnings and feedback from completed runs to progressively improve future supervisor decisions.

---

## 3. Two-Week Development Roadmap

If given an additional two weeks of development time, the following milestones would be implemented:

### Week 1: Multi-Step Agent Execution & Automated Verification
- [ ] **Multi-Step Tool Chaining:** Refactor `OrderWorkflow` and `reasoning.py` to support multi-step tool execution loops within an active session until a sub-goal is achieved or an explicit sleep condition is met.
- [ ] **Verification & Evidence Engine:** Implement automated post-action state verification activities that generate verifiable audit trails and failure rollback triggers.
- [ ] **Browser Automation Connector:** Implement a Playwright-based Activity for navigating web dashboards, scraping external shipment tracking portals, and downloading invoices.

### Week 2: Enterprise Knowledge & Production Hardening
- [ ] **Cross-Run Knowledge Retrieval (RAG):** Implement `pgvector` storage for company SOPs, allowing supervisors to retrieve organizational policies dynamically.
- [ ] **Semantic Wake-Up Classifier:** Replace static event lists with a low-latency SLM (Small Language Model) classifier to evaluate incoming webhook payloads contextually.
- [ ] **Workflow History Optimization:** Implement Temporal `continue_as_new` to ensure indefinite execution support for multi-month business lifecycles without performance degradation.
- [ ] **Security & Multi-Tenancy:** Add role-based access control (RBAC), API key authentication, and audit log cryptographic signing.
