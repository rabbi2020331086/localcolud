from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.dependencies import CurrentUser
from app.core.config import settings
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.metrics import CPU_PERCENT_COMMAND
from app.infrastructure.models import (
    Ami,
    Instance,
    InstanceStatus,
    Operation,
    OperationStatus,
    OperationType,
)
from app.infrastructure.vm import provider, safe_name
from app.schemas import AcceptedOut, InstanceCreate, InstanceOut

router = APIRouter(prefix="/instances", tags=["Instances"])


async def owned(database: Database, user_id: UUID, instance_id: UUID) -> Instance:
    item = from_document(
        Instance,
        await database.instances.find_one(
            {"_id": str(instance_id), "user_id": str(user_id), "deleted_at": None}
        ),
    )
    if not item:
        raise HTTPException(404, "Instance not found")
    return item


async def enqueue(
    database: Database, user_id: UUID, instance: Instance, kind: OperationType
) -> AcceptedOut:
    active = await database.operations.find_one(
        {
            "instance_id": str(instance.id),
            "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
        }
    )
    if active:
        raise HTTPException(409, "Another operation is already running for this instance")
    operation = Operation(user_id=user_id, instance_id=instance.id, operation_type=kind)
    await database.operations.insert_one(to_document(operation))
    return AcceptedOut(operation_id=operation.id, instance_id=instance.id, status=operation.status)


@router.get("", response_model=list[InstanceOut])
async def list_instances(user: CurrentUser, database: Database) -> list[Instance]:
    documents = (
        await database.instances.find({"user_id": str(user.id), "deleted_at": None})
        .sort("created_at", -1)
        .to_list(length=None)
    )
    return from_documents(Instance, documents)


@router.post("", response_model=AcceptedOut, status_code=202)
async def create(data: InstanceCreate, user: CurrentUser, database: Database) -> AcceptedOut:
    count = await database.instances.count_documents({"user_id": str(user.id), "deleted_at": None})
    if count >= settings.max_total_instances:
        raise HTTPException(409, "Instance limit reached")
    ami = None
    if data.ami_id:
        ami = from_document(
            Ami,
            await database.amis.find_one({"_id": str(data.ami_id), "user_id": str(user.id)}),
        )
        if not ami:
            raise HTTPException(404, "AMI not found")
        if data.disk_gb < ami.disk_gb:
            raise HTTPException(422, f"Disk must be at least {ami.disk_gb} GB for this AMI")
    if data.key_pair_id and not await database.key_pairs.find_one(
        {"_id": str(data.key_pair_id), "user_id": str(user.id)}
    ):
        raise HTTPException(404, "Key pair not found")
    security_count = await database.security_groups.count_documents(
        {"_id": {"$in": [str(value) for value in data.security_group_ids]}, "user_id": str(user.id)}
    )
    if security_count != len(set(data.security_group_ids)):
        raise HTTPException(404, "One or more security groups were not found")
    item = Instance(
        user_id=user.id,
        multipass_name=safe_name(str(user.id), data.name),
        display_name=data.name,
        description=data.description,
        image=ami.image if ami else data.image,
        ami_id=ami.id if ami else None,
        cpu_count=data.cpu_count,
        memory_mb=data.memory_mb,
        disk_gb=data.disk_gb,
        instance_type=data.instance_type,
        key_pair_id=data.key_pair_id,
        security_group_ids=data.security_group_ids,
    )
    operation = Operation(
        user_id=user.id,
        instance_id=item.id,
        operation_type=OperationType.LAUNCH,
        request_payload=data.model_dump(),
    )
    await database.instances.insert_one(to_document(item))
    try:
        await database.operations.insert_one(to_document(operation))
    except Exception:
        await database.instances.delete_one({"_id": str(item.id)})
        raise
    return AcceptedOut(operation_id=operation.id, instance_id=item.id, status=operation.status)


@router.get("/{instance_id}", response_model=InstanceOut)
async def get(instance_id: UUID, user: CurrentUser, database: Database) -> Instance:
    return await owned(database, user.id, instance_id)


@router.post("/{instance_id}/start", response_model=AcceptedOut, status_code=202)
async def start(instance_id: UUID, user: CurrentUser, database: Database) -> AcceptedOut:
    item = await owned(database, user.id, instance_id)
    if item.status not in (InstanceStatus.STOPPED, InstanceStatus.HIBERNATED):
        raise HTTPException(409, "Instance cannot be started in its current state")
    return await enqueue(database, user.id, item, OperationType.START)


@router.post("/{instance_id}/retry", response_model=AcceptedOut, status_code=202)
async def retry(instance_id: UUID, user: CurrentUser, database: Database) -> AcceptedOut:
    item = await owned(database, user.id, instance_id)
    if item.status not in [InstanceStatus.ERROR, InstanceStatus.UNKNOWN]:
        raise HTTPException(409, "Only a failed launch can be retried")
    previous = await database.operations.find_one(
        {
            "instance_id": str(item.id),
            "operation_type": OperationType.LAUNCH,
            "status": OperationStatus.FAILED,
        },
        sort=[("created_at", -1)],
    )
    if not previous:
        raise HTTPException(409, "Original launch request is unavailable")
    operation = Operation(
        user_id=user.id,
        instance_id=item.id,
        operation_type=OperationType.LAUNCH,
        request_payload=previous.get("request_payload", {}),
    )
    await database.instances.update_one(
        {"_id": str(item.id)},
        {"$set": {"status": InstanceStatus.PROVISIONING, "last_error": None}},
    )
    await database.operations.insert_one(to_document(operation))
    return AcceptedOut(operation_id=operation.id, instance_id=item.id, status=operation.status)


@router.post("/{instance_id}/stop", response_model=AcceptedOut, status_code=202)
async def stop(instance_id: UUID, user: CurrentUser, database: Database) -> AcceptedOut:
    item = await owned(database, user.id, instance_id)
    if item.status != InstanceStatus.RUNNING:
        raise HTTPException(409, "Instance is not running")
    return await enqueue(database, user.id, item, OperationType.STOP)


@router.post("/{instance_id}/hibernate", response_model=AcceptedOut, status_code=202)
async def hibernate(instance_id: UUID, user: CurrentUser, database: Database) -> AcceptedOut:
    item = await owned(database, user.id, instance_id)
    if item.status != InstanceStatus.RUNNING:
        raise HTTPException(409, "Only a running instance can be hibernated")
    return await enqueue(database, user.id, item, OperationType.HIBERNATE)


@router.delete("/{instance_id}", response_model=AcceptedOut, status_code=202)
async def terminate(instance_id: UUID, user: CurrentUser, database: Database) -> AcceptedOut:
    return await enqueue(
        database,
        user.id,
        await owned(database, user.id, instance_id),
        OperationType.TERMINATE,
    )


@router.get("/{instance_id}/metrics/snapshot")
async def metrics(instance_id: UUID, user: CurrentUser, database: Database) -> dict[str, object]:
    item = await owned(database, user.id, instance_id)
    if item.status != InstanceStatus.RUNNING:
        raise HTTPException(409, "Instance is stopped")
    output = await provider.exec(
        item.multipass_name,
        [
            "sh",
            "-lc",
            CPU_PERCENT_COMMAND + "; echo; "
            "free -m | awk 'NR==2{printf \"%.1f\",$3*100/$2}'; echo; "
            "df -P / | awk 'NR==2{gsub(/%/,\"\",$5);print $5}'; "
            "awk 'NR>2{rx+=$2;tx+=$10} END{print rx;print tx}' /proc/net/dev",
        ],
    )
    values = output.splitlines()
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "cpu_percent": float(values[0] or 0),
        "memory_percent": float(values[1] or 0),
        "disk_percent": float(values[2] or 0),
        "network_rx_bytes": int(values[3] or 0),
        "network_tx_bytes": int(values[4] or 0),
    }
