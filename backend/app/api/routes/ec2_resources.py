import base64
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.core.security import fernet
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import Instance, InstanceStatus, KeyPair, SecurityGroup, Volume
from app.schemas import (
    KeyPairIn,
    KeyPairOut,
    SecurityGroupIn,
    SecurityGroupOut,
    VolumeAttachIn,
    VolumeIn,
    VolumeOut,
)
from app.services import node_jobs
from app.services.operations import key_pair, security_script

key_pairs = APIRouter(prefix="/key-pairs", tags=["Key pairs"])
security_groups = APIRouter(prefix="/security-groups", tags=["Security groups"])
volumes = APIRouter(prefix="/volumes", tags=["Volumes"])


def volume_output(row: Volume) -> VolumeOut:
    capacity = row.size_gb * 1024**3
    return VolumeOut.model_validate(
        {
            **row.model_dump(),
            "used_bytes": 0,
            "available_bytes": capacity,
            "usage_percent": 0,
        }
    )


async def owned(
    database: Database, collection: str, model: type, user_id: UUID, item_id: UUID
) -> Any:
    row = from_document(
        model,
        await database[collection].find_one({"_id": str(item_id), "user_id": str(user_id)}),
    )
    if not row:
        raise HTTPException(404, f"{model.__name__} not found")
    return row


@key_pairs.get("", response_model=list[KeyPairOut])
async def key_list(user: CurrentUser, database: Database) -> list[KeyPair]:
    return from_documents(
        KeyPair,
        await database.key_pairs.find({"user_id": str(user.id)}).sort("name", 1).to_list(None),
    )


@key_pairs.post("", response_model=KeyPairOut, status_code=201)
async def key_create(data: KeyPairIn, user: CurrentUser, database: Database) -> KeyPair:
    private, public, fingerprint = key_pair()
    row = KeyPair(
        user_id=user.id,
        name=data.name,
        public_key=public,
        encrypted_private_key=fernet().encrypt(private),
        fingerprint=fingerprint,
    )
    try:
        await database.key_pairs.insert_one(to_document(row))
    except DuplicateKeyError:
        raise HTTPException(409, "A key pair with this name already exists") from None
    return row


@key_pairs.get("/{item_id}/download")
async def key_download(item_id: UUID, user: CurrentUser, database: Database) -> StreamingResponse:
    row: KeyPair = await owned(database, "key_pairs", KeyPair, user.id, item_id)
    if not row.encrypted_private_key:
        raise HTTPException(410, "The private key has already been downloaded")
    private = fernet().decrypt(row.encrypted_private_key)
    await database.key_pairs.update_one(
        {"_id": str(row.id)}, {"$set": {"encrypted_private_key": b""}}
    )
    return StreamingResponse(
        io.BytesIO(private),
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="{row.name}.pem"'},
    )


@key_pairs.delete("/{item_id}", status_code=204)
async def key_delete(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row: KeyPair = await owned(database, "key_pairs", KeyPair, user.id, item_id)
    if await database.instances.find_one({"key_pair_id": str(row.id), "deleted_at": None}):
        raise HTTPException(409, "Key pair is used by an instance")
    if await database.launch_templates.find_one({"key_pair_id": str(row.id)}):
        raise HTTPException(409, "Key pair is used by a launch template")
    await database.key_pairs.delete_one({"_id": str(row.id)})


@security_groups.get("", response_model=list[SecurityGroupOut])
async def sg_list(user: CurrentUser, database: Database) -> list[SecurityGroup]:
    return from_documents(
        SecurityGroup,
        await database.security_groups.find({"user_id": str(user.id)})
        .sort("name", 1)
        .to_list(None),
    )


@security_groups.post("", response_model=SecurityGroupOut, status_code=201)
async def sg_create(data: SecurityGroupIn, user: CurrentUser, database: Database) -> SecurityGroup:
    row = SecurityGroup(user_id=user.id, **data.model_dump())
    try:
        await database.security_groups.insert_one(to_document(row))
    except DuplicateKeyError:
        raise HTTPException(409, "A security group with this name already exists") from None
    return row


@security_groups.put("/{item_id}", response_model=SecurityGroupOut)
async def sg_update(
    item_id: UUID, data: SecurityGroupIn, user: CurrentUser, database: Database
) -> SecurityGroup:
    row: SecurityGroup = await owned(database, "security_groups", SecurityGroup, user.id, item_id)
    update = {**data.model_dump(mode="json"), "updated_at": datetime.now(UTC)}
    await database.security_groups.update_one({"_id": str(row.id)}, {"$set": update})
    affected = from_documents(
        Instance,
        await database.instances.find(
            {
                "security_group_ids": str(row.id),
                "deleted_at": None,
                "status": InstanceStatus.RUNNING,
                "node_id": {"$ne": None},
            }
        ).to_list(None),
    )
    failures = 0
    for instance in affected:
        script = await security_script(instance)
        encoded = base64.b64encode(script.encode()).decode()
        try:
            await node_jobs.remote_exec(
                instance,
                f"printf '%s' '{encoded}' | base64 -d | sudo bash",
                timeout=120,
            )
        except Exception:
            failures += 1
    if failures:
        raise HTTPException(
            502,
            f"Policy was saved, but {failures} running instance(s) could not apply it; "
            "restart those instances after their nodes reconnect.",
        )
    return SecurityGroup.model_validate({**row.model_dump(), **update})


@security_groups.delete("/{item_id}", status_code=204)
async def sg_delete(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row: SecurityGroup = await owned(database, "security_groups", SecurityGroup, user.id, item_id)
    if await database.instances.find_one({"security_group_ids": str(row.id), "deleted_at": None}):
        raise HTTPException(409, "Security group is used by an instance")
    await database.security_groups.delete_one({"_id": str(row.id)})


@volumes.get("", response_model=list[VolumeOut])
async def volume_list(user: CurrentUser, database: Database) -> list[VolumeOut]:
    rows = from_documents(
        Volume,
        await database.volumes.find({"user_id": str(user.id)}).sort("name", 1).to_list(None),
    )
    return [volume_output(row) for row in rows]


@volumes.post("", response_model=VolumeOut, status_code=201)
async def volume_create(data: VolumeIn, user: CurrentUser, database: Database) -> VolumeOut:
    raise HTTPException(
        501,
        "Distributed volumes require Ceph RBD or another node-accessible block-storage backend; "
        "control-plane host directories have been disabled.",
    )


@volumes.post("/{item_id}/attach", response_model=VolumeOut)
async def volume_attach(
    item_id: UUID, data: VolumeAttachIn, user: CurrentUser, database: Database
) -> VolumeOut:
    raise HTTPException(501, "Distributed volume attachment is not configured")


@volumes.post("/{item_id}/detach", response_model=VolumeOut)
async def volume_detach(item_id: UUID, user: CurrentUser, database: Database) -> VolumeOut:
    raise HTTPException(501, "Distributed volume detachment is not configured")


@volumes.delete("/{item_id}", status_code=204)
async def volume_delete(item_id: UUID, user: CurrentUser, database: Database) -> None:
    raise HTTPException(501, "Distributed volume deletion is not configured")
