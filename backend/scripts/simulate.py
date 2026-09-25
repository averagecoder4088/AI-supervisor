"""Live external-world simulator for the demo. Manual tooling; NOT part of pytest.

Each command changes the mock operational tables (mock_orders, mock_shipments, mock_customer_messages)
and THEN sends the matching event to the running API, so the tools read exactly the state the event
describes. It drives the same ``ExternalWorld`` that the S1 to S5 scenario tests use, against your live
stack (PostgreSQL + FastAPI), so there is no hand-written SQL in a demo.

Typical order lifecycle (start the run in the UI between `place` and `created`):

    python backend/scripts/simulate.py place   DEMO-2001            # mock order row (status created); no event
    ...                                                             # UI: Start run for DEMO-2001
    python backend/scripts/simulate.py created DEMO-2001            # event order_created
    python backend/scripts/simulate.py pay     DEMO-2001            # order -> payment_confirmed, event payment_confirmed
    python backend/scripts/simulate.py ship    DEMO-2001            # shipment row + order -> shipped, event shipment_created
    python backend/scripts/simulate.py delay   DEMO-2001            # shipment + order -> delayed, event shipment_delayed
    python backend/scripts/simulate.py message DEMO-2001 "Where is my order?"   # inbound message row + event
    python backend/scripts/simulate.py deliver DEMO-2001            # shipment + order -> delivered, event delivered

Alternative endings: `fail-payment` (created -> payment_failed) and `cancel` (before a shipment exists).
Other commands: `show ORDER` prints the run and the mock rows; `reset ORDER --yes` deletes that order's run
and mock rows (it does NOT stop a live workflow; Terminate it in the UI first).

The transitions are the simulator's: `pay` needs the order in `created`, `ship` in `payment_confirmed`,
`delay` in `shipped`, `deliver` in `shipped` or `delayed`. A step out of order is refused with the reason.

Options: --api URL (default http://127.0.0.1:8000). Reads DATABASE_URL from backend/.env like the backend.
Exit codes: 0 done, 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.mock_models import MockCustomerMessage, MockOrder, MockShipment  # noqa: E402
from app.db.models import Run  # noqa: E402
from app.simulation.world import ExternalWorld, ScenarioError  # noqa: E402

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_DELAY_REASON = "Carrier capacity shortage"
DEFAULT_PAYMENT_FAILURE = "card_declined"
DEFAULT_CANCEL_REASON = "Customer request"

# command -> (help text, needs a run). `place`, `show` and `reset` work without a run.
COMMANDS = {
    "place": "create the mock order row (status created); sends no event",
    "created": "send order_created",
    "pay": "confirm payment: order -> payment_confirmed, then send payment_confirmed",
    "ship": "create the shipment: order -> shipped, then send shipment_created",
    "delay": "delay the shipment: shipment and order -> delayed, then send shipment_delayed",
    "message": "the customer writes in: one inbound message row, then send customer_message_received",
    "deliver": "deliver: shipment and order -> delivered, then send delivered (ends the run)",
    "fail-payment": "payment fails: order created -> payment_failed, then send payment_failed",
    "cancel": "cancel the order before a shipment exists, then send order_cancelled",
    "show": "print the run and the mock rows for the order",
    "reset": "delete the order's run and mock rows (needs --yes)",
}
NEEDS_RUN = {"created", "pay", "ship", "delay", "message", "deliver", "fail-payment", "cancel"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Change the mock world and send the matching event to a live run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="commands:\n" + "\n".join(f"  {name:<13} {text}" for name, text in COMMANDS.items()),
    )
    parser.add_argument("command", choices=sorted(COMMANDS), metavar="command")
    parser.add_argument("order_id", metavar="ORDER_ID")
    parser.add_argument("text", nargs="?", help="message text (message command only)")
    parser.add_argument("--reason", help="reason for delay, fail-payment or cancel")
    parser.add_argument("--api", default=DEFAULT_API, help=f"FastAPI base URL (default {DEFAULT_API})")
    parser.add_argument("--yes", action="store_true", help="confirm reset")
    return parser


async def find_run(session_factory: async_sessionmaker, order_id: str) -> Optional[Run]:
    async with session_factory() as session:
        return (await session.execute(select(Run).where(Run.order_id == order_id))).scalar_one_or_none()


async def show(session_factory: async_sessionmaker, order_id: str) -> None:
    run = await find_run(session_factory, order_id)
    print(f"order {order_id}")
    if run is None:
        print("  run:       none (start one in the UI)")
    else:
        print(f"  run:       {run.id}  status={run.status}  order_status={run.order_status}")
    async with session_factory() as session:
        order = (await session.execute(select(MockOrder).where(MockOrder.order_id == order_id))).scalar_one_or_none()
        shipment = (
            await session.execute(select(MockShipment).where(MockShipment.order_id == order_id))
        ).scalar_one_or_none()
        messages = (
            await session.execute(
                select(MockCustomerMessage)
                .where(MockCustomerMessage.order_id == order_id)
                .order_by(MockCustomerMessage.created_at)
            )
        ).scalars().all()
    if order is None:
        print("  mock order: none (run `place` first)")
        return
    print(f"  mock order:    status={order.status}  customer={order.customer_id}")
    if shipment is None:
        print("  mock shipment: none")
    else:
        print(
            f"  mock shipment: {shipment.shipment_id}  status={shipment.status}  tracking={shipment.tracking_number}"
            f"  escalated={shipment.escalated}  delay_reason={shipment.delay_reason}"
        )
    print(f"  messages:      {len(messages)}")
    for message in messages:
        print(f"    [{message.direction}] {message.message}")


async def reset(session_factory: async_sessionmaker, order_id: str) -> None:
    async with session_factory() as session:
        async with session.begin():
            runs = (await session.execute(delete(Run).where(Run.order_id == order_id))).rowcount
            orders = (await session.execute(delete(MockOrder).where(MockOrder.order_id == order_id))).rowcount
    print(f"reset {order_id}: deleted {runs} run(s) and {orders} mock order(s) (shipments and messages cascade)")
    print("a live Temporal workflow for this order is NOT stopped; Terminate it in the UI if one is still running")


async def run_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.command == "show":
            await show(session_factory, args.order_id)
            return 0
        if args.command == "reset":
            if not args.yes:
                print("reset deletes this order's run and mock rows; repeat with --yes to confirm", file=sys.stderr)
                return 2
            await reset(session_factory, args.order_id)
            return 0

        async with httpx.AsyncClient(base_url=args.api, timeout=30.0) as api:
            world = ExternalWorld(session_factory, api)
            if args.command == "place":
                await world.place_order(args.order_id)
                print(f"placed {args.order_id}: mock order created with status 'created' (no event sent)")
                return 0

            run = await find_run(session_factory, args.order_id)
            if run is None:
                print(f"no run exists for {args.order_id}: start one in the UI first", file=sys.stderr)
                return 1
            run_id = str(run.id)
            order_id = args.order_id
            if args.command == "created":
                await world.order_created(run_id, order_id)
            elif args.command == "pay":
                await world.confirm_payment(run_id, order_id)
            elif args.command == "ship":
                await world.create_shipment(run_id, order_id)
            elif args.command == "delay":
                await world.delay_shipment(run_id, order_id, args.reason or DEFAULT_DELAY_REASON)
            elif args.command == "message":
                if not args.text:
                    print('message needs text, for example: message DEMO-2001 "Where is my order?"', file=sys.stderr)
                    return 2
                await world.customer_message(run_id, order_id, args.text)
            elif args.command == "deliver":
                await world.deliver(run_id, order_id)
            elif args.command == "fail-payment":
                await world.fail_payment(run_id, order_id, args.reason or DEFAULT_PAYMENT_FAILURE)
            elif args.command == "cancel":
                await world.cancel_order(run_id, order_id, args.reason or DEFAULT_CANCEL_REASON)
            print(f"done: {args.command} for {order_id} (mock world updated, event accepted by the API)")
            return 0
    except ScenarioError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"could not reach the API at {args.api}: {error}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run_command(args))


if __name__ == "__main__":
    sys.exit(main())
