"""LLM layer: provider-agnostic client interface, structured-output schemas, prompts.

The LLM only reasons and returns structured decisions. It never writes the
database, executes tools or touches workflow state; the Activities that call
it return validated data to the workflow, which decides what happens.
"""
