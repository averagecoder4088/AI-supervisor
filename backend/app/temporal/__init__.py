"""Temporal orchestration layer.

Contains OrderWorkflow (one long-running workflow per order, ID ``order-<order_id>``),
its Signal/data types, the deterministic wake policy, and the worker.
Activities (LLM, tools, PostgreSQL, final output) and the FastAPI client
integration are added in subsequent implementation steps.
"""
