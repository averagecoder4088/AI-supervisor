"""Translation boundary: PostgreSQL supervisor/run rows -> OrderWorkflowInput.

The database JSON shapes stop here; workflow code only sees
``OrderWorkflowInput``. Everything is validated rather than passed through:

- ``wake_policy`` must be ``{"important_event_types": [<event type>, ...]}``
- ``order_status_by_event`` keys must be known event types, values non-empty strings
- ``enabled_tools`` must be tools from the fixed registry
- event types must belong to the frozen vocabulary (``EVENT_TYPES``)

Step 6 (FastAPI) is the caller when it starts a workflow for a run.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List

from app.temporal.constants import EVENT_TYPES
from app.temporal.types import OrderWorkflowInput, RunInstruction
from app.tools.registry import TOOL_SPECS

if TYPE_CHECKING:  # pragma: no cover
    from app.db.models import Run, Supervisor


class SupervisorConfigError(ValueError):
    """Supervisor/run configuration cannot be translated into a valid workflow input."""


def _string_list(value: Any, field_name: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise SupervisorConfigError(f"{field_name} must be a list of non-empty strings")
    return list(value)


def _known_event_types(values: List[str], field_name: str) -> List[str]:
    unknown = sorted(set(values) - EVENT_TYPES)
    if unknown:
        raise SupervisorConfigError(f"{field_name} contains unknown event types {unknown}")
    return values


def important_event_types_from_wake_policy(wake_policy: Any) -> List[str]:
    if not isinstance(wake_policy, dict) or set(wake_policy) != {"important_event_types"}:
        raise SupervisorConfigError('wake_policy must be {"important_event_types": [...]}')
    events = _string_list(wake_policy["important_event_types"], "wake_policy.important_event_types")
    return _known_event_types(events, "wake_policy.important_event_types")


def order_status_mapping(mapping: Any) -> Dict[str, str]:
    if not isinstance(mapping, dict):
        raise SupervisorConfigError("order_status_by_event must be an object")
    _known_event_types(list(mapping), "order_status_by_event")
    for event_type, status in mapping.items():
        if not isinstance(status, str) or not status.strip():
            raise SupervisorConfigError(f"order_status_by_event[{event_type!r}] must be a non-empty string")
    return dict(mapping)


def enabled_tool_names(tools: Any) -> List[str]:
    names = _string_list(tools, "enabled_tools")
    unknown = sorted(set(names) - set(TOOL_SPECS))
    if unknown:
        raise SupervisorConfigError(f"enabled_tools contains unknown tools {unknown}")
    return names


def run_instructions(stored: Any) -> List[RunInstruction]:
    if not isinstance(stored, list):
        raise SupervisorConfigError("run_instructions must be a list")
    result = []
    for item in stored:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise SupervisorConfigError("each run instruction must be {'text': str, 'added_at': iso8601|null}")
        added_at = item.get("added_at")
        result.append(
            RunInstruction(text=item["text"], added_at=datetime.fromisoformat(added_at) if added_at else None)
        )
    return result


def build_workflow_input(supervisor: "Supervisor", run: "Run") -> OrderWorkflowInput:
    if run.id is None:
        raise SupervisorConfigError("run must be persisted (have an id) before its workflow starts")
    minimum, default, maximum = (
        supervisor.min_wake_interval,
        supervisor.default_wake_interval,
        supervisor.max_wake_interval,
    )
    if not (0 < minimum <= default <= maximum):
        raise SupervisorConfigError("wake intervals must satisfy 0 < min <= default <= max")

    return OrderWorkflowInput(
        order_id=run.order_id,
        order_status=run.order_status,
        important_event_types=important_event_types_from_wake_policy(supervisor.wake_policy),
        terminal_order_statuses=_string_list(supervisor.terminal_order_statuses, "terminal_order_statuses"),
        order_status_by_event=order_status_mapping(supervisor.order_status_by_event),
        default_wake_interval_minutes=default,
        min_wake_interval_minutes=minimum,
        max_wake_interval_minutes=maximum,
        run_id=str(run.id),
        supervisor_instructions=supervisor.instructions,
        enabled_tools=enabled_tool_names(supervisor.enabled_tools),
        supervisor_version=supervisor.version,
        run_instructions=run_instructions(run.run_instructions),
    )
