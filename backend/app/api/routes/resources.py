import io
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.core.security import fernet, token_hash
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import (
    AutoScalingGroup,
    Instance,
    LaunchTemplate,
    OneTimeSecret,
    Operation,
    OperationStatus,
    OperationType,
    ScalingEvent,
)
from app.schemas import (
    DeleteGroupIn,
    DesiredIn,
    GroupIn,
    GroupOut,
    OperationOut,
    TemplateIn,
    TemplateOut,
)

operations = APIRouter(prefix="/operations", tags=["Operations"])


@operations.get("", response_model=list[OperationOut])
async def ops(user: CurrentUser, database: Database) -> list[Operation]:
    documents = (
        await database.operations.find({"user_id": str(user.id)})
        .sort("created_at", -1)
        .limit(100)
        .to_list(length=100)
    )
    return from_documents(Operation, documents)


@operations.get("/{operation_id}", response_model=OperationOut)
async def op(operation_id: UUID, user: CurrentUser, database: Database) -> Operation:
    row = from_document(
        Operation,
        await database.operations.find_one({"_id": str(operation_id), "user_id": str(user.id)}),
    )
    if not row:
        raise HTTPException(404, "Operation not found")
    return row


keys = APIRouter(prefix="/key-downloads", tags=["Keys"])


@keys.get("/{token}")
async def download(token: str, user: CurrentUser, database: Database) -> StreamingResponse:
    now = datetime.now(UTC)
    document = await database.one_time_secrets.find_one_and_update(
        {
            "token_hash": token_hash(token),
            "user_id": str(user.id),
            "consumed_at": None,
            "expires_at": {"$gt": now},
        },
        {"$set": {"consumed_at": now}},
    )
    row = from_document(OneTimeSecret, document)
    if not row:
        raise HTTPException(404, "Key is unavailable or expired")
    value = fernet().decrypt(row.encrypted_value)
    await database.one_time_secrets.update_one(
        {"_id": str(row.id)}, {"$set": {"encrypted_value": b""}}
    )
    return StreamingResponse(
        io.BytesIO(value),
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": f'attachment; filename="{row.filename}"',
            "Cache-Control": "no-store",
        },
    )


templates = APIRouter(prefix="/launch-templates", tags=["Launch templates"])


async def template_owned(database: Database, user_id: UUID, item_id: UUID) -> LaunchTemplate:
    row = from_document(
        LaunchTemplate,
        await database.launch_templates.find_one({"_id": str(item_id), "user_id": str(user_id)}),
    )
    if not row:
        raise HTTPException(404, "Launch template not found")
    return row


@templates.get("", response_model=list[TemplateOut])
async def template_list(user: CurrentUser, database: Database) -> list[LaunchTemplate]:
    documents = (
        await database.launch_templates.find({"user_id": str(user.id)})
        .sort("created_at", -1)
        .to_list(length=None)
    )
    return from_documents(LaunchTemplate, documents)


@templates.post("", response_model=TemplateOut, status_code=201)
async def template_create(
    data: TemplateIn, user: CurrentUser, database: Database
) -> LaunchTemplate:
    if data.key_pair_id and not await database.key_pairs.find_one(
        {"_id": str(data.key_pair_id), "user_id": str(user.id)}
    ):
        raise HTTPException(404, "Key pair not found")
    row = LaunchTemplate(user_id=user.id, **data.model_dump())
    try:
        await database.launch_templates.insert_one(to_document(row))
    except DuplicateKeyError:
        raise HTTPException(409, "A launch template with this name already exists") from None
    return row


@templates.get("/{item_id}", response_model=TemplateOut)
async def template_get(item_id: UUID, user: CurrentUser, database: Database) -> LaunchTemplate:
    return await template_owned(database, user.id, item_id)


@templates.patch("/{item_id}", response_model=TemplateOut)
async def template_patch(
    item_id: UUID, data: TemplateIn, user: CurrentUser, database: Database
) -> LaunchTemplate:
    row = await template_owned(database, user.id, item_id)
    if data.key_pair_id and not await database.key_pairs.find_one(
        {"_id": str(data.key_pair_id), "user_id": str(user.id)}
    ):
        raise HTTPException(404, "Key pair not found")
    update = data.model_dump()
    update["updated_at"] = datetime.now(UTC)
    try:
        await database.launch_templates.update_one({"_id": str(row.id)}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "A launch template with this name already exists") from None
    return LaunchTemplate.model_validate({**row.model_dump(), **update})


@templates.delete("/{item_id}", status_code=204)
async def template_delete(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row = await template_owned(database, user.id, item_id)
    if await database.autoscaling_groups.find_one({"launch_template_id": str(row.id)}):
        raise HTTPException(409, "Launch template is used by an autoscaling group")
    await database.launch_templates.delete_one({"_id": str(row.id)})


groups = APIRouter(prefix="/autoscaling-groups", tags=["Autoscaling"])


async def group_owned(database: Database, user_id: UUID, item_id: UUID) -> AutoScalingGroup:
    row = from_document(
        AutoScalingGroup,
        await database.autoscaling_groups.find_one({"_id": str(item_id), "user_id": str(user_id)}),
    )
    if not row:
        raise HTTPException(404, "Autoscaling group not found")
    return row


@groups.get("", response_model=list[GroupOut])
async def group_list(user: CurrentUser, database: Database) -> list[AutoScalingGroup]:
    documents = (
        await database.autoscaling_groups.find({"user_id": str(user.id)})
        .sort("created_at", -1)
        .to_list(length=None)
    )
    return from_documents(AutoScalingGroup, documents)


@groups.post("", response_model=GroupOut, status_code=201)
async def group_create(data: GroupIn, user: CurrentUser, database: Database) -> AutoScalingGroup:
    if not await database.launch_templates.find_one(
        {"_id": str(data.launch_template_id), "user_id": str(user.id)}
    ):
        raise HTTPException(404, "Launch template not found")
    row = AutoScalingGroup(user_id=user.id, **data.model_dump())
    try:
        await database.autoscaling_groups.insert_one(to_document(row))
    except DuplicateKeyError:
        raise HTTPException(409, "An autoscaling group with this name already exists") from None
    return row


@groups.get("/{item_id}", response_model=GroupOut)
async def group_get(item_id: UUID, user: CurrentUser, database: Database) -> AutoScalingGroup:
    return await group_owned(database, user.id, item_id)


@groups.patch("/{item_id}", response_model=GroupOut)
async def group_patch(
    item_id: UUID, data: GroupIn, user: CurrentUser, database: Database
) -> AutoScalingGroup:
    row = await group_owned(database, user.id, item_id)
    update = data.model_dump(mode="json")
    update["updated_at"] = datetime.now(UTC)
    try:
        await database.autoscaling_groups.update_one({"_id": str(row.id)}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "An autoscaling group with this name already exists") from None
    return AutoScalingGroup.model_validate({**row.model_dump(), **update})


@groups.post("/{item_id}/set-desired-capacity", response_model=GroupOut)
async def group_desired(
    item_id: UUID, data: DesiredIn, user: CurrentUser, database: Database
) -> AutoScalingGroup:
    row = await group_owned(database, user.id, item_id)
    if not row.minimum_size <= data.desired_size <= row.maximum_size:
        raise HTTPException(422, "Desired capacity is outside group bounds")
    now = datetime.now(UTC)
    await database.autoscaling_groups.update_one(
        {"_id": str(row.id)},
        {"$set": {"desired_size": data.desired_size, "updated_at": now}},
    )
    row.desired_size = data.desired_size
    row.updated_at = now
    return row


@groups.get("/{item_id}/events")
async def group_events(item_id: UUID, user: CurrentUser, database: Database) -> list[ScalingEvent]:
    await group_owned(database, user.id, item_id)
    documents = (
        await database.scaling_events.find({"autoscaling_group_id": str(item_id)})
        .sort("created_at", -1)
        .to_list(length=None)
    )
    return from_documents(ScalingEvent, documents)


@groups.delete("/{item_id}", status_code=204)
async def group_delete(
    item_id: UUID, data: DeleteGroupIn, user: CurrentUser, database: Database
) -> None:
    row = await group_owned(database, user.id, item_id)
    if await database.load_balancers.find_one({"autoscaling_group_id": str(row.id)}):
        raise HTTPException(409, "Delete the group's load balancer first")
    # Prevent the autoscaling worker from scheduling more instances while
    # cleanup operations are being queued.
    await database.autoscaling_groups.update_one(
        {"_id": str(row.id)}, {"$set": {"enabled": False, "updated_at": datetime.now(UTC)}}
    )
    managed_documents = await database.instances.find(
        {"autoscaling_group_id": str(row.id), "deleted_at": None}
    ).to_list(length=None)
    managed = from_documents(Instance, managed_documents)
    if managed and not data.terminate_managed_instances:
        raise HTTPException(409, "Group still manages instances; explicitly request termination")
    for instance in managed:
        termination = await database.operations.find_one(
            {
                "instance_id": str(instance.id),
                "operation_type": {"$in": [OperationType.TERMINATE, OperationType.SCALE_IN]},
                "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
            }
        )
        if not termination:
            # If a launch is active, the single worker completes it before
            # this queued termination, so the new VM cannot be orphaned.
            operation = Operation(
                user_id=user.id,
                instance_id=instance.id,
                operation_type=OperationType.TERMINATE,
            )
            await database.operations.insert_one(to_document(operation))
    await database.autoscaling_groups.delete_one({"_id": str(row.id)})
