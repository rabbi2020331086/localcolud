import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import PlatformOperator
from app.core.config import settings
from app.core.security import opaque_token, token_hash
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import CloudNode, NodeStatus
from app.schemas import NodeCreate, NodeEnrollmentOut, NodeHeartbeat, NodeOut, NodeUpdate

router = APIRouter(prefix="/nodes", tags=["Cloud nodes"])
agent = APIRouter(prefix="/agent/nodes", tags=["Node agent"])
agent_files = APIRouter(prefix="/agent", tags=["Node agent"])


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
    if changes.get("approved") is True and not node.last_seen_at:
        raise HTTPException(409, "Connect the node agent before approving this host")
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
