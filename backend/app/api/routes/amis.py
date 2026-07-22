from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.core.config import settings
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import Ami, AmiStatus, CloudNode, Instance, InstanceStatus
from app.schemas import AmiIn, AmiOut
from app.services import node_jobs, scheduler

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
            {"_id": str(data.source_instance_id), "user_id": str(user.id), "deleted_at": None}
        ),
    )
    if not source:
        raise HTTPException(404, "Source instance not found")
    if source.status != InstanceStatus.STOPPED:
        raise HTTPException(409, "Stop the source instance before creating an AMI")
    scheduler.require_distributed(source)
    row = Ami(
        user_id=user.id,
        name=data.name,
        description=data.description,
        source_instance_id=source.id,
        node_id=source.node_id,
        backing_instance_name=source.multipass_name,
        image=source.image,
        cpu_count=source.cpu_count,
        memory_mb=source.memory_mb,
        disk_gb=source.disk_gb,
    )
    try:
        await database.amis.insert_one(to_document(row))
        await node_jobs.dispatch(
            source,
            "AMI_CAPTURE",
            {"name": source.multipass_name, "ami_id": str(row.id)},
            timeout=1800,
        )
        await database.amis.update_one(
            {"_id": str(row.id)},
            {"$set": {"status": AmiStatus.AVAILABLE, "updated_at": datetime.now(UTC)}},
        )
        row.status = AmiStatus.AVAILABLE
    except DuplicateKeyError:
        raise HTTPException(409, "An AMI with this name already exists") from None
    except Exception:
        await database.amis.update_one(
            {"_id": str(row.id)},
            {"$set": {"status": AmiStatus.FAILED}},
        )
        Path(settings.ami_storage_root).expanduser().joinpath(f"{row.id}.box").unlink(
            missing_ok=True
        )
        raise
    return row


@router.delete("/{ami_id}", status_code=204)
async def delete_ami(ami_id: UUID, user: CurrentUser, database: Database) -> None:
    row = from_document(
        Ami,
        await database.amis.find_one({"_id": str(ami_id), "user_id": str(user.id)}),
    )
    if not row:
        raise HTTPException(404, "AMI not found")
    if row.status == AmiStatus.CREATING:
        raise HTTPException(409, "AMI capture is still in progress")
    if await database.instances.find_one({"ami_id": str(row.id), "deleted_at": None}):
        raise HTTPException(409, "AMI is used by an active instance")
    Path(settings.ami_storage_root).expanduser().joinpath(f"{row.id}.box").unlink(missing_ok=True)
    nodes = from_documents(CloudNode, await database.cloud_nodes.find({}).to_list(None))
    for node in nodes:
        try:
            await node_jobs.dispatch_node(
                node, "AMI_DELETE_LOCAL", {"ami_id": str(row.id)}, timeout=30
            )
        except Exception:
            pass
    await database.amis.delete_one({"_id": str(row.id)})
