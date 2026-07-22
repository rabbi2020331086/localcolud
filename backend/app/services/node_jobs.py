import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import settings
from app.infrastructure.database import database, from_document, to_document
from app.infrastructure.models import CloudNode, Instance, NodeJob, NodeJobStatus
from app.infrastructure.vm import ProviderUnavailable


async def create_job(
    instance: Instance,
    job_type: str,
    payload: dict[str, object],
    operation_id: UUID | None = None,
) -> NodeJob:
    if not instance.node_id:
        raise ProviderUnavailable("Instance is not assigned to a compute node")
    if operation_id:
        existing = from_document(
            NodeJob,
            await database.node_jobs.find_one({"operation_id": str(operation_id)}),
        )
        if existing:
            return existing
    job = NodeJob(
        node_id=instance.node_id,
        instance_id=instance.id,
        operation_id=operation_id,
        job_type=job_type,
        payload=payload,
    )
    await database.node_jobs.insert_one(to_document(job))
    return job


async def create_node_job(
    node: CloudNode, job_type: str, payload: dict[str, object]
) -> NodeJob:
    """Create a platform job that targets a host rather than a guest instance."""
    job = NodeJob(node_id=node.id, job_type=job_type, payload=payload)
    await database.node_jobs.insert_one(to_document(job))
    return job


async def dispatch_node(
    node: CloudNode,
    job_type: str,
    payload: dict[str, object],
    timeout: float | None = None,
) -> dict[str, object]:
    completed = await wait_for_job(await create_node_job(node, job_type, payload), timeout)
    return completed.result_payload


async def wait_for_job(job: NodeJob, timeout: float | None = None) -> NodeJob:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (timeout if timeout is not None else settings.node_job_timeout_seconds)
    while loop.time() < deadline:
        # Do not depend solely on the reconciliation worker to recover a node
        # job whose agent disappeared while an operation is waiting on it.
        await reap_expired_leases()
        current = from_document(
            NodeJob,
            await database.node_jobs.find_one({"_id": str(job.id)}),
        )
        if not current:
            raise ProviderUnavailable("Node job disappeared before completion")
        if current.status == NodeJobStatus.SUCCEEDED:
            return current
        if current.status == NodeJobStatus.FAILED:
            raise ProviderUnavailable(current.error_message or "Compute node job failed")
        if (
            current.status == NodeJobStatus.LEASED
            and current.lease_expires_at
            and current.lease_expires_at > datetime.now(UTC)
        ):
            remaining = (current.lease_expires_at - datetime.now(UTC)).total_seconds()
            deadline = max(deadline, loop.time() + remaining + 5)
        await asyncio.sleep(settings.node_job_poll_seconds)
    await database.node_jobs.update_one(
        {"_id": str(job.id), "status": NodeJobStatus.QUEUED},
        {
            "$set": {
                "status": NodeJobStatus.FAILED,
                "error_message": "Control plane timed out before the node claimed this job",
                "finished_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        },
    )
    raise ProviderUnavailable(
        "Compute node did not finish the job before the control-plane timeout"
    )


async def dispatch(
    instance: Instance,
    job_type: str,
    payload: dict[str, object],
    operation_id: UUID | None = None,
    timeout: float | None = None,
) -> dict[str, object]:
    completed = await wait_for_job(
        await create_job(instance, job_type, payload, operation_id), timeout
    )
    return completed.result_payload


async def dispatch_secret(
    instance: Instance,
    job_type: str,
    payload: dict[str, object],
    timeout: float | None = None,
) -> dict[str, object]:
    """Dispatch a secret-bearing job and erase its payload as soon as it finishes."""
    job = await create_job(instance, job_type, payload)
    try:
        completed = await wait_for_job(job, timeout)
        return completed.result_payload
    finally:
        await database.node_jobs.update_one(
            {"_id": str(job.id)},
            {"$set": {"payload": {"redacted": True}, "updated_at": datetime.now(UTC)}},
        )


async def remote_exec(instance: Instance, command: str, timeout: float = 60) -> str:
    result = await dispatch(
        instance,
        "EXEC",
        {"name": instance.multipass_name, "command": command, "timeout": timeout},
        timeout=timeout + 30,
    )
    return str(result.get("stdout", ""))


async def reap_expired_leases() -> int:
    now = datetime.now(UTC)
    result = await database.node_jobs.update_many(
        {
            "status": NodeJobStatus.LEASED,
            "lease_expires_at": {"$lt": now},
            "attempts": {"$lt": 3},
        },
        {
            "$set": {
                "status": NodeJobStatus.QUEUED,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )
    await database.node_jobs.update_many(
        {
            "status": NodeJobStatus.LEASED,
            "lease_expires_at": {"$lt": now - timedelta(seconds=5)},
            "attempts": {"$gte": 3},
        },
        {
            "$set": {
                "status": NodeJobStatus.FAILED,
                "error_message": "Node job lease expired after three attempts",
                "finished_at": now,
                "updated_at": now,
            }
        },
    )
    return result.modified_count
