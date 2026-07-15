import asyncio
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
from app.infrastructure.vm import provider, safe_name

samples: dict[UUID, deque[float]] = defaultdict(lambda: deque(maxlen=20))
worker_task: asyncio.Task[None] | None = None


async def average_cpu(instances: list[Instance]) -> float | None:
    values: list[float] = []
    for instance in instances:
        if instance.status != InstanceStatus.RUNNING:
            continue
        try:
            output = await provider.exec(
                instance.multipass_name,
                [
                    "sh",
                    "-lc",
                    CPU_PERCENT_COMMAND,
                ],
            )
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
        key_pair_id=template.key_pair_id,
    )
    operation = Operation(
        user_id=group.user_id,
        instance_id=instance.id,
        operation_type=OperationType.SCALE_OUT,
        request_payload={
            "create_key_pair": template.create_key_pair,
            "autoscaling_group_id": str(group.id),
            "user_data": template.user_data,
        },
    )
    event = ScalingEvent(
        autoscaling_group_id=group.id,
        action="SCALE_OUT",
        previous_size=current_size,
        new_size=current_size + 1,
        reason=reason,
        average_cpu=samples[group.id][-1] if samples[group.id] else None,
        status="QUEUED",
    )
    await database.instances.insert_one(to_document(instance))
    await database.operations.insert_one(to_document(operation))
    await database.scaling_events.insert_one(to_document(event))
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
        action="SCALE_IN",
        previous_size=len(instances),
        new_size=len(instances) - 1,
        reason=reason,
        average_cpu=samples[group.id][-1] if samples[group.id] else None,
        status="QUEUED",
    )
    await database.operations.insert_one(to_document(operation))
    await database.scaling_events.insert_one(to_document(event))
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
    instances = from_documents(Instance, instance_documents)
    if instances and await database.operations.find_one(
        {
            "instance_id": {"$in": [str(item.id) for item in instances]},
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
        documents = await database.autoscaling_groups.find({"enabled": True}).to_list(length=None)
        for document in documents:
            group = from_document(AutoScalingGroup, document)
            if group:
                await evaluate_group(group)
        await asyncio.sleep(settings.autoscaling_interval_seconds)
