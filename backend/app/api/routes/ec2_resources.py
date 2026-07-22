import base64
import io
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.core.config import settings
from app.core.security import fernet
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import (
    CloudNode,
    Instance,
    InstanceStatus,
    KeyPair,
    NodeStatus,
    SecurityGroup,
    Volume,
)
from app.schemas import (
    KeyPairIn,
    KeyPairOut,
    SecurityGroupIn,
    SecurityGroupOut,
    VolumeAttachIn,
    VolumeIn,
    VolumeOut,
    VolumeStorageNodeOut,
)
from app.services import node_jobs
from app.services.operations import key_pair, security_script

key_pairs = APIRouter(prefix="/key-pairs", tags=["Key pairs"])
security_groups = APIRouter(prefix="/security-groups", tags=["Security groups"])
volumes = APIRouter(prefix="/volumes", tags=["Volumes"])


def volume_output(
    row: Volume,
    usage: tuple[int, int, float] | None = None,
    attachment_zone: str | None = None,
) -> VolumeOut:
    capacity = row.size_gb * 1024**3
    used, available, percent = usage or (0, capacity, 0)
    return VolumeOut.model_validate(
        {
            **row.model_dump(),
            "used_bytes": used,
            "available_bytes": available,
            "usage_percent": percent,
            "attachment_zone": attachment_zone,
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
    output: list[VolumeOut] = []
    for row in rows:
        usage = None
        attachment_zone = None
        if row.attached_instance_id and row.mount_path:
            instance = from_document(
                Instance,
                await database.instances.find_one({"_id": str(row.attached_instance_id)}),
            )
            if instance and instance.status == InstanceStatus.RUNNING:
                attachment_zone = instance.availability_zone
                try:
                    value = await node_jobs.remote_exec(
                        instance,
                        f"df -B1 --output=used,avail,pcent {shlex.quote(row.mount_path)} | tail -1",
                        timeout=15,
                    )
                    fields = value.strip().split()
                    usage = (int(fields[0]), int(fields[1]), float(fields[2].rstrip("%")))
                except Exception:
                    pass
        node_ids = [value for value in (row.origin_node_id, row.node_id) if value]
        nodes = {
            str(value["_id"]): str(value.get("name") or "Unknown node")
            for value in await database.cloud_nodes.find(
                {"_id": {"$in": [str(value) for value in node_ids]}}
            ).to_list(None)
        }
        result = volume_output(row, usage, attachment_zone)
        result.origin_node_name = nodes.get(str(row.origin_node_id))
        result.current_node_id = row.node_id
        result.current_node_name = nodes.get(str(row.node_id))
        output.append(result)
    return output


@volumes.get("/storage-nodes", response_model=list[VolumeStorageNodeOut])
async def volume_storage_nodes(user: CurrentUser, database: Database) -> list[dict[str, object]]:
    del user
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    rows = await database.cloud_nodes.find(
        {
            "approved": True,
            "schedulable": True,
            "status": NodeStatus.ONLINE.value,
            "last_seen_at": {"$gte": deadline},
            "capabilities": "volume-create",
        }
    ).sort("name", 1).to_list(None)
    return [
        {
            "id": row["_id"],
            "name": row["name"],
            "availability_zone": row["availability_zone"],
            "storage_total_gb": int(row.get("storage_total_gb") or 0),
            "storage_allocated_gb": int(row.get("storage_allocated_gb") or 0),
        }
        for row in rows
    ]


@volumes.post("", response_model=VolumeOut, status_code=201)
async def volume_create(data: VolumeIn, user: CurrentUser, database: Database) -> VolumeOut:
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    node = from_document(
        CloudNode,
        await database.cloud_nodes.find_one(
            {
                "_id": str(data.node_id),
                "approved": True,
                "schedulable": True,
                "status": NodeStatus.ONLINE.value,
                "last_seen_at": {"$gte": deadline},
                "capabilities": "volume-create",
            }
        ),
    )
    if not node:
        raise HTTPException(409, "Select an online approved storage node")
    available_gb = node.storage_total_gb - node.storage_reserved_gb - node.storage_allocated_gb
    if node.storage_total_gb and data.size_gb > available_gb:
        raise HTTPException(409, "The selected node does not have enough allocatable storage")
    row = Volume(
        user_id=user.id,
        name=data.name,
        size_gb=data.size_gb,
        availability_zone=node.availability_zone,
        host_path="",
        storage_backend="node-synchronized",
        node_id=node.id,
        origin_node_id=node.id,
        last_node_id=node.id,
    )
    row.host_path = str(
        Path(settings.volume_storage_root).expanduser().resolve() / f"{row.id}.qcow2"
    )
    await database.volumes.insert_one(to_document(row))
    try:
        result = await node_jobs.dispatch_node(
            node,
            "VOLUME_CREATE",
            {
                # Agent 0.5.12 validates the common job context before it
                # reaches the host-only volume branch. Keep a safe synthetic
                # name so already-upgraded nodes can execute this job.
                "name": "volume-storage",
                "volume_id": str(row.id),
                "size_gb": row.size_gb,
            },
            timeout=600,
        )
    except Exception as exc:
        await database.volumes.delete_one({"_id": str(row.id)})
        Path(row.host_path).unlink(missing_ok=True)
        raise HTTPException(502, f"Storage node could not create the volume: {exc}") from exc
    row.host_path = str(result.get("host_path") or row.host_path)
    await database.volumes.update_one(
        {"_id": str(row.id)},
        {"$set": {"host_path": row.host_path, "updated_at": datetime.now(UTC)}},
    )
    result_out = volume_output(row)
    result_out.origin_node_name = node.name
    result_out.current_node_id = node.id
    result_out.current_node_name = node.name
    return result_out


@volumes.post("/{item_id}/attach", response_model=VolumeOut)
async def volume_attach(
    item_id: UUID, data: VolumeAttachIn, user: CurrentUser, database: Database
) -> VolumeOut:
    row: Volume = await owned(database, "volumes", Volume, user.id, item_id)
    if row.attached_instance_id:
        raise HTTPException(409, "Volume is already attached")
    instance = from_document(
        Instance,
        await database.instances.find_one(
            {"_id": str(data.instance_id), "user_id": str(user.id), "deleted_at": None}
        ),
    )
    if not instance or instance.status != InstanceStatus.RUNNING or not instance.node_id:
        raise HTTPException(409, "Select a running distributed instance")
    claimed = await database.volumes.update_one(
        {
            "_id": str(row.id),
            "attached_instance_id": None,
            "attachment_pending": {"$ne": True},
        },
        {"$set": {"attachment_pending": True, "updated_at": datetime.now(UTC)}},
    )
    if not claimed.modified_count:
        raise HTTPException(409, "Volume attachment is already in progress")
    attached = await database.volumes.find({"attached_instance_id": str(instance.id)}).to_list(None)
    used_devices = {str(value.get("device_name")) for value in attached}
    device_name = next(
        (
            f"vd{letter}"
            for letter in "bcdefghijklmnopqrstuvwxyz"
            if f"vd{letter}" not in used_devices
        ),
        None,
    )
    if not device_name:
        raise HTTPException(409, "Instance has no available block-device slots")
    try:
        result = await node_jobs.dispatch(
            instance,
            "VOLUME_ATTACH",
            {
                "name": instance.multipass_name,
                "volume_id": str(row.id),
                "size_gb": row.size_gb,
                "mount_path": data.mount_path,
                "device_name": device_name,
            },
            timeout=600,
        )
    except Exception:
        await database.volumes.update_one(
            {"_id": str(row.id)}, {"$unset": {"attachment_pending": ""}}
        )
        raise
    update = {
        "attached_instance_id": str(instance.id),
        "node_id": str(instance.node_id),
        "origin_node_id": str(row.origin_node_id or instance.node_id),
        "last_node_id": str(instance.node_id),
        "device_name": str(result.get("device_name") or device_name),
        "mount_path": data.mount_path,
        "host_path": row.host_path,
        "updated_at": datetime.now(UTC),
    }
    await database.volumes.update_one(
        {"_id": str(row.id)}, {"$set": update, "$unset": {"attachment_pending": ""}}
    )
    updated = Volume.model_validate({**row.model_dump(), **update})
    return volume_output(
        updated,
        (
            int(str(result.get("used_bytes") or 0)),
            int(str(result.get("available_bytes") or 0)),
            float(str(result.get("usage_percent") or 0)),
        ),
    )


@volumes.post("/{item_id}/detach", response_model=VolumeOut)
async def volume_detach(item_id: UUID, user: CurrentUser, database: Database) -> VolumeOut:
    row: Volume = await owned(database, "volumes", Volume, user.id, item_id)
    if not row.attached_instance_id:
        return volume_output(row)
    instance = from_document(
        Instance, await database.instances.find_one({"_id": str(row.attached_instance_id)})
    )
    if not instance or instance.status != InstanceStatus.RUNNING:
        raise HTTPException(409, "Start the attached instance before detaching its volume")
    await node_jobs.dispatch(
        instance,
        "VOLUME_DETACH",
        {
            "name": instance.multipass_name,
            "mount_path": row.mount_path or "/mnt/data",
            "device_name": row.device_name or "vdb",
            "volume_id": str(row.id),
        },
        timeout=120,
    )
    update = {
        "attached_instance_id": None,
        "device_name": None,
        "mount_path": None,
        "node_id": None,
        "last_node_id": str(instance.node_id),
        "updated_at": datetime.now(UTC),
    }
    await database.volumes.update_one({"_id": str(row.id)}, {"$set": update})
    return volume_output(Volume.model_validate({**row.model_dump(), **update}))


@volumes.delete("/{item_id}", status_code=204)
async def volume_delete(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row: Volume = await owned(database, "volumes", Volume, user.id, item_id)
    if row.attached_instance_id:
        raise HTTPException(409, "Detach the volume before deleting it")
    canonical = Path(settings.volume_storage_root).expanduser().resolve() / f"{row.id}.qcow2"
    canonical.unlink(missing_ok=True)
    await database.volumes.delete_one({"_id": str(row.id)})
