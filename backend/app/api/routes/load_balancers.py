from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.infrastructure.database import Database, from_document, from_documents, to_document
from app.infrastructure.models import AutoScalingGroup, LoadBalancer
from app.schemas import LoadBalancerIn, LoadBalancerOut, LoadBalancerTargetOut
from app.services import load_balancing

router = APIRouter(prefix="/load-balancers", tags=["Load balancers"])


async def owned(database: Database, user_id: UUID, item_id: UUID) -> LoadBalancer:
    row = from_document(
        LoadBalancer,
        await database.load_balancers.find_one({"_id": str(item_id), "user_id": str(user_id)}),
    )
    if not row:
        raise HTTPException(404, "Load balancer not found")
    return row


async def output(row: LoadBalancer) -> LoadBalancerOut:
    targets = await load_balancing.group_targets(row)
    healthy = sum(load_balancing.health.get((row.id, item.id), False) for item in targets)
    return LoadBalancerOut.model_validate(
        {
            **row.model_dump(),
            "healthy_targets": healthy,
            "total_targets": len(targets),
        }
    )


async def validate_group(database: Database, user_id: UUID, group_id: UUID) -> None:
    group = from_document(
        AutoScalingGroup,
        await database.autoscaling_groups.find_one({"_id": str(group_id), "user_id": str(user_id)}),
    )
    if not group:
        raise HTTPException(404, "Autoscaling group not found")


@router.get("", response_model=list[LoadBalancerOut])
async def list_load_balancers(user: CurrentUser, database: Database) -> list[LoadBalancerOut]:
    rows = from_documents(
        LoadBalancer,
        await database.load_balancers.find({"user_id": str(user.id)})
        .sort("created_at", -1)
        .to_list(None),
    )
    return [await output(row) for row in rows]


@router.post("", response_model=LoadBalancerOut, status_code=201)
async def create_load_balancer(
    data: LoadBalancerIn, user: CurrentUser, database: Database
) -> LoadBalancerOut:
    await validate_group(database, user.id, data.autoscaling_group_id)
    row = LoadBalancer(user_id=user.id, **data.model_dump())
    try:
        await database.load_balancers.insert_one(to_document(row))
    except DuplicateKeyError:
        raise HTTPException(409, "A load balancer with this name already exists") from None
    await load_balancing.refresh_load_balancer(row)
    return await output(row)


@router.patch("/{item_id}", response_model=LoadBalancerOut)
async def update_load_balancer(
    item_id: UUID,
    data: LoadBalancerIn,
    user: CurrentUser,
    database: Database,
) -> LoadBalancerOut:
    row = await owned(database, user.id, item_id)
    await validate_group(database, user.id, data.autoscaling_group_id)
    update = {**data.model_dump(mode="json"), "updated_at": datetime.now(UTC)}
    try:
        await database.load_balancers.update_one({"_id": str(row.id)}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "A load balancer with this name already exists") from None
    changed = LoadBalancer.model_validate({**row.model_dump(), **update})
    if changed.enabled:
        await load_balancing.refresh_load_balancer(changed)
    return await output(changed)


@router.get("/{item_id}/targets", response_model=list[LoadBalancerTargetOut])
async def targets(
    item_id: UUID, user: CurrentUser, database: Database
) -> list[LoadBalancerTargetOut]:
    row = await owned(database, user.id, item_id)
    items = await load_balancing.group_targets(row)
    return [
        LoadBalancerTargetOut(
            instance_id=item.id,
            name=item.display_name,
            address=item.ipv4_address,
            status=item.status,
            healthy=load_balancing.health.get((row.id, item.id), False),
            active_connections=load_balancing.active_connections[(row.id, item.id)],
            cpu_percent=load_balancing.cpu_loads.get((row.id, item.id)),
        )
        for item in items
    ]


@router.post("/{item_id}/check", response_model=LoadBalancerOut)
async def check(item_id: UUID, user: CurrentUser, database: Database) -> LoadBalancerOut:
    row = await owned(database, user.id, item_id)
    await load_balancing.refresh_load_balancer(row)
    return await output(row)


@router.delete("/{item_id}", status_code=204)
async def delete_load_balancer(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row = await owned(database, user.id, item_id)
    await database.load_balancers.delete_one({"_id": str(row.id)})
    for key in [key for key in load_balancing.health if key[0] == row.id]:
        load_balancing.health.pop(key, None)
        load_balancing.cpu_loads.pop(key, None)
        load_balancing.active_connections.pop(key, None)
    load_balancing.round_robin_positions.pop(row.id, None)
    load_balancing.last_checks.pop(row.id, None)
