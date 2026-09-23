"""Temporal worker: hosts OrderWorkflow on the Order Supervisor task queue.

Run from the backend directory (a Temporal server must be reachable):

    python -m app.temporal.worker
"""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import get_settings
from app.temporal.constants import TASK_QUEUE
from app.temporal.workflows import OrderWorkflow


def create_worker(client: Client) -> Worker:
    """Build the worker. Activities (LLM, tools, DB, final output) are added in later steps."""
    return Worker(client, task_queue=TASK_QUEUE, workflows=[OrderWorkflow])


async def run_worker() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await create_worker(client).run()


if __name__ == "__main__":
    asyncio.run(run_worker())
