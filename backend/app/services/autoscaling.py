import asyncio
import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import settings
from app.infrastructure.database import database, from_document, from_documents, to_document
from app.infrastructure.metrics import CPU_PERCENT_COMMAND
from app.infrastructure.models import (
    AutoScalingGroup,
    Instance,
    InstanceStatus,
    LaunchTemplate,
    Operation,
    OperationStatus,
    OperationType,
    ScalingEvent,
)
from app.infrastructure.vm import safe_name
from app.services import node_jobs, scheduler

samples: dict[UUID, deque[float]] = defaultdict(lambda: deque(maxlen=20))
worker_task: asyncio.Task[None] | None = None
log = logging.getLogger("localec2.autoscaling")


def capacity_instances(instances: list[Instance]) -> list[Instance]:
    """Return managed instances that can still contribute to desired capacity."""
    return [
        instance
        for instance in instances
        if instance.status not in (InstanceStatus.ERROR, InstanceStatus.TERMINATING)
    ]


async def average_cpu(instances: list[Instance]) -> float | None:
    values: list[float] = []
    for instance in instances:
        if instance.status != InstanceStatus.RUNNING:
            continue
        try:
            scheduler.require_distributed(instance)
            output = await node_jobs.remote_exec(instance, CPU_PERCENT_COMMAND)
            values.append(float(output.strip() or 0))
        except Exception:
            continue
    return sum(values) / len(values) if values else None


async def scale_out(group: AutoScalingGroup, current_size: int, reason: str) -> None:
    template = from_document(
        LaunchTemplate,
        await database.launch_templates.find_one({"_id": str(group.launch_template_id)}),
    )
    if not template:
        return
    requested_zone = None
    if template.availability_zones:
        zone_counts: dict[str, int] = {zone: 0 for zone in template.availability_zones}
        existing = await database.instances.find(
            {"autoscaling_group_id": str(group.id), "deleted_at": None},
            {"availability_zone": 1},
        ).to_list(None)
        for row in existing:
            zone = str(row.get("availability_zone") or "")
            if zone in zone_counts:
                zone_counts[zone] += 1
        requested_zone = min(zone_counts, key=lambda zone: (zone_counts[zone], zone))
    instance = Instance(
        user_id=group.user_id,
        autoscaling_group_id=group.id,
        multipass_name=safe_name(str(group.user_id), f"{group.name}-{current_size + 1}"),
        display_name=f"{group.name}-{current_size + 1}",
        description=f"Managed by autoscaling group {group.name}",
        image=template.image,
        cpu_count=template.cpu_count,
        memory_mb=template.memory_mb,
        disk_gb=template.disk_gb,
        instance_type=template.instance_type,
        key_pair_id=template.key_pair_id,
        security_group_ids=template.security_group_ids,
        availability_zone="pending-placement",
    )
    operation = Operation(
        user_id=group.user_id,
        instance_id=instance.id,
        operation_type=OperationType.SCALE_OUT,
        request_payload={
            "create_key_pair": template.create_key_pair,
            "autoscaling_group_id": str(group.id),
            "user_data": template.user_data,
            "availability_zone": requested_zone,
        },
    )
    event = ScalingEvent(
        autoscaling_group_id=group.id,
        operation_id=operation.id,
        action="SCALE_OUT",
        previous_size=current_size,
        new_size=current_size + 1,
        reason=reason,
        average_cpu=samples[group.id][-1] if samples[group.id] else None,
        status="QUEUED",
    )
    await database.instances.insert_one(to_document(instance))
    try:
        instance = await scheduler.reserve_instance(database, instance, requested_zone)
        await database.scaling_events.insert_one(to_document(event))
        await database.operations.insert_one(to_document(operation))
    except Exception as exc:
        current = from_document(
            Instance, await database.instances.find_one({"_id": str(instance.id)})
        )
        if current:
            await scheduler.release_instance_capacity(database, current)
        await database.instances.delete_one({"_id": str(instance.id)})
        failure = await database.scaling_events.update_one(
            {"_id": str(event.id)},
            {
                "$set": {
                    "status": "FAILED",
                    "reason": f"{reason}: {str(exc)[:300]}",
                }
            },
        )
        if not failure.matched_count:
            event.status = "FAILED"
            event.reason = f"{reason}: {str(exc)[:300]}"
            await database.scaling_events.insert_one(to_document(event))
        return
    await database.autoscaling_groups.update_one(
        {"_id": str(group.id)},
        {
            "$set": {
                "desired_size": group.desired_size,
                "last_scaling_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        },
    )


async def scale_in(group: AutoScalingGroup, instances: list[Instance], reason: str) -> None:
    candidate = next(
        (
            item
            for item in sorted(instances, key=lambda value: value.created_at, reverse=True)
            if item.status
            in (
                InstanceStatus.RUNNING,
                InstanceStatus.STOPPED,
                InstanceStatus.HIBERNATED,
            )
        ),
        None,
    )
    if not candidate:
        return
    operation = Operation(
        user_id=group.user_id,
        instance_id=candidate.id,
        operation_type=OperationType.SCALE_IN,
    )
    event = ScalingEvent(
        autoscaling_group_id=group.id,
        operation_id=operation.id,
        action="SCALE_IN",
        previous_size=len(instances),
        new_size=len(instances) - 1,
        reason=reason,
        average_cpu=samples[group.id][-1] if samples[group.id] else None,
        status="QUEUED",
    )
    await database.scaling_events.insert_one(to_document(event))
    await database.operations.insert_one(to_document(operation))
    await database.autoscaling_groups.update_one(
        {"_id": str(group.id)},
        {
            "$set": {
                "desired_size": group.desired_size,
                "last_scaling_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        },
    )


async def evaluate_group(group: AutoScalingGroup) -> None:
    instance_documents = await database.instances.find(
        {"autoscaling_group_id": str(group.id), "deleted_at": None}
    ).to_list(length=None)
    all_instances = from_documents(Instance, instance_documents)
    instances = capacity_instances(all_instances)
    if all_instances and await database.operations.find_one(
        {
            "instance_id": {"$in": [str(item.id) for item in all_instances]},
            "status": {"$in": [OperationStatus.PENDING, OperationStatus.RUNNING]},
        }
    ):
        return
    current_size = len(instances)
    if current_size < group.desired_size:
        await scale_out(group, current_size, "Reconciling desired capacity")
        return
    if current_size > group.desired_size:
        await scale_in(group, instances, "Reconciling desired capacity")
        return
    cpu = await average_cpu(instances)
    if cpu is None:
        return
    history = samples[group.id]
    history.append(cpu)
    if len(history) < group.evaluation_periods:
        return
    recent = list(history)[-group.evaluation_periods :]
    if group.last_scaling_at and datetime.now(UTC) - group.last_scaling_at < timedelta(
        seconds=group.cooldown_seconds
    ):
        return
    if (
        all(value >= group.scale_out_threshold for value in recent)
        and current_size < group.maximum_size
    ):
        group.desired_size = current_size + 1
        await scale_out(group, current_size, "Sustained CPU above scale-out threshold")
    elif (
        all(value <= group.scale_in_threshold for value in recent)
        and current_size > group.minimum_size
    ):
        group.desired_size = current_size - 1
        await scale_in(group, instances, "Sustained CPU below scale-in threshold")


async def autoscaling_loop() -> None:
    while True:
        try:
            documents = await database.autoscaling_groups.find({"enabled": True}).to_list(
                length=None
            )
            for document in documents:
                group = from_document(AutoScalingGroup, document)
                if group:
                    await evaluate_group(group)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Autoscaling worker retrying after control-plane error: %s", exc)
        await asyncio.sleep(settings.autoscaling_interval_seconds)
