"""Translation boundary: PostgreSQL supervisor/run rows -> OrderWorkflowInput.

The database JSON shapes stop here; workflow code only sees
``OrderWorkflowInput``. Everything is validated rather than passed through:

- ``wake_policy`` must be ``{"important_event_types": [<event type>, ...]}``
- ``order_status_by_event`` keys must be known event types, values non-empty strings
- ``enabled_tools`` must be tools from the fixed registry
- event types must belong to the frozen vocabulary (``EVENT_TYPES``)

``validate_supervisor_config`` is the single validator: the API uses it when a
supervisor is created (Step 6) and ``build_workflow_input`` uses it when a run's
workflow is started, so both apply exactly the same rules.
"""

from dataclasses import dataclass
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


@dataclass
class ValidatedSupervisorConfig:
    important_event_types: List[str]
    enabled_tools: List[str]
    terminal_order_statuses: List[str]
    order_status_by_event: Dict[str, str]
    default_wake_interval: int
    min_wake_interval: int
    max_wake_interval: int


def validate_supervisor_config(
    *,
    wake_policy: Any,
    enabled_tools: Any,
    terminal_order_statuses: Any,
    order_status_by_event: Any,
    default_wake_interval: Any,
    min_wake_interval: Any,
    max_wake_interval: Any,
) -> ValidatedSupervisorConfig:
    """Validate a supervisor configuration; raise SupervisorConfigError if invalid."""
    intervals = (min_wake_interval, default_wake_interval, max_wake_interval)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in intervals):
        raise SupervisorConfigError("wake intervals must be integers")
    if not (0 < min_wake_interval <= default_wake_interval <= max_wake_interval):
        raise SupervisorConfigError("wake intervals must satisfy 0 < min <= default <= max")
    return ValidatedSupervisorConfig(
        important_event_types=important_event_types_from_wake_policy(wake_policy),
        enabled_tools=enabled_tool_names(enabled_tools),
        terminal_order_statuses=_string_list(terminal_order_statuses, "terminal_order_statuses"),
        order_status_by_event=order_status_mapping(order_status_by_event),
        default_wake_interval=default_wake_interval,
        min_wake_interval=min_wake_interval,
        max_wake_interval=max_wake_interval,
    )


def build_workflow_input(supervisor: "Supervisor", run: "Run") -> OrderWorkflowInput:
    if run.id is None:
        raise SupervisorConfigError("run must be persisted (have an id) before its workflow starts")
    config = validate_supervisor_config(
        wake_policy=supervisor.wake_policy,
        enabled_tools=supervisor.enabled_tools,
        terminal_order_statuses=supervisor.terminal_order_statuses,
        order_status_by_event=supervisor.order_status_by_event,
        default_wake_interval=supervisor.default_wake_interval,
        min_wake_interval=supervisor.min_wake_interval,
        max_wake_interval=supervisor.max_wake_interval,
    )
    return OrderWorkflowInput(
        order_id=run.order_id,
        order_status=run.order_status,
        important_event_types=config.important_event_types,
        terminal_order_statuses=config.terminal_order_statuses,
        order_status_by_event=config.order_status_by_event,
        default_wake_interval_minutes=config.default_wake_interval,
        min_wake_interval_minutes=config.min_wake_interval,
        max_wake_interval_minutes=config.max_wake_interval,
        run_id=str(run.id),
        supervisor_instructions=supervisor.instructions,
        enabled_tools=config.enabled_tools,
        supervisor_version=supervisor.version,
        run_instructions=run_instructions(run.run_instructions),
    )
