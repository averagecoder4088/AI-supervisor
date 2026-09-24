"""Step 8: simulation of the external operational world (S8-D1).

The simulator plays the order/shipping systems outside the Order Supervisor. For
each simulated event it (1) applies the deterministic change to the MOCK
operational tables and (2) submits the event through the existing FastAPI event
endpoint, which sends the Temporal Signal. It never writes supervisor/history
tables (runs, events, timeline_entries, actions, tool_executions,
memory_snapshots, final_outputs): the API, workflow and Activities produce those.
"""
