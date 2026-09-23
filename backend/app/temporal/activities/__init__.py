"""Temporal Activities: all external / non-deterministic work (Step 4).

- persistence.PersistenceActivities: PostgreSQL writes
- reasoning.ReasoningActivities:     LLM calls (reasoning decision, final output)
- tools.ToolActivities:              the single ``execute_tool`` dispatcher

Activity classes take their dependencies in ``__init__`` so the worker (and
tests) inject them; nothing connects to a service at import time.
"""
