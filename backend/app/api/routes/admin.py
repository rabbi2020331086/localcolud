import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import PlatformAdmin, PlatformOperator, has_platform_admin_access
from app.api.routes.nodes import observed_status
from app.core.config import settings
from app.core.security import hash_password
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.metrics import CPU_PERCENT_COMMAND
from app.infrastructure.models import (
    CloudNode,
    Instance,
    InstanceStatus,
    Operation,
    OperationStatus,
    OperationType,
    PlatformRole,
    RecoveryPoint,
    ReplicationPolicy,
    User,
    Volume,
)
from app.infrastructure.vm import ProviderUnavailable
from app.schemas import (
    AgentUpdateIn,
    HostCommandIn,
    RecoveryRequestIn,
    ReplicationPolicyIn,
    TeamMemberCreate,
    TeamMemberOut,
    TeamMemberUpdate,
)
from app.services import node_jobs, replication, scheduler
from app.services.operations import security_script

router = APIRouter(prefix="/admin", tags=["Platform administration"])


def recovery_output(
    point: RecoveryPoint,
    instances: dict[str, Instance],
    nodes: dict[str, CloudNode],
) -> dict[str, object]:
    instance = instances.get(str(point.instance_id))
    return {
        "id": str(point.id),
        "instance_id": str(point.instance_id),
        "instance": instance.display_name if instance else "Deleted instance",
        "source_node_id": str(point.source_node_id),
        "source_node": nodes.get(str(point.source_node_id)).name
        if nodes.get(str(point.source_node_id))
        else None,
        "status": point.status,
        "size_bytes": point.size_bytes,
        "pause_seconds": point.pause_seconds,
        "sha256": point.sha256,
        "canonical_present": point.canonical_present,
        "replica_node_ids": [str(value) for value in point.replica_node_ids],
        "replica_nodes": [
            nodes[str(value)].name for value in point.replica_node_ids if str(value) in nodes
        ],
        "automatic": point.automatic,
        "error_message": point.error_message
        or (
            f"{point.progress}% · {point.progress_message}"
            if point.status.value == "CREATING"
            else None
        ),
        "progress": point.progress,
        "progress_message": point.progress_message,
        "created_at": point.created_at,
        "completed_at": point.completed_at,
    }


@router.get("/replication")
async def replication_status(admin: PlatformOperator, database: Database) -> dict[str, object]:
    points = from_documents(
        RecoveryPoint,
        await database.recovery_points.find({}).sort("created_at", -1).limit(250).to_list(None),
    )
    instances = {
        str(item.id): item
        for item in from_documents(Instance, await database.instances.find({}).to_list(None))
    }
    nodes = {
        str(item.id): item
        for item in from_documents(CloudNode, await database.cloud_nodes.find({}).to_list(None))
    }
    policies = from_documents(
        ReplicationPolicy, await database.replication_policies.find({}).to_list(None)
    )
    displayed = policies[0].model_dump(mode="json") if policies else {
        "enabled": False,
        "interval_minutes": 30,
        "replica_count": 2,
        "retention_points": 3,
        "pause_seconds_warning": 30,
        "updated_at": datetime.now(UTC),
    }
    return {
        "policy": displayed,
        "policies": [item.model_dump(mode="json") for item in policies],
        "points": [recovery_output(point, instances, nodes) for point in points],
    }


@router.put("/replication/policy")
async def override_all_replication_policies(
    data: ReplicationPolicyIn, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    """Administrator bulk override; customers otherwise own each instance policy."""
    now = datetime.now(UTC)
    instances = from_documents(
        Instance,
        await database.instances.find(
            {
                "deleted_at": None,
                "$or": [{"system_role": "customer"}, {"system_role": {"$exists": False}}],
            }
        ).to_list(None),
    )
    for item in instances:
        await database.replication_policies.update_one(
            {"instance_id": str(item.id)},
            {
                "$set": {
                    **data.model_dump(),
                    "user_id": str(item.user_id),
                    "updated_by": str(admin.id),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": str(uuid4()),
                    "instance_id": str(item.id),
                    "created_at": now,
                },
            },
            upsert=True,
        )
    return {**data.model_dump(), "instances_overridden": len(instances), "updated_at": now}


@router.put("/instances/{instance_id}/replication-policy")
async def update_replication_policy(
    instance_id: UUID,
    data: ReplicationPolicyIn,
    admin: PlatformAdmin,
    database: Database,
) -> dict[str, object]:
    item = await admin_instance(database, instance_id)
    now = datetime.now(UTC)
    changes = {**data.model_dump(), "updated_by": str(admin.id), "updated_at": now}
    await database.replication_policies.update_one(
        {"instance_id": str(item.id)},
        {
            "$set": changes,
            "$setOnInsert": {
                "_id": str(uuid4()),
                "user_id": str(item.user_id),
                "instance_id": str(item.id),
                "created_at": now,
            },
        },
        upsert=True,
    )
    return {
        **changes,
        "user_id": str(item.user_id),
        "instance_id": str(item.id),
    }


@router.post("/instances/{instance_id}/recovery-points")
async def create_recovery_point(
    instance_id: UUID, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    item = await admin_instance(database, instance_id)
    try:
        point = await replication.capture(item, requested_by=admin.id)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    nodes = {
        str(node.id): node
        for node in from_documents(CloudNode, await database.cloud_nodes.find({}).to_list(None))
    }
    return recovery_output(point, {str(item.id): item}, nodes)


@router.delete("/recovery-points/{recovery_point_id}", status_code=204)
async def delete_recovery_point(
    recovery_point_id: UUID, admin: PlatformAdmin, database: Database
) -> None:
    point = from_document(
        RecoveryPoint,
        await database.recovery_points.find_one({"_id": str(recovery_point_id)}),
    )
    if not point:
        raise HTTPException(404, "Recovery point not found")
    if point.status.value == "CREATING":
        raise HTTPException(409, "A recovery point cannot be deleted while it is being created")
    await replication.remove_point(point)


@router.post("/recovery-points/{recovery_point_id}/recover")
async def recover_instance(
    recovery_point_id: UUID,
    data: RecoveryRequestIn,
    admin: PlatformAdmin,
    database: Database,
) -> dict[str, object]:
    point = from_document(
        RecoveryPoint,
        await database.recovery_points.find_one(
            {"_id": str(recovery_point_id), "status": "AVAILABLE"}
        ),
    )
    if not point:
        raise HTTPException(404, "Available recovery point not found")
    item = await admin_instance(database, point.instance_id)
    if await database.volumes.find_one({"attached_instance_id": str(item.id)}):
        raise HTTPException(409, "Detach persistent volumes before root-disk recovery")
    if await database.operations.find_one(
        {"instance_id": str(item.id), "status": {"$in": ["PENDING", "RUNNING"]}}
    ):
        raise HTTPException(409, "Another instance operation is active")
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    source = (
        from_document(CloudNode, await database.cloud_nodes.find_one({"_id": str(item.node_id)}))
        if item.node_id
        else None
    )
    if source and source.last_seen_at and source.last_seen_at >= deadline:
        raise HTTPException(409, "Source node is still online; fence or drain it before recovery")
    target = await scheduler.reserve_node(
        database,
        item.cpu_count,
        item.memory_mb,
        item.disk_gb,
        required_node_id=data.target_node_id,
    )
    if target.id == item.node_id:
        await database.cloud_nodes.update_one(
            {"_id": str(target.id)},
            {
                "$inc": {
                    "cpu_allocated": -item.cpu_count,
                    "memory_allocated_mb": -item.memory_mb,
                    "storage_allocated_gb": -item.disk_gb,
                }
            },
        )
        raise HTTPException(409, "Recovery target must differ from the failed source node")
    payload = {
        "name": item.multipass_name,
        "image": item.image,
        "cpu_count": item.cpu_count,
        "memory_mb": item.memory_mb,
        "disk_gb": item.disk_gb,
        "public_key": "",
        "user_data": "",
        "security_script": await security_script(item),
        "ami_id": str(item.ami_id) if item.ami_id else "",
        "recovery_point_id": str(point.id),
    }
    try:
        remote = await node_jobs.dispatch_node(target, "RECOVER", payload, timeout=3600)
    except Exception as exc:
        await database.cloud_nodes.update_one(
            {"_id": str(target.id)},
            {
                "$inc": {
                    "cpu_allocated": -item.cpu_count,
                    "memory_allocated_mb": -item.memory_mb,
                    "storage_allocated_gb": -item.disk_gb,
                }
            },
        )
        raise HTTPException(502, f"Recovery failed: {exc}") from exc
    if item.node_id and item.capacity_reserved:
        await database.cloud_nodes.update_one(
            {"_id": str(item.node_id)},
            [
                {
                    "$set": {
                        "cpu_allocated": {
                            "$max": [0, {"$subtract": ["$cpu_allocated", item.cpu_count]}]
                        },
                        "memory_allocated_mb": {
                            "$max": [0, {"$subtract": ["$memory_allocated_mb", item.memory_mb]}]
                        },
                        "storage_allocated_gb": {
                            "$max": [0, {"$subtract": ["$storage_allocated_gb", item.disk_gb]}]
                        },
                        "updated_at": datetime.now(UTC),
                    }
                }
            ],
        )
    now = datetime.now(UTC)
    await database.instances.update_one(
        {"_id": str(item.id)},
        {
            "$set": {
                "node_id": str(target.id),
                "availability_zone": target.availability_zone,
                "status": InstanceStatus.RUNNING,
                "ipv4_address": remote.get("ipv4_address"),
                "capacity_reserved": True,
                "last_error": f"Recovered by administrator {admin.id} from {point.completed_at}",
                "updated_at": now,
            }
        },
    )
    return {
        "recovered": True,
        "instance_id": str(item.id),
        "target_node_id": str(target.id),
        "ipv4_address": remote.get("ipv4_address"),
    }


@router.post("/nodes/{node_id}/update-agent")
async def update_node_agent(
    node_id: UUID, data: AgentUpdateIn, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    node = from_document(CloudNode, await database.cloud_nodes.find_one({"_id": str(node_id)}))
    if not node:
        raise HTTPException(404, "Cloud node not found")
    if observed_status(node).value != "ONLINE":
        raise HTTPException(409, "The node must be online to update its agent remotely")
    try:
        if node.agent_version and tuple(
            int(part) for part in node.agent_version.split(".") if part.isdigit()
        ) >= (0, 5, 4):
            result = await node_jobs.dispatch_node(
                node,
                "AGENT_UPDATE",
                {"control_plane_url": data.control_plane_url},
                timeout=180,
            )
        else:
            base = shlex.quote(data.control_plane_url)
            bootstrap = (
                "set -e; d=$(mktemp -d); trap 'rm -rf \"$d\"' EXIT; "
                f'curl -fsSL {base}/agent/agent.py -o "$d/agent.py"; '
                f"curl -fsSL {base}/agent/localec2-agent.service "
                '-o "$d/localec2-agent.service"; '
                'install -o root -g root -m 0755 "$d/agent.py" '
                "/opt/localec2-agent/agent.py; "
                'install -o root -g root -m 0644 "$d/localec2-agent.service" '
                "/etc/systemd/system/localec2-agent.service; "
                "systemd-run --unit=localec2-agent-bootstrap-update "
                "--on-active=10s /bin/sh -c "
                + shlex.quote("systemctl daemon-reload && systemctl restart localec2-agent")
            )
            await node_jobs.dispatch_node(
                node,
                "HOST_EXEC",
                {"command": bootstrap, "timeout": 120, "run_as_root": True},
                timeout=180,
            )
            result = {"updated": True, "version": "latest", "restart_in_seconds": 10}
    except ProviderUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        **result,
        "node_id": str(node.id),
        "requested_by": str(admin.id),
        "message": "Agent files updated; service restart has been scheduled",
    }


@router.post("/nodes/{node_id}/execute")
async def execute_host_command(
    node_id: UUID, data: HostCommandIn, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    """Run an audited command on a node host. Restricted to full administrators."""
    node = from_document(CloudNode, await database.cloud_nodes.find_one({"_id": str(node_id)}))
    if not node:
        raise HTTPException(404, "Cloud node not found")
    if observed_status(node).value != "ONLINE":
        raise HTTPException(409, "The compute node is offline or unavailable")
    try:
        result = await node_jobs.dispatch_node(
            node,
            "HOST_EXEC",
            {
                "command": data.command,
                "timeout": data.timeout_seconds,
                "run_as_root": data.run_as_root,
            },
            timeout=data.timeout_seconds + 30,
        )
    except ProviderUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "node_id": str(node.id),
        "command": data.command,
        "run_as_root": data.run_as_root,
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "exit_code": result.get("exit_code", 0),
    }


@router.post("/nodes/{node_id}/force-remove")
async def force_remove_node(
    node_id: UUID, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    """Remove a node and all of its control-plane dependencies without waiting for agents."""
    node = from_document(CloudNode, await database.cloud_nodes.find_one({"_id": str(node_id)}))
    if not node:
        raise HTTPException(404, "Cloud node not found")
    now = datetime.now(UTC)
    instances = from_documents(
        Instance,
        await database.instances.find({"node_id": str(node.id), "deleted_at": None}).to_list(None),
    )
    instance_ids = [str(instance.id) for instance in instances]
    if instance_ids:
        await database.operations.update_many(
            {
                "instance_id": {"$in": instance_ids},
                "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
            },
            {
                "$set": {
                    "status": OperationStatus.FAILED,
                    "progress": 100,
                    "message": "Cancelled by forced node removal",
                    "error_code": "ADMIN_FORCE_REMOVED_NODE",
                    "error_message": (
                        f"Node {node.name} was force removed by platform administrator {admin.id}"
                    ),
                    "finished_at": now,
                }
            },
        )
        await database.instances.update_many(
            {"_id": {"$in": instance_ids}},
            {
                "$set": {
                    "status": InstanceStatus.TERMINATING,
                    "capacity_reserved": False,
                    "deleted_at": now,
                    "ipv4_address": None,
                    "public_endpoint": None,
                    "public_endpoint_status": "DISABLED",
                    "last_error": f"Assigned node {node.name} was force removed",
                    "updated_at": now,
                }
            },
        )
    jobs = await database.node_jobs.count_documents(
        {"node_id": str(node.id), "status": {"$in": ["QUEUED", "LEASED"]}}
    )
    await database.node_jobs.update_many(
        {"node_id": str(node.id), "status": {"$in": ["QUEUED", "LEASED"]}},
        {
            "$set": {
                "status": "FAILED",
                "error_message": "Cancelled because the compute node was force removed",
                "lease_expires_at": None,
                "finished_at": now,
                "updated_at": now,
            }
        },
    )
    volumes = await database.volumes.count_documents({"node_id": str(node.id)})
    await database.volumes.update_many(
        {"node_id": str(node.id)},
        {
            "$set": {
                "attached_instance_id": None,
                "device_name": None,
                "mount_path": None,
                "node_id": None,
                "last_node_id": str(node.id),
                "updated_at": now,
            },
            "$unset": {"attachment_pending": ""},
        },
    )
    # Canonical AMI packages remain launchable. Only their former capture-node
    # association is cleared.
    await database.amis.update_many(
        {"node_id": str(node.id)}, {"$set": {"node_id": None, "updated_at": now}}
    )
    await database.cloud_nodes.delete_one({"_id": str(node.id)})
    return {
        "removed": True,
        "node": node.name,
        "instances_removed": len(instances),
        "volumes_detached": volumes,
        "jobs_cancelled": jobs,
        "warning": (
            "The physical host was not contacted. Running VMs and unsynchronized attached-volume "
            "data may remain on that host; only canonical detached copies are recoverable."
        ),
    }


def member_output(user: User) -> TeamMemberOut:
    roles = user.platform_roles
    if user.is_platform_admin and PlatformRole.ADMINISTRATOR not in roles:
        roles = [*roles, PlatformRole.ADMINISTRATOR]
    return TeamMemberOut.model_validate({**user.model_dump(), "platform_roles": roles})


@router.get("/overview")
async def overview(admin: PlatformOperator, database: Database) -> dict[str, object]:
    nodes = from_documents(CloudNode, await database.cloud_nodes.find({}).to_list(None))
    eligible = [
        node
        for node in nodes
        if observed_status(node).value == "ONLINE"
        and {"jobs", "vagrant", "libvirt", "kvm"}.issubset(node.capabilities)
    ]
    instances = from_documents(
        Instance, await database.instances.find({"deleted_at": None}).to_list(None)
    )
    operations = from_documents(Operation, await database.operations.find({}).to_list(None))
    return {
        "users": await database.users.count_documents({"is_active": True}),
        "nodes": len(nodes),
        "online_nodes": sum(observed_status(node).value == "ONLINE" for node in nodes),
        "pending_nodes": sum(not node.approved for node in nodes),
        "instances": len(instances),
        "running_instances": sum(item.status.value == "RUNNING" for item in instances),
        "failed_instances": sum(item.status.value in {"ERROR", "UNKNOWN"} for item in instances),
        "active_operations": sum(
            item.status.value in {"PENDING", "RUNNING"} for item in operations
        ),
        "failed_operations": sum(item.status.value == "FAILED" for item in operations),
        "cpu_total": sum(max(0, node.cpu_total - node.cpu_reserved) for node in eligible),
        "cpu_allocated": sum(node.cpu_allocated for node in eligible),
        "memory_total_mb": sum(
            max(0, node.memory_total_mb - node.memory_reserved_mb) for node in eligible
        ),
        "memory_allocated_mb": sum(node.memory_allocated_mb for node in eligible),
        "storage_total_gb": sum(
            max(0, node.storage_total_gb - node.storage_reserved_gb) for node in eligible
        ),
        "storage_allocated_gb": sum(node.storage_allocated_gb for node in eligible),
        "zones": sorted({node.availability_zone for node in eligible}),
    }


@router.get("/instances")
async def all_instances(admin: PlatformOperator, database: Database) -> list[dict[str, object]]:
    instances = from_documents(
        Instance,
        await database.instances.find({"deleted_at": None}).sort("created_at", -1).to_list(None),
    )
    user_ids = {str(item.user_id) for item in instances}
    node_ids = {str(item.node_id) for item in instances if item.node_id}
    users = {
        str(item.id): item
        for item in from_documents(
            User, await database.users.find({"_id": {"$in": list(user_ids)}}).to_list(None)
        )
    }
    nodes = {
        str(item.id): item
        for item in from_documents(
            CloudNode,
            await database.cloud_nodes.find({"_id": {"$in": list(node_ids)}}).to_list(None),
        )
    }
    result: list[dict[str, object]] = []
    for item in instances:
        owner = users.get(str(item.user_id))
        node = nodes.get(str(item.node_id)) if item.node_id else None
        result.append(
            {
                "id": str(item.id),
                "name": item.display_name,
                "status": item.status,
                "owner": owner.email if owner else "Unknown",
                "node": node.name if node else None,
                "node_id": str(node.id) if node else None,
                "zone": node.availability_zone if node else None,
                "provider": item.provider_name,
                "ipv4_address": item.ipv4_address,
                "instance_type": item.instance_type,
                "last_error": item.last_error,
                "autoscaling_group_id": (
                    str(item.autoscaling_group_id) if item.autoscaling_group_id else None
                ),
                "cpu_count": item.cpu_count,
                "memory_mb": item.memory_mb,
                "disk_gb": item.disk_gb,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
        )
    return result


async def admin_instance(database: Database, instance_id: UUID) -> Instance:
    row = from_document(
        Instance,
        await database.instances.find_one({"_id": str(instance_id), "deleted_at": None}),
    )
    if not row:
        raise HTTPException(404, "Instance not found")
    return row


@router.post("/instances/{instance_id}/{action}")
async def control_instance(
    instance_id: UUID,
    action: str,
    admin: PlatformAdmin,
    database: Database,
) -> dict[str, object]:
    item = await admin_instance(database, instance_id)
    active = await database.operations.find_one(
        {
            "instance_id": str(item.id),
            "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
        }
    )
    if active:
        raise HTTPException(409, "Another operation is already active for this instance")
    allowed: dict[str, tuple[set[InstanceStatus], OperationType]] = {
        "start": ({InstanceStatus.STOPPED, InstanceStatus.HIBERNATED}, OperationType.START),
        "stop": ({InstanceStatus.RUNNING}, OperationType.STOP),
        "hibernate": ({InstanceStatus.RUNNING}, OperationType.HIBERNATE),
        "terminate": (
            {
                InstanceStatus.RUNNING,
                InstanceStatus.STOPPED,
                InstanceStatus.HIBERNATED,
                InstanceStatus.ERROR,
                InstanceStatus.UNKNOWN,
            },
            OperationType.TERMINATE,
        ),
    }
    definition = allowed.get(action)
    if not definition:
        raise HTTPException(404, "Unsupported instance action")
    statuses, operation_type = definition
    if item.status not in statuses:
        raise HTTPException(409, f"Instance cannot {action} from {item.status}")
    scheduler.require_distributed(item)
    operation = Operation(
        user_id=item.user_id,
        instance_id=item.id,
        operation_type=operation_type,
        request_payload={"requested_by_platform_admin": str(admin.id)},
    )
    await database.operations.insert_one(to_document(operation))
    return {"operation_id": str(operation.id), "status": operation.status}


@router.post("/instances/{instance_id}/force/remove")
async def force_remove_instance(
    instance_id: UUID, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    """Cancel control-plane work, best-effort destroy the VM, and remove its reservation."""
    item = await admin_instance(database, instance_id)
    now = datetime.now(UTC)
    await database.operations.update_many(
        {
            "instance_id": str(item.id),
            "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
        },
        {
            "$set": {
                "status": OperationStatus.FAILED,
                "progress": 100,
                "message": "Cancelled by platform administrator",
                "error_code": "ADMIN_FORCE_REMOVED",
                "error_message": f"Force removed by platform administrator {admin.id}",
                "finished_at": now,
            }
        },
    )
    await database.node_jobs.update_many(
        {"instance_id": str(item.id), "status": {"$in": ["QUEUED", "LEASED"]}},
        {
            "$set": {
                "status": "FAILED",
                "error_message": "Cancelled by platform administrator force removal",
                "lease_expires_at": None,
                "finished_at": now,
                "updated_at": now,
            }
        },
    )
    cleanup_error = None
    if item.node_id:
        node = from_document(
            CloudNode, await database.cloud_nodes.find_one({"_id": str(item.node_id)})
        )
        if node and observed_status(node).value == "ONLINE":
            try:
                await node_jobs.dispatch_node(
                    node, "DELETE", {"name": item.multipass_name}, timeout=600
                )
            except Exception as exc:
                cleanup_error = str(exc)[:1000]
    await database.volumes.update_many(
        {"attached_instance_id": str(item.id)},
        {
            "$set": {
                "attached_instance_id": None,
                "device_name": None,
                "mount_path": None,
                "node_id": None,
                "last_node_id": str(item.node_id) if item.node_id else None,
                "updated_at": now,
            }
        },
    )
    await scheduler.release_instance_capacity(database, item)
    await database.instances.update_one(
        {"_id": str(item.id)},
        {
            "$set": {
                "status": InstanceStatus.TERMINATING,
                "deleted_at": now,
                "ipv4_address": None,
                "public_endpoint": None,
                "public_endpoint_status": "DISABLED",
                "last_error": cleanup_error,
                "updated_at": now,
            }
        },
    )
    return {"removed": True, "host_cleanup_error": cleanup_error}


@router.get("/instances/{instance_id}/diagnostics")
async def instance_diagnostics(
    instance_id: UUID,
    admin: PlatformOperator,
    database: Database,
) -> dict[str, object]:
    item = await admin_instance(database, instance_id)
    scheduler.require_distributed(item)
    inspect = await node_jobs.dispatch(item, "INSPECT", {"name": item.multipass_name}, timeout=90)
    metrics = None
    if str(inspect.get("state", "")).lower() == "running":
        output = await node_jobs.remote_exec(
            item,
            CPU_PERCENT_COMMAND + "; echo; free -m | awk 'NR==2{printf \"%.1f\",$3*100/$2}'; "
            "echo; df -P / | awk 'NR==2{print $5}'",
            timeout=30,
        )
        values = output.strip().splitlines()[-3:]
        metrics = {
            "cpu_percent": float(values[0]),
            "memory_percent": float(values[1]),
            "root_disk_percent": values[2],
        }
    jobs = (
        await database.node_jobs.find({"instance_id": str(item.id)})
        .sort("created_at", -1)
        .limit(10)
        .to_list(None)
    )
    return {
        "instance_id": str(item.id),
        "inspect": inspect,
        "metrics": metrics,
        "recent_jobs": [
            {
                "id": str(job["_id"]),
                "type": job["job_type"],
                "status": job["status"],
                "error": job.get("error_message"),
                "created_at": job["created_at"],
            }
            for job in jobs
        ],
    }


@router.get("/volumes")
async def all_volumes(admin: PlatformOperator, database: Database) -> list[dict[str, object]]:
    volumes = from_documents(
        Volume, await database.volumes.find({}).sort("created_at", -1).to_list(None)
    )
    users = {
        str(item.id): item.email
        for item in from_documents(User, await database.users.find({}).to_list(None))
    }
    nodes = {
        str(item.id): item.name
        for item in from_documents(CloudNode, await database.cloud_nodes.find({}).to_list(None))
    }
    instances = {
        str(item.id): item.display_name
        for item in from_documents(
            Instance,
            await database.instances.find(
                {
                    "_id": {
                        "$in": [
                            str(volume.attached_instance_id)
                            for volume in volumes
                            if volume.attached_instance_id
                        ]
                    }
                }
            ).to_list(None),
        )
    }
    return [
        {
            "id": str(volume.id),
            "name": volume.name,
            "owner": users.get(str(volume.user_id), "Unknown"),
            "size_gb": volume.size_gb,
            "availability_zone": volume.availability_zone,
            "storage_backend": volume.storage_backend,
            "origin_node": nodes.get(str(volume.origin_node_id)) if volume.origin_node_id else None,
            "current_node": nodes.get(str(volume.node_id)) if volume.node_id else None,
            "last_node": nodes.get(str(volume.last_node_id)) if volume.last_node_id else None,
            "attached_instance": (
                instances.get(str(volume.attached_instance_id))
                if volume.attached_instance_id
                else None
            ),
            "attached_instance_id": (
                str(volume.attached_instance_id) if volume.attached_instance_id else None
            ),
            "device_name": volume.device_name,
            "mount_path": volume.mount_path,
            "canonical_present": (
                Path(settings.volume_storage_root).expanduser().resolve() / f"{volume.id}.qcow2"
            ).is_file(),
            "created_at": volume.created_at,
            "updated_at": volume.updated_at,
        }
        for volume in volumes
    ]


@router.post("/volumes/{volume_id}/detach")
async def admin_detach_volume(
    volume_id: UUID,
    admin: PlatformAdmin,
    database: Database,
) -> dict[str, object]:
    volume = from_document(Volume, await database.volumes.find_one({"_id": str(volume_id)}))
    if not volume:
        raise HTTPException(404, "Volume not found")
    if not volume.attached_instance_id:
        return {"status": "AVAILABLE"}
    item = await admin_instance(database, volume.attached_instance_id)
    if item.status != InstanceStatus.RUNNING:
        raise HTTPException(409, "Start the attached instance before detaching its volume")
    await node_jobs.dispatch(
        item,
        "VOLUME_DETACH",
        {
            "name": item.multipass_name,
            "mount_path": volume.mount_path or "/mnt/data",
            "device_name": volume.device_name or "vdb",
            "volume_id": str(volume.id),
        },
        timeout=600,
    )
    await database.volumes.update_one(
        {"_id": str(volume.id)},
        {
            "$set": {
                "attached_instance_id": None,
                "device_name": None,
                "mount_path": None,
                "node_id": None,
                "last_node_id": str(item.node_id),
                "updated_at": datetime.now(UTC),
            }
        },
    )
    return {"status": "AVAILABLE"}


@router.delete("/volumes/{volume_id}", status_code=204)
async def admin_delete_volume(
    volume_id: UUID,
    admin: PlatformAdmin,
    database: Database,
) -> None:
    volume = from_document(Volume, await database.volumes.find_one({"_id": str(volume_id)}))
    if not volume:
        raise HTTPException(404, "Volume not found")
    if volume.attached_instance_id:
        raise HTTPException(409, "Detach the volume before deleting it")
    canonical = Path(settings.volume_storage_root).expanduser().resolve() / f"{volume.id}.qcow2"
    canonical.unlink(missing_ok=True)
    await database.volumes.delete_one({"_id": str(volume.id)})


@router.get("/node-contributions")
async def node_contributions(
    admin: PlatformOperator, database: Database
) -> list[dict[str, object]]:
    nodes = from_documents(CloudNode, await database.cloud_nodes.find({}).to_list(None))
    instances = await database.instances.find({"deleted_at": None}).to_list(None)
    volumes = await database.volumes.find({}).to_list(None)
    return [
        {
            "node_id": str(node.id),
            "node": node.name,
            "zone": node.availability_zone,
            "status": observed_status(node),
            "cpu_contributed": max(0, node.cpu_total - node.cpu_reserved),
            "cpu_allocated": node.cpu_allocated,
            "memory_contributed_mb": max(0, node.memory_total_mb - node.memory_reserved_mb),
            "memory_allocated_mb": node.memory_allocated_mb,
            "storage_contributed_gb": max(0, node.storage_total_gb - node.storage_reserved_gb),
            "storage_allocated_gb": node.storage_allocated_gb,
            "instances": sum(row.get("node_id") == str(node.id) for row in instances),
            "attached_volumes": sum(row.get("node_id") == str(node.id) for row in volumes),
            "attached_volume_gb": sum(
                int(row.get("size_gb", 0)) for row in volumes if row.get("node_id") == str(node.id)
            ),
            "originated_volumes": sum(row.get("origin_node_id") == str(node.id) for row in volumes),
        }
        for node in nodes
    ]


@router.get("/operations")
async def all_operations(admin: PlatformOperator, database: Database) -> list[dict[str, object]]:
    operations = from_documents(
        Operation,
        await database.operations.find({}).sort("created_at", -1).limit(250).to_list(None),
    )
    users = {
        str(item.id): item.email
        for item in from_documents(User, await database.users.find({}).to_list(None))
    }
    return [
        {
            "id": str(item.id),
            "instance_id": str(item.instance_id) if item.instance_id else None,
            "owner": users.get(str(item.user_id), "Unknown"),
            "type": item.operation_type,
            "status": item.status,
            "progress": item.progress,
            "message": item.message,
            "error_message": item.error_message,
            "created_at": item.created_at,
            "started_at": item.started_at,
            "finished_at": item.finished_at,
        }
        for item in operations
    ]


@router.get("/team", response_model=list[TeamMemberOut])
async def list_members(admin: PlatformAdmin, database: Database) -> list[TeamMemberOut]:
    identity_filter: dict[str, object] = {
        "$or": [
            {"is_platform_admin": True},
            {"platform_roles": {"$exists": True, "$ne": []}},
        ]
    }
    if settings.platform_admin_email.strip():
        identity_filter["$or"].append(  # type: ignore[union-attr]
            {"email": settings.platform_admin_email.strip().lower()}
        )
    users = from_documents(
        User,
        await database.users.find(identity_filter).sort("created_at", 1).to_list(None),
    )
    return [member_output(user) for user in users]


@router.post("/team", response_model=TeamMemberOut, status_code=201)
async def create_member(
    data: TeamMemberCreate, admin: PlatformAdmin, database: Database
) -> TeamMemberOut:
    user = User(
        email=data.email.lower(),
        display_name=data.display_name.strip(),
        password_hash=hash_password(data.temporary_password),
        platform_roles=data.platform_roles,
        is_platform_admin=PlatformRole.ADMINISTRATOR in data.platform_roles,
        must_change_password=True,
    )
    try:
        await database.users.insert_one(to_document(user))
    except DuplicateKeyError:
        raise HTTPException(409, "A team member with this email already exists") from None
    return member_output(user)


@router.patch("/team/{user_id}", response_model=TeamMemberOut)
async def update_member(
    user_id: UUID, data: TeamMemberUpdate, admin: PlatformAdmin, database: Database
) -> TeamMemberOut:
    user = from_document(User, await database.users.find_one({"_id": str(user_id)}))
    if not user or not has_platform_admin_access(user) and not user.platform_roles:
        raise HTTPException(404, "Team member not found")
    if user.id == admin.id and (
        PlatformRole.ADMINISTRATOR not in data.platform_roles or not data.is_active
    ):
        raise HTTPException(409, "You cannot remove your own administrator access")
    changes = {
        "platform_roles": [role.value for role in data.platform_roles],
        "is_platform_admin": PlatformRole.ADMINISTRATOR in data.platform_roles,
        "is_active": data.is_active,
        "can_view_agent_code": data.can_view_agent_code or data.can_edit_agent_code,
        "can_edit_agent_code": data.can_edit_agent_code,
    }
    await database.users.update_one({"_id": str(user.id)}, {"$set": changes})
    return member_output(User.model_validate({**user.model_dump(), **changes}))
