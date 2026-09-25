### Final Architecture & Workflow

# 1. Overview

The Order Supervisor is a long-running AI system that supervises an order from creation until completion.

The architecture uses one Temporal OrderWorkflow per order. Events are delivered through Signals, the workflow wakes only when needed, the LLM provides a structured decision, and Activities perform tools and persistence.

# 2. Executive Architecture Summary

Layer

Responsibility

Next.js

UI for creating runs, viewing state, events, and human controls

FastAPI

API boundary, validation, PostgreSQL access, and Temporal control

Temporal

Durable workflows, Signals, timers, and workflow state

Activities & Tools

LLM calls, database operations, and tool execution

Gemini

Structured reasoning and final output

PostgreSQL

Application data and complete run history

# 3. System Architecture

flowchart LR
    U[Users] --> FE[Next.js]
    FE --> API[FastAPI]
    API --> T[Temporal<br/>OrderWorkflow]

    T --> A[Activities & Tools]
    A --> L[Gemini]
    A --> DB[(PostgreSQL)]

    API --> DB

Core Flow

FastAPI starts one workflow for the order.

Events are sent to the workflow through Signals.

The workflow decides whether to wake immediately or wait for a timer.

A reasoning Activity asks Gemini for a structured JSON decision.

The workflow validates the decision and may execute one tool.

Memory and history are persisted.

The workflow continues until a terminal order status is reached.

# 4. Wake Triggers

Trigger

Purpose

Workflow start

Initial reasoning

Important event

React immediately to configured events

Scheduled wake-up

Periodic re-evaluation using a durable Temporal timer

Instruction added

Reconsider after human input

Resume

Restart reasoning after a pause

# 5. Activities & Tools

Activities keep non-deterministic work outside the workflow.

Tools:

get_order_status

get_shipment_status

escalate_shipment

send_customer_update

The LLM does not directly execute tools or modify the database. It only returns a structured decision that the workflow validates.

# 6. Data & State

PostgreSQL stores runs, events, timeline entries, actions, tool executions, memory snapshots, and final outputs.

Temporal stores the durable workflow state required for orchestration, such as pending events, memory, wake time, and control state.

Memory is compact reasoning context; the timeline is the complete human-readable history.

# 7. Human Controls

Pause — stops reasoning while keeping the workflow alive.

Resume — resumes reasoning.

Interrupt — stops the current reasoning cycle.

Terminate — hard-terminates the workflow.

# 8. Terminal State

A workflow ends when the order reaches a configured terminal status.

Terminal Status
      ↓
Final Output
      ↓
Complete Run
      ↓
Workflow Ends

The terminal state is determined by workflow/event logic, not by the LLM.

# 9. Key Design Principles

One durable Temporal workflow per order

Temporal handles orchestration; PostgreSQL handles persistence

Workflow code remains deterministic

LLM returns structured decisions, not direct side effects

Activities perform I/O and tool execution

Every event is recorded

Important events trigger immediate reasoning

# 10. Scope

This is a POC. Authentication, multi-tenancy, production scaling, advanced RAG, multi-agent orchestration, LangGraph, and production external integrations are outside the current scope.
