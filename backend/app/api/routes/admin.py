from uuid import UUID

from fastapi import APIRouter, HTTPException
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import PlatformAdmin, PlatformOperator
from app.api.routes.nodes import observed_status
from app.core.security import hash_password
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import CloudNode, Instance, Operation, PlatformRole, User
from app.schemas import TeamMemberCreate, TeamMemberOut, TeamMemberUpdate

router = APIRouter(prefix="/admin", tags=["Platform administration"])


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
                "zone": node.availability_zone if node else None,
                "provider": item.provider_name,
                "ipv4_address": item.ipv4_address,
                "cpu_count": item.cpu_count,
                "memory_mb": item.memory_mb,
                "disk_gb": item.disk_gb,
                "created_at": item.created_at,
            }
        )
    return result


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
    users = from_documents(User, await database.users.find({}).sort("created_at", 1).to_list(None))
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
    if not user:
        raise HTTPException(404, "Team member not found")
    if user.id == admin.id and (
        PlatformRole.ADMINISTRATOR not in data.platform_roles or not data.is_active
    ):
        raise HTTPException(409, "You cannot remove your own administrator access")
    changes = {
        "platform_roles": [role.value for role in data.platform_roles],
        "is_platform_admin": PlatformRole.ADMINISTRATOR in data.platform_roles,
        "is_active": data.is_active,
    }
    await database.users.update_one({"_id": str(user.id)}, {"$set": changes})
    return member_output(User.model_validate({**user.model_dump(), **changes}))
