"""Supervisor endpoints (PostgreSQL only; no Temporal involved)."""

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api import supervisors as supervisors_api
from app.db.models import Run, Supervisor
from app.main import create_app
from app.temporal import supervisor_config
from app.temporal.supervisor_config import SupervisorConfigError, build_workflow_input
from tests.api_support import PREFIX, pooled_session_factory, supervisor_body  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api(pooled_session_factory):  # noqa: F811
    app = create_app(session_factory=pooled_session_factory, connect_temporal=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_create_supervisor_returns_201_version_1(api):
    response = await api.post("/api/supervisors", json=supervisor_body())
    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["name"] == f"{PREFIX}Shipment Supervisor"
    assert body["wake_policy"] == {"important_event_types": ["shipment_delayed", "customer_message_received"]}
    assert body["order_status_by_event"] == {"delivered": "delivered", "order_cancelled": "cancelled"}
    assert (body["default_wake_interval_minutes"], body["min_wake_interval_minutes"], body["max_wake_interval_minutes"]) == (30, 5, 120)
    uuid.UUID(body["id"])


async def test_order_status_by_event_is_optional_and_defaults_to_empty(api):
    body = supervisor_body()
    del body["order_status_by_event"]
    response = await api.post("/api/supervisors", json=body)
    assert response.status_code == 201
    assert response.json()["order_status_by_event"] == {}


async def test_same_name_creates_version_n_plus_1_and_keeps_previous_unchanged(api):
    v1 = (await api.post("/api/supervisors", json=supervisor_body())).json()
    v2 = (await api.post("/api/supervisors", json=supervisor_body(instructions="Escalate every delay."))).json()
    v3 = (await api.post("/api/supervisors", json=supervisor_body(instructions="Third."))).json()
    assert (v1["version"], v2["version"], v3["version"]) == (1, 2, 3)
    assert len({v1["id"], v2["id"], v3["id"]}) == 3  # new immutable rows, never an in-place update

    again = (await api.get(f"/api/supervisors/{v1['id']}")).json()
    assert again == v1


async def test_get_supervisor(api):
    created = (await api.post("/api/supervisors", json=supervisor_body())).json()
    response = await api.get(f"/api/supervisors/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


async def test_unknown_supervisor_is_404(api):
    response = await api.get(f"/api/supervisors/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"error": "Supervisor not found", "code": "SUPERVISOR_NOT_FOUND"}


@pytest.mark.parametrize(
    "changes",
    [
        {"wake_policy": {"important_event_types": ["package_lost"]}},  # unknown event type
        {"wake_policy": ["shipment_delayed"]},  # wrong shape
        {"enabled_tools": ["refund_customer"]},  # invented tool
        {"min_wake_interval_minutes": 60},  # min > default
        {"order_status_by_event": {"package_lost": "lost"}},  # unknown event cannot map to a status
        {"order_status_by_event": {"delivered": ""}},
    ],
)
async def test_invalid_supervisor_configuration_is_400(api, pooled_session_factory, changes):  # noqa: F811
    response = await api.post("/api/supervisors", json=supervisor_body(**changes))
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    async with pooled_session_factory() as session:
        assert (await session.execute(select(Supervisor).where(Supervisor.name.like(f"{PREFIX}%")))).first() is None


@pytest.mark.parametrize(
    "body",
    [
        {},  # missing fields
        supervisor_body(unexpected="x"),  # unknown field
        supervisor_body(default_wake_interval_minutes="soon"),
        supervisor_body(name=""),
    ],
)
async def test_request_schema_errors_are_400_not_422(api, body):
    response = await api.post("/api/supervisors", json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_malformed_supervisor_id_is_400(api):
    response = await api.get("/api/supervisors/not-a-uuid")
    assert (response.status_code, response.json()["code"]) == (400, "VALIDATION_ERROR")


async def test_api_and_workflow_translation_share_one_validator(api):
    # The API module uses the very same function object as the Step 4 translation.
    assert supervisors_api.validate_supervisor_config is supervisor_config.validate_supervisor_config

    response = await api.post("/api/supervisors", json=supervisor_body(min_wake_interval_minutes=60))
    stored = Supervisor(
        wake_policy={"important_event_types": []},
        enabled_tools=[],
        terminal_order_statuses=[],
        order_status_by_event={},
        default_wake_interval=30,
        min_wake_interval=60,
        max_wake_interval=120,
        instructions="x",
        version=1,
    )
    with pytest.raises(SupervisorConfigError) as exc:
        build_workflow_input(stored, Run(id=uuid.uuid4(), order_id="x", run_instructions=[]))
    assert response.json()["error"] == str(exc.value)  # same rule, same message
