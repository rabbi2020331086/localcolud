import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.core.config import settings
from app.infrastructure.database import database, from_document, from_documents, to_document
from app.infrastructure.models import (
    CloudNode,
    Instance,
    InstanceStatus,
    RecoveryPoint,
    RecoveryPointStatus,
    ReplicationPolicy,
)
from app.services import node_jobs

worker_task: asyncio.Task[None] | None = None
log = logging.getLogger("localec2.replication")
async def policy_for(instance: Instance) -> ReplicationPolicy:
    row = from_document(
        ReplicationPolicy,
        await database.replication_policies.find_one({"instance_id": str(instance.id)}),
    )
    return row or ReplicationPolicy(user_id=instance.user_id, instance_id=instance.id)


async def eligible_replica_nodes(instance: Instance, count: int) -> list[CloudNode]:
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    rows = (
        await database.cloud_nodes.find(
            {
                "_id": {"$ne": str(instance.node_id)},
                "approved": True,
                "last_seen_at": {"$gte": deadline},
                "provider_name": "vagrant-libvirt",
                "capabilities": {"$all": ["jobs", "vagrant", "libvirt", "kvm"]},
            }
        )
        .sort([("storage_allocated_gb", 1), ("name", 1)])
        .limit(count)
        .to_list(None)
    )
    return from_documents(CloudNode, rows)


async def remove_point(point: RecoveryPoint) -> None:
    nodes = from_documents(
        CloudNode,
        await database.cloud_nodes.find(
            {"_id": {"$in": [str(value) for value in point.replica_node_ids]}}
        ).to_list(None),
    )
    for node in nodes:
        try:
            await node_jobs.dispatch_node(
                node, "REPLICA_DELETE", {"recovery_point_id": str(point.id)}, timeout=90
            )
        except Exception:
            log.warning("Could not remove recovery point %s from node %s", point.id, node.id)
    path = Path(settings.recovery_storage_root).expanduser().resolve() / f"{point.id}.qcow2"
    path.unlink(missing_ok=True)
    await database.recovery_points.delete_one({"_id": str(point.id)})


async def enforce_retention(instance_id: UUID, keep: int) -> None:
    rows = (
        await database.recovery_points.find(
            {"instance_id": str(instance_id), "status": RecoveryPointStatus.AVAILABLE}
        )
        .sort("completed_at", -1)
        .skip(keep)
        .to_list(None)
    )
    for point in from_documents(RecoveryPoint, rows):
        await remove_point(point)


async def capture(instance: Instance, requested_by: UUID | None = None) -> RecoveryPoint:
    if not instance.node_id or instance.status not in {
        InstanceStatus.RUNNING,
        InstanceStatus.STOPPED,
        InstanceStatus.HIBERNATED,
    }:
        raise RuntimeError("Only a placed running or stopped instance can be replicated")
    if await database.operations.find_one(
        {"instance_id": str(instance.id), "status": {"$in": ["PENDING", "RUNNING"]}}
    ):
        raise RuntimeError("Another instance operation is active")
    existing = await database.recovery_points.find_one(
        {"instance_id": str(instance.id), "status": RecoveryPointStatus.CREATING}
    )
    if existing:
        raise RuntimeError("A recovery point is already being created for this instance")
    current_policy = await policy_for(instance)
    point = RecoveryPoint(
        instance_id=instance.id,
        source_node_id=instance.node_id,
        requested_by=requested_by,
        automatic=requested_by is None,
    )
    await database.recovery_points.insert_one(to_document(point))
    try:
        await database.recovery_points.update_one(
            {"_id": str(point.id)},
            {"$set": {"progress": 2, "progress_message": "Waiting for source node"}},
        )
        captured = await node_jobs.dispatch(
            instance,
            "REPLICA_CAPTURE",
            {"name": instance.multipass_name, "recovery_point_id": str(point.id)},
            timeout=max(settings.node_job_timeout_seconds, 3600),
        )
        await database.recovery_points.update_one(
            {"_id": str(point.id)},
            {"$set": {"progress": 90, "progress_message": "Selecting replica laptops"}},
        )
        replicas: list[UUID] = []
        targets = await eligible_replica_nodes(instance, current_policy.replica_count)
        for index, node in enumerate(targets):
            try:
                await database.recovery_points.update_one(
                    {"_id": str(point.id)},
                    {"$set": {
                        "progress": 90 + int(9 * index / max(1, len(targets))),
                        "progress_message": f"Copying (0%) snapshot to {node.name}",
                    }},
                )
                await node_jobs.dispatch_node(
                    node,
                    "REPLICA_DOWNLOAD",
                    {
                        "recovery_point_id": str(point.id),
                        "sha256": str(captured.get("sha256") or ""),
                    },
                    timeout=max(settings.node_job_timeout_seconds, 3600),
                )
                replicas.append(node.id)
            except Exception as exc:
                log.warning(
                    "Recovery point %s replication to %s failed: %s", point.id, node.id, exc
                )
        now = datetime.now(UTC)
        warnings: list[str] = []
        pause_seconds = float(captured.get("pause_seconds") or 0)
        if pause_seconds > current_policy.pause_seconds_warning:
            warnings.append(
                f"VM I/O pause {pause_seconds:.1f}s exceeded the configured warning threshold"
            )
        if len(replicas) < current_policy.replica_count:
            warnings.append(
                f"Only {len(replicas)} of {current_policy.replica_count} requested node "
                "replicas completed"
            )
        changes = {
            "status": RecoveryPointStatus.AVAILABLE,
            "size_bytes": int(captured.get("size_bytes") or 0),
            "pause_seconds": pause_seconds,
            "sha256": str(captured.get("sha256") or "") or None,
            "canonical_present": True,
            "replica_node_ids": [str(value) for value in replicas],
            "completed_at": now,
            "updated_at": now,
            "error_message": "; ".join(warnings) or None,
            "progress": 100,
            "progress_message": "Snapshot completed",
        }
        await database.recovery_points.update_one({"_id": str(point.id)}, {"$set": changes})
        await enforce_retention(instance.id, current_policy.retention_points)
        return RecoveryPoint.model_validate({**point.model_dump(), **changes})
    except Exception as exc:
        await database.recovery_points.update_one(
            {"_id": str(point.id)},
            {
                "$set": {
                    "status": RecoveryPointStatus.FAILED,
                "error_message": str(exc)[:1000],
                "progress": 100,
                "progress_message": "Snapshot failed",
                    "updated_at": datetime.now(UTC),
                }
            },
        )
        raise


async def replicate_due() -> None:
    policies = from_documents(
        ReplicationPolicy,
        await database.replication_policies.find({"enabled": True}).to_list(None),
    )
    for current_policy in policies:
        instance = from_document(
            Instance,
            await database.instances.find_one(
                {
                    "_id": str(current_policy.instance_id),
                    "user_id": str(current_policy.user_id),
                    "deleted_at": None,
                    "status": {"$in": ["RUNNING", "STOPPED"]},
                    "node_id": {"$type": "string"},
                }
            ),
        )
        if not instance:
            continue
        cutoff = datetime.now(UTC) - timedelta(minutes=current_policy.interval_minutes)
        latest = await database.recovery_points.find_one(
            {"instance_id": str(instance.id)}, sort=[("created_at", -1)]
        )
        if latest and latest.get("created_at") and latest["created_at"] > cutoff:
            continue
        if await database.operations.find_one(
            {"instance_id": str(instance.id), "status": {"$in": ["PENDING", "RUNNING"]}}
        ):
            continue
        try:
            await capture(instance)
        except Exception as exc:
            log.warning("Scheduled replication failed for %s: %s", instance.id, exc)


async def replication_loop() -> None:
    while True:
        try:
            await replicate_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Replication cycle failed")
        await asyncio.sleep(settings.replication_poll_seconds)
