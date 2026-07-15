from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.dependencies import CurrentUser
from app.infrastructure.database import Database, from_document, from_documents
from app.infrastructure.models import Ami
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
    raise HTTPException(
        501,
        "Distributed AMI capture requires node image storage and cross-node replication; "
        "legacy control-plane cloning has been disabled.",
    )


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
    raise HTTPException(
        501,
        "Legacy AMI deletion cannot safely modify a compute node. Remove it after distributed "
        "image storage is configured.",
    )
