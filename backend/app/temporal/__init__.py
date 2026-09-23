"""Temporal orchestration layer.

Contains OrderWorkflow (one long-running workflow per order, ID ``order-<order_id>``),
its Signal/data types, the deterministic wake policy, the Activity contracts and
implementations (LLM, tools, PostgreSQL, final output), the supervisor-config
translation, and the worker. The FastAPI client integration is added in Step 6.
"""
