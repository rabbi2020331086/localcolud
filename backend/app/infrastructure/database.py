from collections.abc import AsyncIterator, Iterable
from enum import Enum
from typing import Annotated, Any, TypeVar, cast
from uuid import UUID

from fastapi import Depends
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel
from pymongo.server_api import ServerApi

from app.core.config import settings
from app.core.security import hash_password
from app.infrastructure.models import PlatformRole, User

Document = dict[str, Any]
ModelType = TypeVar("ModelType", bound=BaseModel)
client: AsyncMongoClient[Document] = AsyncMongoClient(
    settings.mongodb_uri,
    server_api=ServerApi("1"),
    tz_aware=True,
    serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
)
database = client[settings.mongodb_database]


async def get_database() -> AsyncIterator[Any]:
    yield database


Database = Annotated[Any, Depends(get_database)]


def bson_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: bson_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [bson_value(item) for item in value]
    return value


def to_document(model: Any) -> Document:
    data = cast(Document, bson_value(model.model_dump(mode="python")))
    data["_id"] = data.pop("id")
    return data


def from_document(model_type: type[ModelType], document: Document | None) -> ModelType | None:
    if document is None:
        return None
    data = dict(document)
    data["id"] = data.pop("_id")
    return model_type.model_validate(data)


def from_documents(model_type: type[ModelType], documents: Iterable[Document]) -> list[ModelType]:
    result: list[ModelType] = []
    for document in documents:
        model = from_document(model_type, document)
        if model:
            result.append(model)
    return result


async def initialize_database() -> None:
    await database.command("ping")
    # Older installations did not persist placement or host-reservation fields.
    # Set explicit legacy/safe values before models are read so an upgrade can
    # never reinterpret a control-plane Multipass VM as distributed capacity.
    await database.instances.update_many(
        {"availability_zone": {"$exists": False}},
        {"$set": {"availability_zone": "legacy-unassigned"}},
    )
    await database.instances.update_many(
        {"capacity_reserved": {"$exists": False}},
        {"$set": {"capacity_reserved": False}},
    )
    await database.instances.update_many(
        {"provider_name": {"$exists": False}},
        {"$set": {"provider_name": "multipass"}},
    )
    await database.cloud_nodes.update_many(
        {},
        [
            {
                "$set": {
                    "cpu_reserved": {"$ifNull": ["$cpu_reserved", 1]},
                    "memory_reserved_mb": {"$ifNull": ["$memory_reserved_mb", 1024]},
                    "storage_reserved_gb": {"$ifNull": ["$storage_reserved_gb", 10]},
                }
            }
        ],
    )
    # Allocation counters are derived state. Rebuild them from instance
    # reservations on startup to repair interrupted placement/release writes.
    await database.cloud_nodes.update_many(
        {},
        {
            "$set": {
                "cpu_allocated": 0,
                "memory_allocated_mb": 0,
                "storage_allocated_gb": 0,
            },
            "$unset": {"placement_pending": ""},
        },
    )
    allocations = await database.instances.aggregate(
        [
            {
                "$match": {
                    "deleted_at": None,
                    "capacity_reserved": True,
                    "node_id": {"$type": "string"},
                }
            },
            {
                "$group": {
                    "_id": "$node_id",
                    "cpu": {"$sum": "$cpu_count"},
                    "memory": {"$sum": "$memory_mb"},
                    "storage": {"$sum": "$disk_gb"},
                }
            },
        ]
    )
    async for allocation in allocations:
        await database.cloud_nodes.update_one(
            {"_id": allocation["_id"]},
            {
                "$set": {
                    "cpu_allocated": allocation["cpu"],
                    "memory_allocated_mb": allocation["memory"],
                    "storage_allocated_gb": allocation["storage"],
                }
            },
        )
    await database.users.create_indexes([IndexModel("email", unique=True, name="uq_users_email")])
    if settings.app_env != "production" and settings.development_default_admin:
        default_email = "admin@localec2.local"
        if not await database.users.find_one({"email": default_email}):
            await database.users.insert_one(
                to_document(
                    User(
                        email=default_email,
                        display_name="Platform Administrator",
                        password_hash=hash_password("admin"),
                        is_platform_admin=True,
                        platform_roles=[PlatformRole.ADMINISTRATOR],
                        must_change_password=True,
                    )
                )
            )
    await database.refresh_tokens.create_indexes(
        [
            IndexModel("jti", unique=True),
            IndexModel("token_hash", unique=True),
            IndexModel("expires_at", expireAfterSeconds=0),
        ]
    )
    await database.instances.create_indexes(
        [
            IndexModel("multipass_name", unique=True),
            IndexModel([("user_id", ASCENDING), ("deleted_at", ASCENDING)]),
            IndexModel("autoscaling_group_id"),
            IndexModel("node_id"),
        ]
    )
    await database.cloud_nodes.create_indexes(
        [
            IndexModel("name", unique=True, name="uq_cloud_nodes_name"),
            IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True),
            IndexModel("agent_token_hash", unique=True),
            IndexModel([("status", ASCENDING), ("last_seen_at", DESCENDING)]),
        ]
    )
    await database.node_jobs.create_indexes(
        [
            IndexModel([("node_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)]),
            IndexModel("operation_id"),
            IndexModel("instance_id"),
            IndexModel("lease_expires_at"),
        ]
    )
    await database.operations.create_indexes(
        [
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("status", ASCENDING), ("created_at", ASCENDING)]),
            IndexModel("instance_id"),
        ]
    )
    await database.one_time_secrets.create_indexes(
        [IndexModel("token_hash", unique=True), IndexModel("expires_at")]
    )
    await database.launch_templates.create_indexes(
        [IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True)]
    )
    await database.autoscaling_groups.create_indexes(
        [IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True)]
    )
    await database.load_balancers.create_indexes(
        [
            IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True),
            IndexModel("autoscaling_group_id"),
        ]
    )
    await database.scaling_events.create_indexes(
        [IndexModel([("autoscaling_group_id", ASCENDING), ("created_at", DESCENDING)])]
    )
    await database.amis.create_indexes(
        [IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True)]
    )
    for collection in (database.key_pairs, database.security_groups, database.volumes):
        await collection.create_indexes(
            [IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True)]
        )


async def close_database() -> None:
    await client.close()
