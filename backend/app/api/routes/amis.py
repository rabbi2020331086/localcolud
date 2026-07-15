from uuid import UUID

from fastapi import APIRouter, HTTPException
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import Ami, Instance, InstanceStatus
from app.infrastructure.vm import ProviderUnavailable, provider, safe_name
from app.schemas import AmiIn, AmiOut

router = APIRouter(prefix="/amis", tags=["AMIs"])


@router.get("", response_model=list[AmiOut])
async def list_amis(user: CurrentUser, database: Database) -> list[Ami]:
    return from_documents(
        Ami,
        await database.amis.find({"user_id": str(user.id)}).sort("created_at", -1).to_list(None),
    )


@router.post("", response_model=AmiOut, status_code=201)
async def create_ami(data: AmiIn, user: CurrentUser, database: Database) -> Ami:
    source = from_document(
        Instance,
        await database.instances.find_one(
            {
                "_id": str(data.source_instance_id),
                "user_id": str(user.id),
                "deleted_at": None,
            }
        ),
    )
    if not source:
        raise HTTPException(404, "Source instance not found")
    if source.status != InstanceStatus.STOPPED:
        raise HTTPException(409, "Stop the source instance before creating an AMI")
    row = Ami(
        user_id=user.id,
        name=data.name,
        description=data.description,
        source_instance_id=source.id,
        backing_instance_name=safe_name(str(user.id), f"ami-{data.name}"),
        image=source.image,
        cpu_count=source.cpu_count,
        memory_mb=source.memory_mb,
        disk_gb=source.disk_gb,
    )
    if await database.amis.find_one({"user_id": str(user.id), "name": row.name}):
        raise HTTPException(409, "An AMI with this name already exists")
    try:
        await provider.clone(
            source.multipass_name,
            row.backing_instance_name,
            source.cpu_count,
            source.memory_mb,
            source.disk_gb,
        )
        await provider.stop(row.backing_instance_name)
        await database.amis.insert_one(to_document(row))
    except DuplicateKeyError:
        await provider.delete(row.backing_instance_name)
        raise HTTPException(409, "An AMI with this name already exists") from None
    except ProviderUnavailable as exc:
        await provider.delete(row.backing_instance_name)
        raise HTTPException(409, str(exc)) from None
    return row


@router.delete("/{ami_id}", status_code=204)
async def delete_ami(ami_id: UUID, user: CurrentUser, database: Database) -> None:
    row = from_document(
        Ami,
        await database.amis.find_one({"_id": str(ami_id), "user_id": str(user.id)}),
    )
    if not row:
        raise HTTPException(404, "AMI not found")
    if await database.instances.find_one({"ami_id": str(row.id), "deleted_at": None}):
        raise HTTPException(409, "AMI is used by an active instance")
    try:
        await provider.delete(row.backing_instance_name)
    except ProviderUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    await database.amis.delete_one({"_id": str(row.id)})
