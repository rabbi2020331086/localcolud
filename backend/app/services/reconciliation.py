import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.infrastructure.database import database, from_document
from app.infrastructure.models import CloudNode, Instance, InstanceStatus
from app.services import node_jobs

worker_task: asyncio.Task[None] | None = None
log = logging.getLogger("localec2.reconciliation")


def provider_status(value: str) -> InstanceStatus:
    mapping = {
        "running": InstanceStatus.RUNNING,
        "stopped": InstanceStatus.STOPPED,
        "poweroff": InstanceStatus.STOPPED,
        "starting": InstanceStatus.STARTING,
        "suspended": InstanceStatus.HIBERNATED,
    }
    return mapping.get(value.lower(), InstanceStatus.UNKNOWN)


async def reconcile_once() -> None:
    documents = await database.instances.find({"deleted_at": None}).to_list(length=None)
    transitional = {
        InstanceStatus.PROVISIONING,
        InstanceStatus.STARTING,
        InstanceStatus.STOPPING,
        InstanceStatus.HIBERNATING,
        InstanceStatus.TERMINATING,
        # A failed launch has already removed its Vagrant workspace and
        # released capacity. It must not generate an INSPECT job every cycle.
        InstanceStatus.ERROR,
    }
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    for document in documents:
        instance = from_document(Instance, document)
        if not instance or instance.status in transitional:
            continue
        if not instance.node_id or instance.availability_zone == "legacy-unassigned":
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {
                    "$set": {
                        "status": InstanceStatus.UNKNOWN,
                        "last_error": "Legacy local instance is not assigned to an approved node.",
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            continue
        node = from_document(
            CloudNode,
            await database.cloud_nodes.find_one({"_id": str(instance.node_id)}),
        )
        if not node or not node.approved or not node.last_seen_at or node.last_seen_at < deadline:
            if instance.status == InstanceStatus.RUNNING:
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {
                        "$set": {
                            "status": InstanceStatus.UNKNOWN,
                            "last_error": "Assigned compute node is offline or unapproved.",
                            "updated_at": datetime.now(UTC),
                        }
                    },
                )
            continue
        try:
            result = await node_jobs.dispatch(
                instance,
                "INSPECT",
                {"name": instance.multipass_name},
                timeout=60,
            )
            status = provider_status(str(result.get("state", "unknown")))
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {
                    "$set": {
                        "status": status,
                        "ipv4_address": result.get("ipv4_address"),
                        "last_error": None,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
        except Exception as exc:
            if instance.status == InstanceStatus.RUNNING:
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {
                        "$set": {
                            "status": InstanceStatus.UNKNOWN,
                            "last_error": str(exc)[:1000],
                            "updated_at": datetime.now(UTC),
                        }
                    },
                )
    now = datetime.now(UTC)
    await database.one_time_secrets.update_many(
        {"expires_at": {"$lt": now}, "consumed_at": None},
        {"$set": {"encrypted_value": b"", "consumed_at": now}},
    )
    await node_jobs.reap_expired_leases()


async def reconciliation_loop() -> None:
    while True:
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Reconciliation worker retrying after control-plane error: %s", exc)
        await asyncio.sleep(settings.state_reconcile_seconds)
