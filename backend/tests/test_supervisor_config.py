"""Translation boundary: supervisor/run rows -> OrderWorkflowInput (no DB needed)."""

import uuid
from datetime import datetime, timezone

import pytest

from app.db.models import Run, Supervisor
from app.temporal.supervisor_config import SupervisorConfigError, build_workflow_input


def _supervisor(**overrides) -> Supervisor:
    values = dict(
        name="default",
        instructions="Keep the customer informed.",
        wake_policy={"important_event_types": ["shipment_delayed", "refund_requested"]},
        enabled_tools=["get_shipment_status", "escalate_shipment"],
        default_wake_interval=60,
        min_wake_interval=5,
        max_wake_interval=1440,
        terminal_order_statuses=["delivered", "cancelled"],
        order_status_by_event={"delivered": "delivered", "order_cancelled": "cancelled"},
        version=3,
    )
    values.update(overrides)
    return Supervisor(**values)


def _run(**overrides) -> Run:
    values = dict(
        id=uuid.uuid4(),
        order_id="12345",
        order_status="created",
        run_instructions=[{"text": "Prioritize speed over cost.", "added_at": "2026-09-24T10:00:00+00:00"}],
    )
    values.update(overrides)
    return Run(**values)


def test_translates_supervisor_and_run_into_workflow_input():
    run = _run()
    wf = build_workflow_input(_supervisor(), run)
    assert wf.order_id == "12345"
    assert wf.run_id == str(run.id)
    assert wf.order_status == "created"
    assert wf.important_event_types == ["shipment_delayed", "refund_requested"]
    assert wf.order_status_by_event == {"delivered": "delivered", "order_cancelled": "cancelled"}
    assert wf.terminal_order_statuses == ["delivered", "cancelled"]
    assert (wf.default_wake_interval_minutes, wf.min_wake_interval_minutes, wf.max_wake_interval_minutes) == (60, 5, 1440)
    assert wf.enabled_tools == ["get_shipment_status", "escalate_shipment"]
    assert wf.supervisor_instructions == "Keep the customer informed."
    assert wf.supervisor_version == 3
    assert wf.run_instructions[0].text == "Prioritize speed over cost."
    assert wf.run_instructions[0].added_at == datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


def test_empty_status_mapping_is_allowed_and_passed_through():
    assert build_workflow_input(_supervisor(order_status_by_event={}), _run()).order_status_by_event == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"wake_policy": ["shipment_delayed"]},  # list, not the agreed object
        {"wake_policy": {}},  # missing key
        {"wake_policy": {"important_event_types": ["shipment_delayed"], "extra": 1}},
        {"wake_policy": {"important_event_types": ["package_lost"]}},  # not in frozen vocabulary
        {"order_status_by_event": {"package_lost": "lost"}},  # unknown event cannot become a status
        {"order_status_by_event": {"delivered": ""}},
        {"order_status_by_event": ["delivered"]},
        {"enabled_tools": ["refund_customer"]},  # invented tool
        {"terminal_order_statuses": "delivered"},
        {"min_wake_interval": 90},  # min > default
        {"min_wake_interval": 0},
    ],
)
def test_invalid_configuration_is_rejected(overrides):
    with pytest.raises(SupervisorConfigError):
        build_workflow_input(_supervisor(**overrides), _run())


def test_invalid_run_instructions_and_unsaved_run_are_rejected():
    with pytest.raises(SupervisorConfigError):
        build_workflow_input(_supervisor(), _run(run_instructions=["just text"]))
    with pytest.raises(SupervisorConfigError):
        build_workflow_input(_supervisor(), _run(id=None))
