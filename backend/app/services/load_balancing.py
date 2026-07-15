import asyncio
import logging
import shlex
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import settings
from app.infrastructure.database import database, from_documents
from app.infrastructure.metrics import CPU_PERCENT_COMMAND
from app.infrastructure.models import Instance, InstanceStatus, LoadBalancer
from app.services import node_jobs, scheduler

health: dict[tuple[UUID, UUID], bool] = {}
cpu_loads: dict[tuple[UUID, UUID], float] = {}
active_connections: dict[tuple[UUID, UUID], int] = defaultdict(int)
round_robin_positions: dict[UUID, int] = defaultdict(int)
last_checks: dict[UUID, float] = {}
selection_lock = asyncio.Lock()
worker_task: asyncio.Task[None] | None = None
log = logging.getLogger("localec2.load_balancing")


async def group_targets(load_balancer: LoadBalancer) -> list[Instance]:
    documents = (
        await database.instances.find(
            {
                "autoscaling_group_id": str(load_balancer.autoscaling_group_id),
                "deleted_at": None,
                "status": InstanceStatus.RUNNING,
                "ipv4_address": {"$ne": None},
            }
        )
        .sort("created_at", 1)
        .to_list(None)
    )
    return from_documents(Instance, documents)


async def check_target(lb: LoadBalancer, target: Instance) -> None:
    key = (lb.id, target.id)
    try:
        scheduler.require_distributed(target)
        url = f"http://127.0.0.1:{lb.target_port}{lb.health_check_path}"
        probe = "python3 -c " + shlex.quote(
            "import urllib.request; "
            f"response=urllib.request.urlopen({url!r}, timeout=5); "
            "assert 200 <= response.status < 400"
        )
        output = await node_jobs.remote_exec(
            target, f"{probe} && {CPU_PERCENT_COMMAND}", timeout=15
        )
        cpu_loads[key] = float(output.strip().splitlines()[-1] or 0)
        health[key] = True
    except Exception:
        health[key] = False


async def refresh_load_balancer(lb: LoadBalancer) -> list[Instance]:
    targets = await group_targets(lb)
    last_checks[lb.id] = asyncio.get_running_loop().time()
    if targets:
        await asyncio.gather(*(check_target(lb, target) for target in targets))
    current = {(lb.id, target.id) for target in targets}
    for key in [key for key in health if key[0] == lb.id and key not in current]:
        health.pop(key, None)
        cpu_loads.pop(key, None)
        active_connections.pop(key, None)
    return targets


async def select_lowest_load(lb: LoadBalancer) -> tuple[Instance, float] | None:
    targets = await refresh_load_balancer(lb)
    eligible = [target for target in targets if health.get((lb.id, target.id), False)]
    if not eligible:
        return None
    async with selection_lock:
        target = min(
            eligible,
            key=lambda item: (
                cpu_loads.get((lb.id, item.id), 100.0),
                active_connections[(lb.id, item.id)],
                item.created_at,
            ),
        )
        active_connections[(lb.id, target.id)] += 1
        return target, cpu_loads.get((lb.id, target.id), 0.0)


async def select_target(lb: LoadBalancer, targets: list[Instance]) -> Instance | None:
    eligible = [target for target in targets if health.get((lb.id, target.id), False)]
    if not eligible:
        return None
    async with selection_lock:
        if lb.algorithm == "least_connections":
            target = min(
                eligible,
                key=lambda item: (active_connections[(lb.id, item.id)], item.created_at),
            )
        else:
            position = round_robin_positions[lb.id] % len(eligible)
            target = eligible[position]
            round_robin_positions[lb.id] = (position + 1) % len(eligible)
        active_connections[(lb.id, target.id)] += 1
        return target


async def release_target(lb_id: UUID, instance_id: UUID) -> None:
    async with selection_lock:
        key = (lb_id, instance_id)
        active_connections[key] = max(0, active_connections[key] - 1)


async def health_loop() -> None:
    while True:
        try:
            documents = await database.load_balancers.find({"enabled": True}).to_list(None)
            load_balancers = from_documents(LoadBalancer, documents)
            for lb in load_balancers:
                elapsed = asyncio.get_running_loop().time() - last_checks.get(lb.id, 0)
                if elapsed < lb.health_check_interval_seconds:
                    continue
                await refresh_load_balancer(lb)
                await database.load_balancers.update_one(
                    {"_id": str(lb.id)}, {"$set": {"updated_at": datetime.now(UTC)}}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Load-balancer worker retrying after control-plane error: %s", exc)
        await asyncio.sleep(settings.load_balancer_health_interval_seconds)
