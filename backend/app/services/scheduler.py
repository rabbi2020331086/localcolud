from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from pymongo import ReturnDocument

from app.core.config import settings
from app.infrastructure.database import Database, from_document, from_documents
from app.infrastructure.models import CloudNode, ComputeProvider, Instance
from app.schemas import ZoneCapacityOut


def _available(total: int, reserved: int, allocated: int) -> int:
    return max(0, total - reserved - allocated)


def _capacity_expression(cpu: int, memory_mb: int, storage_gb: int) -> dict[str, object]:
    def fits(total: str, reserved: str, allocated: str, requested: int) -> dict[str, object]:
        return {
            "$lte": [
                {"$add": [{"$ifNull": [allocated, 0]}, requested]},
                {
                    "$subtract": [
                        {"$ifNull": [total, 0]},
                        {"$ifNull": [reserved, 0]},
                    ]
                },
            ]
        }

    return {
        "$and": [
            fits("$cpu_total", "$cpu_reserved", "$cpu_allocated", cpu),
            fits(
                "$memory_total_mb",
                "$memory_reserved_mb",
                "$memory_allocated_mb",
                memory_mb,
            ),
            fits(
                "$storage_total_gb",
                "$storage_reserved_gb",
                "$storage_allocated_gb",
                storage_gb,
            ),
        ]
    }


async def reserve_node(
    database: Database,
    cpu: int,
    memory_mb: int,
    storage_gb: int,
    availability_zone: str | None = None,
    required_node_id: UUID | None = None,
) -> CloudNode:
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    query: dict[str, object] = {
        "approved": True,
        "schedulable": True,
        "last_seen_at": {"$gte": deadline},
        "provider_name": ComputeProvider.VAGRANT_LIBVIRT,
        "capabilities": {"$all": ["jobs", "vagrant", "libvirt", "kvm"]},
        "$expr": _capacity_expression(cpu, memory_mb, storage_gb),
    }
    if availability_zone:
        query["availability_zone"] = availability_zone
    if required_node_id:
        query["_id"] = str(required_node_id)
    document = await database.cloud_nodes.find_one_and_update(
        query,
        {
            "$inc": {
                "cpu_allocated": cpu,
                "memory_allocated_mb": memory_mb,
                "storage_allocated_gb": storage_gb,
            },
            "$set": {"updated_at": datetime.now(UTC)},
        },
        sort=[("cpu_allocated", 1), ("memory_allocated_mb", 1), ("name", 1)],
        return_document=ReturnDocument.AFTER,
    )
    node = from_document(CloudNode, document)
    if not node:
        placement = f" in {availability_zone}" if availability_zone else ""
        raise HTTPException(
            409,
            "No approved cloud capacity"
            f"{placement} can satisfy the requested CPU, memory, and storage",
        )
    return node


async def release_instance_capacity(database: Database, instance: Instance) -> bool:
    if not instance.node_id or not instance.capacity_reserved:
        return False
    changed = await database.instances.find_one_and_update(
        {"_id": str(instance.id), "capacity_reserved": True},
        {"$set": {"capacity_reserved": False, "updated_at": datetime.now(UTC)}},
        return_document=ReturnDocument.BEFORE,
    )
    if not changed:
        return False
    await database.cloud_nodes.update_one(
        {"_id": str(instance.node_id)},
        [
            {
                "$set": {
                    "cpu_allocated": {
                        "$max": [
                            0,
                            {
                                "$subtract": [
                                    {"$ifNull": ["$cpu_allocated", 0]},
                                    instance.cpu_count,
                                ]
                            },
                        ]
                    },
                    "memory_allocated_mb": {
                        "$max": [
                            0,
                            {
                                "$subtract": [
                                    {"$ifNull": ["$memory_allocated_mb", 0]},
                                    instance.memory_mb,
                                ]
                            },
                        ]
                    },
                    "storage_allocated_gb": {
                        "$max": [
                            0,
                            {
                                "$subtract": [
                                    {"$ifNull": ["$storage_allocated_gb", 0]},
                                    instance.disk_gb,
                                ]
                            },
                        ]
                    },
                    "updated_at": datetime.now(UTC),
                }
            }
        ],
    )
    return True


async def reserve_instance(
    database: Database, instance: Instance, availability_zone: str | None = None
) -> Instance:
    claimed = await database.instances.find_one_and_update(
        {
            "_id": str(instance.id),
            "capacity_reserved": False,
            "placement_pending": {"$ne": True},
        },
        {"$set": {"placement_pending": True, "updated_at": datetime.now(UTC)}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        current = from_document(
            Instance, await database.instances.find_one({"_id": str(instance.id)})
        )
        if current and current.capacity_reserved:
            return current
        raise HTTPException(409, "Instance placement is already in progress")
    node: CloudNode | None = None
    try:
        node = await reserve_node(
            database,
            instance.cpu_count,
            instance.memory_mb,
            instance.disk_gb,
            availability_zone,
        )
        changes = {
            "node_id": str(node.id),
            "availability_zone": node.availability_zone,
            "provider_name": node.provider_name,
            "ssh_username": "vagrant",
            "capacity_reserved": True,
            "updated_at": datetime.now(UTC),
        }
        await database.instances.update_one(
            {"_id": str(instance.id), "placement_pending": True},
            {"$set": changes, "$unset": {"placement_pending": ""}},
        )
        return Instance.model_validate({**instance.model_dump(), **changes})
    except Exception:
        current = from_document(
            Instance, await database.instances.find_one({"_id": str(instance.id)})
        )
        if current and current.capacity_reserved:
            return current
        if node:
            await database.cloud_nodes.update_one(
                {"_id": str(node.id)},
                {
                    "$inc": {
                        "cpu_allocated": -instance.cpu_count,
                        "memory_allocated_mb": -instance.memory_mb,
                        "storage_allocated_gb": -instance.disk_gb,
                    }
                },
            )
        await database.instances.update_one(
            {"_id": str(instance.id)}, {"$unset": {"placement_pending": ""}}
        )
        raise


async def zone_capacity(database: Database) -> list[ZoneCapacityOut]:
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    nodes = from_documents(
        CloudNode,
        await database.cloud_nodes.find(
            {
                "approved": True,
                "schedulable": True,
                "last_seen_at": {"$gte": deadline},
                "provider_name": ComputeProvider.VAGRANT_LIBVIRT,
                "capabilities": {"$all": ["jobs", "vagrant", "libvirt", "kvm"]},
            }
        ).to_list(None),
    )
    zones: dict[str, bool] = {}
    for node in nodes:
        zones[node.availability_zone] = zones.get(node.availability_zone, False) or (
            _available(node.cpu_total, node.cpu_reserved, node.cpu_allocated) >= 1
            and _available(
                node.memory_total_mb,
                node.memory_reserved_mb,
                node.memory_allocated_mb,
            )
            >= 512
            and _available(
                node.storage_total_gb,
                node.storage_reserved_gb,
                node.storage_allocated_gb,
            )
            >= 5
        )
    return [
        ZoneCapacityOut(availability_zone=name, available=available)
        for name, available in sorted(zones.items())
    ]


def require_distributed(instance: Instance) -> None:
    if (
        not instance.node_id
        or instance.availability_zone == "legacy-unassigned"
        or instance.provider_name != ComputeProvider.VAGRANT_LIBVIRT
    ):
        raise HTTPException(
            409,
            "This legacy instance is not assigned to managed cloud capacity. Ask a platform "
            "administrator to migrate it, or recreate it in an available zone.",
        )
