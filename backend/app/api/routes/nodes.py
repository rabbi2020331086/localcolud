import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import PlatformOperator
from app.core.config import settings
from app.core.security import opaque_token, token_hash
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import CloudNode, NodeJob, NodeJobStatus, NodeStatus
from app.schemas import (
    AgentJobOut,
    AgentJobResult,
    NodeCreate,
    NodeEnrollmentOut,
    NodeHeartbeat,
    NodeOut,
    NodeUpdate,
)
from app.services.node_jobs import reap_expired_leases

router = APIRouter(prefix="/nodes", tags=["Cloud nodes"])
agent = APIRouter(prefix="/agent/nodes", tags=["Node agent"])
agent_files = APIRouter(prefix="/agent", tags=["Node agent"])
REQUIRED_COMPUTE_CAPABILITIES = {"jobs", "vagrant", "libvirt", "kvm"}


def observed_status(node: CloudNode, now: datetime | None = None) -> NodeStatus:
    if not node.approved:
        return NodeStatus.PENDING
    if not node.schedulable:
        return NodeStatus.DRAINING
    if not node.last_seen_at:
        return NodeStatus.OFFLINE
    deadline = (now or datetime.now(UTC)) - timedelta(seconds=settings.node_offline_seconds)
    return NodeStatus.ONLINE if node.last_seen_at >= deadline else NodeStatus.OFFLINE


def output(node: CloudNode) -> NodeOut:
    return NodeOut.model_validate({**node.model_dump(), "status": observed_status(node)})


async def existing(database: Database, node_id: UUID) -> CloudNode:
    node = from_document(
        CloudNode,
        await database.cloud_nodes.find_one({"_id": str(node_id)}),
    )
    if not node:
        raise HTTPException(404, "Cloud node not found")
    return node


@router.get("", response_model=list[NodeOut])
async def list_nodes(user: PlatformOperator, database: Database) -> list[NodeOut]:
    nodes = from_documents(
        CloudNode,
        await database.cloud_nodes.find({}).sort("name", 1).to_list(None),
    )
    return [output(node) for node in nodes]


@router.post("", response_model=NodeEnrollmentOut, status_code=201)
async def enroll(data: NodeCreate, user: PlatformOperator, database: Database) -> NodeEnrollmentOut:
    secret = opaque_token()
    node = CloudNode(
        user_id=user.id,
        name=data.name,
        availability_zone=data.availability_zone,
        provider_name=data.provider_name,
        labels=data.labels,
        agent_token_hash=token_hash(secret),
    )
    try:
        await database.cloud_nodes.insert_one(to_document(node))
    except DuplicateKeyError:
        raise HTTPException(409, "A cloud node with this name already exists") from None
    return NodeEnrollmentOut.model_validate(
        {**output(node).model_dump(), "enrollment_token": secret}
    )


@router.post("/{node_id}/rotate-token", response_model=NodeEnrollmentOut)
async def rotate_token(
    node_id: UUID, user: PlatformOperator, database: Database
) -> NodeEnrollmentOut:
    node = await existing(database, node_id)
    secret = opaque_token()
    now = datetime.now(UTC)
    changes = {"agent_token_hash": token_hash(secret), "updated_at": now}
    await database.cloud_nodes.update_one({"_id": str(node.id)}, {"$set": changes})
    updated = CloudNode.model_validate({**node.model_dump(), **changes})
    return NodeEnrollmentOut.model_validate(
        {**output(updated).model_dump(), "enrollment_token": secret}
    )


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: UUID, data: NodeUpdate, user: PlatformOperator, database: Database
) -> NodeOut:
    node = await existing(database, node_id)
    changes = data.model_dump(exclude_none=True)
    if not changes:
        return output(node)
    if (
        "availability_zone" in changes
        and changes["availability_zone"] != node.availability_zone
        and await database.instances.find_one(
            {"node_id": str(node.id), "deleted_at": None, "capacity_reserved": True}
        )
    ):
        raise HTTPException(409, "Drain and terminate workloads before moving a node to a new zone")
    if changes.get("approved") is True:
        if not node.last_seen_at:
            raise HTTPException(409, "Connect the node agent before approving this host")
        missing = REQUIRED_COMPUTE_CAPABILITIES.difference(node.capabilities)
        if node.provider_name.value != "vagrant-libvirt" or missing:
            detail = ", ".join(sorted(missing)) or "vagrant-libvirt provider"
            raise HTTPException(
                409,
                f"Node cannot execute distributed VM jobs; missing capability: {detail}. "
                "Run Setup / reconnect to install the current agent.",
            )
    if changes.get("schedulable") is True and node.approved:
        missing = REQUIRED_COMPUTE_CAPABILITIES.difference(node.capabilities)
        if missing:
            raise HTTPException(409, "Upgrade the node agent before enabling scheduling")
    limits = {
        "cpu_reserved": (node.cpu_total, node.cpu_allocated),
        "memory_reserved_mb": (node.memory_total_mb, node.memory_allocated_mb),
        "storage_reserved_gb": (node.storage_total_gb, node.storage_allocated_gb),
    }
    for field, (total, allocated) in limits.items():
        if field in changes and total and int(changes[field]) + allocated >= total:
            raise HTTPException(
                422,
                f"{field} plus allocated capacity must be lower than the reported total",
            )
    changes["updated_at"] = datetime.now(UTC)
    if changes.get("approved") is False:
        changes["status"] = NodeStatus.PENDING
    elif changes.get("schedulable") is False:
        changes["status"] = NodeStatus.DRAINING
    await database.cloud_nodes.update_one({"_id": str(node.id)}, {"$set": changes})
    return output(CloudNode.model_validate({**node.model_dump(), **changes}))


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: UUID, user: PlatformOperator, database: Database) -> None:
    node = await existing(database, node_id)
    if await database.instances.find_one({"node_id": str(node.id), "deleted_at": None}):
        raise HTTPException(409, "Drain or terminate instances before removing this node")
    await database.cloud_nodes.delete_one({"_id": str(node.id)})


async def authenticated_node(
    database: Database, node_id: UUID, authorization: Annotated[str | None, Header()] = None
) -> CloudNode:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Node enrollment token is required")
    supplied_hash = token_hash(authorization.removeprefix("Bearer ").strip())
    node = from_document(CloudNode, await database.cloud_nodes.find_one({"_id": str(node_id)}))
    if not node or not hmac.compare_digest(node.agent_token_hash, supplied_hash):
        raise HTTPException(401, "Invalid node enrollment token")
    return node


@agent.post("/{node_id}/heartbeat", response_model=NodeOut)
async def heartbeat(
    node_id: UUID,
    data: NodeHeartbeat,
    database: Database,
    authorization: Annotated[str | None, Header()] = None,
) -> NodeOut:
    node = await authenticated_node(database, node_id, authorization)
    now = datetime.now(UTC)
    changes = {
        **data.model_dump(mode="json"),
        "last_seen_at": now,
        "updated_at": now,
        "status": NodeStatus.ONLINE if node.approved and node.schedulable else node.status,
    }
    await database.cloud_nodes.update_one({"_id": str(node.id)}, {"$set": changes})
    return output(CloudNode.model_validate({**node.model_dump(), **changes}))


@agent.post("/{node_id}/jobs/claim", response_model=AgentJobOut, responses={204: {}})
async def claim_job(
    node_id: UUID,
    database: Database,
    authorization: Annotated[str | None, Header()] = None,
) -> AgentJobOut | Response:
    node = await authenticated_node(database, node_id, authorization)
    if not node.approved:
        raise HTTPException(403, "Node must be approved before it can claim jobs")
    await reap_expired_leases()
    now = datetime.now(UTC)
    document = await database.node_jobs.find_one_and_update(
        {"node_id": str(node.id), "status": NodeJobStatus.QUEUED},
        {
            "$set": {
                "status": NodeJobStatus.LEASED,
                "started_at": now,
                "lease_expires_at": now + timedelta(seconds=settings.node_job_lease_seconds),
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    job = from_document(NodeJob, document)
    if not job:
        return Response(status_code=204)
    return AgentJobOut.model_validate(job)


@agent.post("/{node_id}/jobs/{job_id}/renew", status_code=204)
async def renew_job(
    node_id: UUID,
    job_id: UUID,
    database: Database,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    node = await authenticated_node(database, node_id, authorization)
    now = datetime.now(UTC)
    result = await database.node_jobs.update_one(
        {
            "_id": str(job_id),
            "node_id": str(node.id),
            "status": NodeJobStatus.LEASED,
        },
        {
            "$set": {
                "lease_expires_at": now + timedelta(seconds=settings.node_job_lease_seconds),
                "updated_at": now,
            }
        },
    )
    if not result.modified_count:
        raise HTTPException(409, "Node job lease is no longer active")


@agent.post("/{node_id}/jobs/{job_id}/complete", status_code=204)
async def complete_job(
    node_id: UUID,
    job_id: UUID,
    data: AgentJobResult,
    database: Database,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    node = await authenticated_node(database, node_id, authorization)
    now = datetime.now(UTC)
    status = NodeJobStatus.SUCCEEDED if data.succeeded else NodeJobStatus.FAILED
    result = await database.node_jobs.update_one(
        {
            "_id": str(job_id),
            "node_id": str(node.id),
            "status": NodeJobStatus.LEASED,
        },
        {
            "$set": {
                "status": status,
                "result_payload": data.result_payload,
                "error_message": None if data.succeeded else data.error_message,
                "lease_expires_at": None,
                "finished_at": now,
                "updated_at": now,
            }
        },
    )
    if result.modified_count:
        return
    existing_job = from_document(
        NodeJob,
        await database.node_jobs.find_one({"_id": str(job_id), "node_id": str(node.id)}),
    )
    if existing_job and existing_job.status in (
        NodeJobStatus.SUCCEEDED,
        NodeJobStatus.FAILED,
    ):
        return
    raise HTTPException(409, "Node job is not leased to this agent")


@agent_files.get("/install.sh", response_class=FileResponse, include_in_schema=False)
async def ubuntu_installer() -> FileResponse:
    path = Path(__file__).resolve().parents[4] / "deploy" / "install-node-ubuntu.sh"
    if not path.is_file():
        raise HTTPException(404, "Node installer is not available")
    return FileResponse(path, media_type="text/x-shellscript", filename="install-node-ubuntu.sh")


@agent_files.get("/agent.py", response_class=FileResponse, include_in_schema=False)
async def agent_program() -> FileResponse:
    path = Path(__file__).resolve().parents[2] / "agent.py"
    if not path.is_file():
        raise HTTPException(404, "Node agent is not available")
    return FileResponse(path, media_type="text/x-python", filename="agent.py")


@agent_files.get("/localec2-agent.service", response_class=FileResponse, include_in_schema=False)
async def agent_service() -> FileResponse:
    path = Path(__file__).resolve().parents[4] / "deploy" / "localec2-agent.service"
    if not path.is_file():
        raise HTTPException(404, "Node agent service definition is not available")
    return FileResponse(path, media_type="text/plain", filename="localec2-agent.service")
