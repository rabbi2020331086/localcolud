import asyncio
from datetime import UTC, datetime

from app.core.config import settings
from app.infrastructure.database import database, from_document
from app.infrastructure.models import Instance, InstanceStatus, OperationStatus
from app.infrastructure.vm import ProviderUnavailable, provider

worker_task: asyncio.Task[None] | None = None


def provider_status(value: str) -> InstanceStatus:
    mapping = {
        "running": InstanceStatus.RUNNING,
        "stopped": InstanceStatus.STOPPED,
        "starting": InstanceStatus.STARTING,
        "suspended": InstanceStatus.HIBERNATED,
    }
    return mapping.get(value.lower(), InstanceStatus.UNKNOWN)


async def reconcile_once() -> None:
    try:
        remote = {item.name: item for item in await provider.list_instances()}
    except ProviderUnavailable:
        return
    documents = await database.instances.find({"deleted_at": None}).to_list(length=None)
    transitional = {
        InstanceStatus.PROVISIONING,
        InstanceStatus.STARTING,
        InstanceStatus.STOPPING,
        InstanceStatus.HIBERNATING,
        InstanceStatus.TERMINATING,
    }
    for document in documents:
        instance = from_document(Instance, document)
        if not instance:
            continue
        current = remote.get(instance.multipass_name)
        update: dict[str, object] = {"updated_at": datetime.now(UTC)}
        if current:
            if instance.status not in transitional:
                update["status"] = provider_status(current.state)
            update["ipv4_address"] = current.ipv4
        elif instance.status == InstanceStatus.TERMINATING:
            # The provider deletion may finish just before an API/worker restart.
            # Finalize the durable records when the VM is already gone.
            update["deleted_at"] = datetime.now(UTC)
            await database.volumes.update_many(
                {"attached_instance_id": str(instance.id)},
                {
                    "$set": {
                        "attached_instance_id": None,
                        "mount_path": None,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            await database.operations.update_many(
                {
                    "instance_id": str(instance.id),
                    "status": OperationStatus.RUNNING,
                },
                {
                    "$set": {
                        "status": OperationStatus.SUCCEEDED,
                        "progress": 100,
                        "message": "Completed after reconciliation",
                        "finished_at": datetime.now(UTC),
                    }
                },
            )
        elif instance.status not in transitional and instance.status != InstanceStatus.ERROR:
            update["status"] = InstanceStatus.UNKNOWN
            update["last_error"] = "The VM no longer exists in Multipass."
        await database.instances.update_one({"_id": str(instance.id)}, {"$set": update})
    now = datetime.now(UTC)
    await database.one_time_secrets.update_many(
        {"expires_at": {"$lt": now}, "consumed_at": None},
        {"$set": {"encrypted_value": b"", "consumed_at": now}},
    )


async def reconciliation_loop() -> None:
    while True:
        await reconcile_once()
        await asyncio.sleep(settings.state_reconcile_seconds)
