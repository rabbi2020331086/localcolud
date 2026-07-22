from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import (
    AutoScalingGroup,
    Instance,
    InstanceStatus,
    Operation,
    OperationStatus,
    OperationType,
    TargetGroup,
)
from app.schemas import TargetGroupIn, TargetGroupOut

router = APIRouter(prefix="/target-groups", tags=["Target groups"])


async def owned(database: Database, user_id: UUID, item_id: UUID) -> TargetGroup:
    row = from_document(
        TargetGroup,
        await database.target_groups.find_one({"_id": str(item_id), "user_id": str(user_id)}),
    )
    if not row:
        raise HTTPException(404, "Target group not found")
    return row


async def target_instances(database: Database, row: TargetGroup) -> list[Instance]:
    query: dict[str, object] = {
        "user_id": str(row.user_id),
        "deleted_at": None,
        "status": InstanceStatus.RUNNING,
    }
    if row.registration_type == "autoscaling":
        query["autoscaling_group_id"] = str(row.autoscaling_group_id)
    else:
        query["_id"] = {"$in": [str(value) for value in row.instance_ids]}
    return from_documents(Instance, await database.instances.find(query).to_list(None))


async def validate_references(database: Database, user_id: UUID, data: TargetGroupIn) -> None:
    if data.registration_type == "autoscaling":
        group = from_document(
            AutoScalingGroup,
            await database.autoscaling_groups.find_one(
                {"_id": str(data.autoscaling_group_id), "user_id": str(user_id)}
            ),
        )
        if not group:
            raise HTTPException(404, "Autoscaling group not found")
        return
    ids = list(dict.fromkeys(data.instance_ids))
    count = await database.instances.count_documents(
        {
            "_id": {"$in": [str(value) for value in ids]},
            "user_id": str(user_id),
            "deleted_at": None,
            "status": InstanceStatus.RUNNING,
        }
    )
    if count != len(ids):
        raise HTTPException(422, "Every selected target must be a running instance you own")


async def output(database: Database, row: TargetGroup) -> TargetGroupOut:
    targets = await target_instances(database, row)
    healthy = sum(item.public_endpoint_status == "ACTIVE" for item in targets)
    configuring = any(item.public_endpoint_status == "CONFIGURING" for item in targets)
    health_status = (
        "healthy"
        if targets and healthy == len(targets)
        else "initial"
        if configuring
        else "unhealthy"
    )
    return TargetGroupOut.model_validate(
        {
            **row.model_dump(),
            "registered_targets": len(targets),
            "healthy_targets": healthy,
            "health_status": health_status,
        }
    )


async def enqueue_endpoint_setup(database: Database, targets: list[Instance]) -> None:
    for instance in targets:
        if instance.public_endpoint_status in ("ACTIVE", "CONFIGURING"):
            continue
        active = await database.operations.find_one(
            {
                "instance_id": str(instance.id),
                "operation_type": OperationType.PUBLISH_ENDPOINT,
                "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
            }
        )
        if active:
            continue
        await database.instances.update_one(
            {"_id": str(instance.id)},
            {"$set": {"public_endpoint_status": "CONFIGURING"}},
        )
        operation = Operation(
            user_id=instance.user_id,
            instance_id=instance.id,
            operation_type=OperationType.PUBLISH_ENDPOINT,
        )
        await database.operations.insert_one(to_document(operation))


@router.get("", response_model=list[TargetGroupOut])
async def list_groups(user: CurrentUser, database: Database) -> list[TargetGroupOut]:
    rows = from_documents(
        TargetGroup,
        await database.target_groups.find({"user_id": str(user.id)})
        .sort("created_at", -1)
        .to_list(None),
    )
    return [await output(database, row) for row in rows]


@router.post("", response_model=TargetGroupOut, status_code=201)
async def create_group(
    data: TargetGroupIn, user: CurrentUser, database: Database
) -> TargetGroupOut:
    await validate_references(database, user.id, data)
    row = TargetGroup(user_id=user.id, **data.model_dump())
    try:
        await database.target_groups.insert_one(to_document(row))
    except DuplicateKeyError:
        raise HTTPException(409, "A target group with this name already exists") from None
    await enqueue_endpoint_setup(database, await target_instances(database, row))
    return await output(database, row)


@router.get("/{item_id}", response_model=TargetGroupOut)
async def get_group(item_id: UUID, user: CurrentUser, database: Database) -> TargetGroupOut:
    return await output(database, await owned(database, user.id, item_id))


@router.patch("/{item_id}", response_model=TargetGroupOut)
async def update_group(
    item_id: UUID, data: TargetGroupIn, user: CurrentUser, database: Database
) -> TargetGroupOut:
    row = await owned(database, user.id, item_id)
    await validate_references(database, user.id, data)
    update = {**data.model_dump(mode="json"), "updated_at": datetime.now(UTC)}
    try:
        await database.target_groups.update_one({"_id": str(row.id)}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "A target group with this name already exists") from None
    changed = TargetGroup.model_validate({**row.model_dump(), **update})
    await enqueue_endpoint_setup(database, await target_instances(database, changed))
    return await output(database, changed)


@router.post("/{item_id}/register-targets", response_model=TargetGroupOut)
async def register_targets(item_id: UUID, user: CurrentUser, database: Database) -> TargetGroupOut:
    row = await owned(database, user.id, item_id)
    targets = await target_instances(database, row)
    await enqueue_endpoint_setup(database, targets)
    return await output(database, row)


@router.delete("/{item_id}", status_code=204)
async def delete_group(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row = await owned(database, user.id, item_id)
    if await database.load_balancers.find_one({"target_group_id": str(row.id)}):
        raise HTTPException(409, "Target group is attached to a load balancer")
    await database.target_groups.delete_one({"_id": str(row.id)})
