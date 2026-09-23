"""Tool Activity: the single, explicit boundary where a tool actually runs.

The workflow has already validated the tool name, that it is enabled, and its
inputs; this Activity re-checks the name as a defense and dispatches to the
registry handler.

- Business-level failure: the handler returns ``ToolResult(success=False)``;
  the Activity completes normally, so it is NOT retried.
- Unexpected crash: the handler raises; Temporal retries according to the
  tool's classification (read-only: retried; side-effecting: single attempt).
"""

from typing import Mapping, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.temporal.contracts import EXECUTE_TOOL, ToolRequest, ToolResult
from app.tools.registry import DEFAULT_TOOL_HANDLERS, ToolHandler, validate_tool_handlers


class ToolActivities:
    def __init__(self, tool_registry: Optional[Mapping[str, ToolHandler]] = None) -> None:
        handlers = dict(DEFAULT_TOOL_HANDLERS if tool_registry is None else tool_registry)
        validate_tool_handlers(handlers)
        self._handlers = handlers

    @activity.defn(name=EXECUTE_TOOL)
    async def execute_tool(self, request: ToolRequest) -> ToolResult:
        handler = self._handlers.get(request.tool_name)
        if handler is None:
            raise ApplicationError(
                f"Unknown or unregistered tool {request.tool_name!r}",
                type="UnknownTool",
                non_retryable=True,
            )
        return await handler(request)
