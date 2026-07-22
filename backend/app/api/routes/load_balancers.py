import base64
import json
import re
import shlex
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser
from app.infrastructure.database import (
    Database,
    from_document,
    from_documents,
    to_document,
)
from app.infrastructure.database import (
    database as mongo_database,
)
from app.infrastructure.models import (
    Instance,
    LoadBalancer,
    Operation,
    OperationType,
    TargetGroup,
)
from app.infrastructure.vm import safe_name
from app.schemas import LoadBalancerIn, LoadBalancerOut, LoadBalancerTargetOut
from app.services import load_balancing, node_jobs, scheduler

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
    endpoint = row.public_endpoint
    endpoint_status = row.public_endpoint_status
    if row.appliance_instance_id:
        appliance = from_document(
            Instance,
            await mongo_database.instances.find_one({"_id": str(row.appliance_instance_id)}),
        )
        if appliance:
            endpoint = appliance.public_endpoint or endpoint
            endpoint_status = (
                "ACTIVE"
                if appliance.status == "RUNNING" and appliance.public_endpoint_status == "ACTIVE"
                else "ERROR"
                if appliance.status == "ERROR" or appliance.public_endpoint_status == "ERROR"
                else "CONFIGURING"
            )
    return LoadBalancerOut.model_validate(
        {
            **row.model_dump(),
            "endpoint_path": f"/api/v1/load-balancers/{row.id}/invoke/",
            "public_endpoint": endpoint,
            "public_endpoint_status": endpoint_status,
            "healthy_targets": healthy,
            "total_targets": len(targets),
        }
    )


async def validate_group(database: Database, user_id: UUID, group_id: UUID) -> TargetGroup:
    group = from_document(
        TargetGroup,
        await database.target_groups.find_one({"_id": str(group_id), "user_id": str(user_id)}),
    )
    if not group:
        raise HTTPException(404, "Target group not found")
    return group


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
    target_group = await validate_group(database, user.id, data.target_group_id)
    if await database.load_balancers.find_one({"user_id": str(user.id), "name": data.name}):
        raise HTTPException(409, "A load balancer with this name already exists")
    values = data.model_dump()
    values["target_port"] = target_group.port
    values["health_check_path"] = target_group.health_check_path
    values["health_check_interval_seconds"] = target_group.health_check_interval_seconds
    appliance = Instance(
        user_id=user.id,
        multipass_name=safe_name(str(user.id), f"lb-{data.name}"),
        display_name=f"{data.name} load balancer",
        description="Managed LocalEC2 load-balancer appliance",
        cpu_count=1,
        memory_mb=1024,
        disk_gb=10,
        instance_type="lb.small",
        image="22.04",
        system_role="load_balancer",
        availability_zone="pending-placement",
        public_hostname=("lb-" + re.sub(r"[^a-z0-9-]+", "-", data.name.lower()).strip("-"))[:63],
    )
    await database.instances.insert_one(to_document(appliance))
    try:
        appliance = await scheduler.reserve_instance(database, appliance, None)
        operation = Operation(
            user_id=user.id,
            instance_id=appliance.id,
            operation_type=OperationType.LAUNCH,
            request_payload={
                "name": appliance.display_name,
                "description": appliance.description,
                "image": appliance.image,
                "cpu_count": appliance.cpu_count,
                "memory_mb": appliance.memory_mb,
                "disk_gb": appliance.disk_gb,
                "instance_type": appliance.instance_type,
                "create_key_pair": False,
                "security_group_ids": [],
                "user_data": "",
            },
        )
        await database.operations.insert_one(to_document(operation))
    except Exception:
        current = from_document(
            Instance, await database.instances.find_one({"_id": str(appliance.id)})
        )
        if current:
            await scheduler.release_instance_capacity(database, current)
        await database.instances.delete_one({"_id": str(appliance.id)})
        raise
    row = LoadBalancer(
        user_id=user.id,
        appliance_instance_id=appliance.id,
        public_endpoint_status="CONFIGURING",
        **values,
    )
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
    target_group = await validate_group(database, user.id, data.target_group_id)
    update = {**data.model_dump(mode="json"), "updated_at": datetime.now(UTC)}
    update.update(
        target_port=target_group.port,
        health_check_path=target_group.health_check_path,
        health_check_interval_seconds=target_group.health_check_interval_seconds,
    )
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
            availability_zone=item.availability_zone,
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


@router.api_route(
    "/{item_id}/invoke/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def invoke(
    item_id: UUID,
    path: str,
    request: Request,
    user: CurrentUser,
    database: Database,
) -> Response:
    row = await owned(database, user.id, item_id)
    if not row.enabled:
        raise HTTPException(409, "Load balancer is disabled")
    targets = await load_balancing.refresh_load_balancer(row)
    target = await load_balancing.select_target(row, targets)
    if not target:
        raise HTTPException(503, "Load balancer has no healthy targets")
    body = await request.body()
    if len(body) > 1024 * 1024:
        await load_balancing.release_target(row.id, target.id)
        raise HTTPException(413, "Request body exceeds the 1 MiB gateway limit")
    target_path = "/" + path.lstrip("/")
    if request.url.query:
        target_path += "?" + request.url.query
    source = (
        "import base64,json,urllib.request,urllib.error\n"
        f"url={'http://127.0.0.1:' + str(row.target_port) + target_path!r}\n"
        f"body=base64.b64decode({base64.b64encode(body).decode()!r})\n"
        f"req=urllib.request.Request(url,data=body or None,method={request.method!r})\n"
        "try:\n response=urllib.request.urlopen(req,timeout=30)\n"
        "except urllib.error.HTTPError as exc:\n response=exc\n"
        "value=response.read(1048577)\n"
        "print(json.dumps({'status':response.status,'content_type':"
        "response.headers.get('content-type','application/octet-stream'),"
        "'body':base64.b64encode(value).decode()}))"
    )
    encoded = base64.b64encode(source.encode()).decode()
    try:
        output_value = await node_jobs.remote_exec(
            target,
            "python3 -c " + shlex.quote(f"import base64;exec(base64.b64decode({encoded!r}))"),
            timeout=45,
        )
        result = json.loads(output_value.strip().splitlines()[-1])
        response_body = base64.b64decode(result["body"])
        if len(response_body) > 1024 * 1024:
            raise HTTPException(502, "Target response exceeds the 1 MiB gateway limit")
        return Response(
            content=response_body,
            status_code=int(result["status"]),
            media_type=str(result["content_type"]).split(";", 1)[0],
            headers={"X-LocalEC2-Target": str(target.id)},
        )
    finally:
        await load_balancing.release_target(row.id, target.id)


@router.delete("/{item_id}", status_code=204)
async def delete_load_balancer(item_id: UUID, user: CurrentUser, database: Database) -> None:
    row = await owned(database, user.id, item_id)
    await database.load_balancers.delete_one({"_id": str(row.id)})
    if row.appliance_instance_id:
        appliance = from_document(
            Instance,
            await database.instances.find_one(
                {"_id": str(row.appliance_instance_id), "deleted_at": None}
            ),
        )
        if appliance:
            operation = Operation(
                user_id=user.id,
                instance_id=appliance.id,
                operation_type=OperationType.TERMINATE,
            )
            await database.operations.insert_one(to_document(operation))
    for key in [key for key in load_balancing.health if key[0] == row.id]:
        load_balancing.health.pop(key, None)
        load_balancing.cpu_loads.pop(key, None)
        load_balancing.active_connections.pop(key, None)
    load_balancing.round_robin_positions.pop(row.id, None)
    load_balancing.last_checks.pop(row.id, None)
