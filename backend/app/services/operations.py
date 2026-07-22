import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pymongo import ReturnDocument

from app.core.config import settings
from app.core.security import fernet, opaque_token, token_hash
from app.infrastructure.database import database, from_document, to_document
from app.infrastructure.models import (
    Instance,
    InstanceStatus,
    KeyPair,
    OneTimeSecret,
    Operation,
    OperationStatus,
    OperationType,
)
from app.infrastructure.vm import ProviderUnavailable
from app.services import node_jobs, scheduler

worker_task: asyncio.Task[None] | None = None
log = logging.getLogger("localec2.operations")


def key_pair() -> tuple[bytes, str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    digest = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public, hashlib.sha256(digest).hexdigest()


async def update(operation_id: UUID, progress: int, message: str) -> None:
    await database.operations.update_one(
        {"_id": str(operation_id)},
        {"$set": {"progress": progress, "message": message}},
    )


async def security_script(instance: Instance) -> str:
    if not instance.security_group_ids:
        return ""
    groups = await database.security_groups.find(
        {"_id": {"$in": [str(value) for value in instance.security_group_ids]}}
    ).to_list(None)
    commands = [
        "command -v ufw >/dev/null || (apt-get update && apt-get install -y ufw)",
        "ufw --force reset",
        "ufw default deny incoming",
        "ufw default allow outgoing",
    ]
    egress_rules: list[dict[str, object]] = []
    for group in groups:
        for rule in group.get("ingress_rules", []):
            port = str(rule["from_port"])
            if rule["to_port"] != rule["from_port"]:
                port += f":{rule['to_port']}"
            commands.append(
                f"ufw allow from {rule['source']} to any port {port} proto {rule['protocol']}"
            )
        egress_rules.extend(group.get("egress_rules", []))
    if groups and all(group.get("egress_rules") for group in groups):
        commands.append("ufw default deny outgoing")
        for rule in egress_rules:
            port = str(rule["from_port"])
            if rule["to_port"] != rule["from_port"]:
                port += f":{rule['to_port']}"
            commands.append(
                f"ufw allow out to {rule['source']} port {port} proto {rule['protocol']}"
            )
        commands.extend(["ufw allow out 53/udp", "ufw allow out 53/tcp"])
        if settings.tailscale_reusable_auth_key:
            commands.extend(
                [
                    "ufw allow out 443/tcp",
                    "ufw allow out 41641/udp",
                    "ufw allow out 3478/udp",
                ]
            )
    commands.extend(["ufw allow 22/tcp", "ufw --force enable"])
    return "\n".join(commands)


async def launch_identity(
    operation: Operation, instance: Instance
) -> tuple[str | None, str | None, dict[str, object]]:
    request = dict(operation.request_payload)
    result = dict(operation.result_payload)
    public = request.get("node_public_key")
    fingerprint = request.get("node_key_fingerprint")
    if isinstance(public, str) and isinstance(fingerprint, str):
        token = result.get("key_download_token")
        if isinstance(token, str):
            await database.one_time_secrets.update_one(
                {"token_hash": token_hash(token), "consumed_at": None},
                {
                    "$set": {
                        "expires_at": datetime.now(UTC)
                        + timedelta(minutes=settings.one_time_secret_minutes)
                    }
                },
            )
        return public, fingerprint, result
    if instance.key_pair_id:
        reusable = from_document(
            KeyPair,
            await database.key_pairs.find_one({"_id": str(instance.key_pair_id)}),
        )
        if not reusable:
            raise RuntimeError("Selected key pair no longer exists")
        public, fingerprint = reusable.public_key, reusable.fingerprint
    elif request.get("create_key_pair"):
        private, public, fingerprint = key_pair()
        token = opaque_token()
        secret = OneTimeSecret(
            user_id=instance.user_id,
            instance_id=instance.id,
            token_hash=token_hash(token),
            encrypted_value=fernet().encrypt(private),
            filename=f"localec2-{instance.display_name}.pem",
            # Keep the secret alive while a slow node launch is in progress. The
            # normal short download window is applied after the launch succeeds.
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        await database.one_time_secrets.insert_one(to_document(secret))
        result.update(key_download_token=token, key_filename=secret.filename)
    else:
        public = fingerprint = None
    request.update(node_public_key=public, node_key_fingerprint=fingerprint)
    await database.operations.update_one(
        {"_id": str(operation.id)},
        {"$set": {"request_payload": request, "result_payload": result}},
    )
    operation.request_payload = request
    operation.result_payload = result
    return (
        public if isinstance(public, str) else None,
        (fingerprint if isinstance(fingerprint, str) else None),
        result,
    )


async def run_launch(operation: Operation, instance: Instance) -> dict[str, object]:
    if not instance.capacity_reserved:
        await update(operation.id, 20, "Selecting approved compute capacity")
        requested_zone = operation.request_payload.get("availability_zone")
        instance = await scheduler.reserve_instance(
            database,
            instance,
            str(requested_zone) if requested_zone else None,
        )
    scheduler.require_distributed(instance)
    public, fingerprint, result = await launch_identity(operation, instance)
    await update(operation.id, 35, f"Dispatching VM creation to {instance.availability_zone}")
    payload: dict[str, object] = {
        "name": instance.multipass_name,
        "image": instance.image,
        "cpu_count": instance.cpu_count,
        "memory_mb": instance.memory_mb,
        "disk_gb": instance.disk_gb,
        "public_key": public or "",
        "user_data": str(operation.request_payload.get("user_data") or ""),
        "security_script": await security_script(instance),
        "ami_id": str(instance.ami_id) if instance.ami_id else "",
    }
    remote = await node_jobs.dispatch(instance, "LAUNCH", payload, operation.id)
    tailscale: dict[str, object] = {}
    public_endpoint_status = "DISABLED"
    public_endpoint_error: str | None = None
    if settings.tailscale_reusable_auth_key:
        try:
            await update(operation.id, 85, "Publishing the instance through Tailscale Funnel")
            tailscale = await publish_instance(instance)
            public_endpoint_status = "ACTIVE"
        except Exception as exc:
            # The VM itself is healthy. Keep its capacity reservation and make
            # the publishing failure visible without misreporting the VM as dead.
            public_endpoint_status = "ERROR"
            public_endpoint_error = f"Public endpoint setup failed: {exc}"[:1000]
            log.warning("Instance %s Tailscale setup failed: %s", instance.id, exc)
    token = result.get("key_download_token")
    if isinstance(token, str):
        await database.one_time_secrets.update_one(
            {"token_hash": token_hash(token), "consumed_at": None},
            {
                "$set": {
                    "expires_at": datetime.now(UTC)
                    + timedelta(minutes=settings.one_time_secret_minutes)
                }
            },
        )
    await database.instances.update_one(
        {"_id": str(instance.id)},
        {
            "$set": {
                "status": InstanceStatus.RUNNING,
                "ipv4_address": remote.get("ipv4_address"),
                "tailscale_ipv4_address": tailscale.get("tailscale_ipv4_address"),
                "tailscale_dns_name": tailscale.get("tailscale_dns_name"),
                "public_endpoint": tailscale.get("public_endpoint"),
                "public_endpoint_status": public_endpoint_status,
                "key_fingerprint": fingerprint,
                "has_key_pair": public is not None,
                "last_error": public_endpoint_error,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    result["instance_id"] = str(instance.id)
    return result


async def publish_instance(instance: Instance) -> dict[str, object]:
    if not settings.tailscale_reusable_auth_key:
        raise RuntimeError("TAILSCALE_REUSABLE_AUTH_KEY is not configured")
    await database.instances.update_one(
        {"_id": str(instance.id)},
        {
            "$set": {
                "public_endpoint_status": "CONFIGURING",
                "last_error": None,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    hostname = instance.public_hostname or f"instance-{str(instance.id).replace('-', '')[:12]}"
    try:
        published = await node_jobs.dispatch_secret(
            instance,
            "TAILSCALE_SETUP",
            {
                "name": instance.multipass_name,
                "auth_key": settings.tailscale_reusable_auth_key,
                "hostname": hostname,
                "tag": settings.tailscale_instance_tag,
                "target_port": settings.tailscale_funnel_target_port,
                "install_default_web": instance.system_role == "customer",
            },
            timeout=360,
        )
    except Exception as exc:
        await database.instances.update_one(
            {"_id": str(instance.id)},
            {
                "$set": {
                    "public_endpoint_status": "ERROR",
                    "last_error": f"Public endpoint setup failed: {exc}"[:1000],
                    "updated_at": datetime.now(UTC),
                }
            },
        )
        raise
    await database.instances.update_one(
        {"_id": str(instance.id)},
        {
            "$set": {
                **published,
                "public_endpoint_status": "ACTIVE",
                "last_error": None,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    return published


async def run_operation(operation: Operation) -> None:
    if not operation.instance_id:
        await database.operations.update_one(
            {"_id": str(operation.id)},
            {
                "$set": {
                    "status": OperationStatus.FAILED,
                    "message": "Failed",
                    "error_code": "INSTANCE_ID_REQUIRED",
                    "error_message": "This operation has no instance target.",
                    "finished_at": datetime.now(UTC),
                }
            },
        )
        return
    instance = from_document(
        Instance,
        await database.instances.find_one({"_id": str(operation.instance_id)}),
    )
    if not instance:
        await database.operations.update_one(
            {"_id": str(operation.id)},
            {
                "$set": {
                    "status": OperationStatus.FAILED,
                    "message": "Failed",
                    "error_code": "INSTANCE_NOT_FOUND",
                    "error_message": "The instance record no longer exists.",
                    "finished_at": datetime.now(UTC),
                }
            },
        )
        return
    try:
        result = dict(operation.result_payload)
        if operation.operation_type in (OperationType.LAUNCH, OperationType.SCALE_OUT):
            result = await run_launch(operation, instance)
        elif operation.operation_type == OperationType.PUBLISH_ENDPOINT:
            scheduler.require_distributed(instance)
            if instance.status != InstanceStatus.RUNNING:
                raise RuntimeError("Instance must be running to publish its endpoint")
            await update(operation.id, 40, "Installing Tailscale and enabling Funnel")
            result.update(await publish_instance(instance))
        else:
            scheduler.require_distributed(instance)
            if operation.operation_type == OperationType.START:
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {"$set": {"status": InstanceStatus.STARTING}},
                )
                await update(operation.id, 40, "Starting instance on assigned node")
                remote = await node_jobs.dispatch(
                    instance,
                    "START",
                    {
                        "name": instance.multipass_name,
                        "security_script": await security_script(instance),
                    },
                    operation.id,
                )
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {
                        "$set": {
                            "status": InstanceStatus.RUNNING,
                            "ipv4_address": remote.get("ipv4_address"),
                            "public_endpoint_status": (
                                "ACTIVE" if instance.public_endpoint else "DISABLED"
                            ),
                            "updated_at": datetime.now(UTC),
                        }
                    },
                )
            elif operation.operation_type == OperationType.STOP:
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {"$set": {"status": InstanceStatus.STOPPING}},
                )
                await update(operation.id, 40, "Stopping instance on assigned node")
                await node_jobs.dispatch(
                    instance, "STOP", {"name": instance.multipass_name}, operation.id
                )
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {
                        "$set": {
                            "status": InstanceStatus.STOPPED,
                            "ipv4_address": None,
                            "public_endpoint_status": "DISABLED",
                            "updated_at": datetime.now(UTC),
                        }
                    },
                )
            elif operation.operation_type == OperationType.HIBERNATE:
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {"$set": {"status": InstanceStatus.HIBERNATING}},
                )
                await update(operation.id, 40, "Suspending instance on assigned node")
                await node_jobs.dispatch(
                    instance, "SUSPEND", {"name": instance.multipass_name}, operation.id
                )
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {
                        "$set": {
                            "status": InstanceStatus.HIBERNATED,
                            "ipv4_address": None,
                            "public_endpoint_status": "DISABLED",
                            "updated_at": datetime.now(UTC),
                        }
                    },
                )
            elif operation.operation_type in (OperationType.TERMINATE, OperationType.SCALE_IN):
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {"$set": {"status": InstanceStatus.TERMINATING}},
                )
                attached_volumes = await database.volumes.find(
                    {"attached_instance_id": str(instance.id)}
                ).to_list(None)
                for volume in attached_volumes:
                    try:
                        await node_jobs.dispatch(
                            instance,
                            "VOLUME_DETACH",
                            {
                                "name": instance.multipass_name,
                                "mount_path": volume.get("mount_path") or "/mnt/data",
                                "device_name": volume.get("device_name") or "vdb",
                                "volume_id": str(volume["_id"]),
                            },
                            timeout=600,
                        )
                    except Exception as exc:
                        # The VM is about to be destroyed, so a missing backing
                        # image, mount, or guest must not leave a persistent
                        # volume logically attached to a deleted instance.
                        log.warning(
                            "Continuing termination after volume %s detach failed: %s",
                            volume["_id"],
                            exc,
                        )
                    finally:
                        await database.volumes.update_one(
                            {
                                "_id": str(volume["_id"]),
                                "attached_instance_id": str(instance.id),
                            },
                            {
                                "$set": {
                                    "attached_instance_id": None,
                                    "device_name": None,
                                    "mount_path": None,
                                    "node_id": None,
                                    "last_node_id": str(instance.node_id),
                                    "updated_at": datetime.now(UTC),
                                }
                            },
                        )
                await update(operation.id, 40, "Deleting instance on assigned node")
                await node_jobs.dispatch(
                    instance, "DELETE", {"name": instance.multipass_name}, operation.id
                )
                await scheduler.release_instance_capacity(database, instance)
                await database.instances.update_one(
                    {"_id": str(instance.id)},
                    {
                        "$set": {
                            "deleted_at": datetime.now(UTC),
                            "ipv4_address": None,
                            "public_endpoint": None,
                            "public_endpoint_status": "DISABLED",
                            "updated_at": datetime.now(UTC),
                        }
                    },
                )
        await database.operations.update_one(
            {"_id": str(operation.id)},
            {
                "$set": {
                    "status": OperationStatus.SUCCEEDED,
                    "progress": 100,
                    "message": "Completed",
                    "result_payload": result,
                    "finished_at": datetime.now(UTC),
                }
            },
        )
        if operation.operation_type in (OperationType.SCALE_OUT, OperationType.SCALE_IN):
            await database.scaling_events.update_one(
                {"operation_id": str(operation.id)}, {"$set": {"status": "SUCCEEDED"}}
            )
    except Exception as exc:
        launch_failed = operation.operation_type in (
            OperationType.LAUNCH,
            OperationType.SCALE_OUT,
        )
        if launch_failed:
            current = from_document(
                Instance,
                await database.instances.find_one({"_id": str(instance.id)}),
            )
            if current:
                await scheduler.release_instance_capacity(database, current)
        await database.instances.update_one(
            {"_id": str(instance.id)},
            {
                "$set": {
                    "status": InstanceStatus.ERROR if launch_failed else instance.status,
                    "last_error": str(exc)[:1000],
                    "updated_at": datetime.now(UTC),
                }
            },
        )
        await database.operations.update_one(
            {"_id": str(operation.id)},
            {
                "$set": {
                    "status": OperationStatus.FAILED,
                    "error_code": (
                        "NODE_EXECUTION_ERROR"
                        if isinstance(exc, ProviderUnavailable)
                        else "OPERATION_FAILED"
                    ),
                    "error_message": str(exc)[:1000],
                    "message": "Failed",
                    "finished_at": datetime.now(UTC),
                }
            },
        )
        if operation.operation_type in (OperationType.SCALE_OUT, OperationType.SCALE_IN):
            await database.scaling_events.update_one(
                {"operation_id": str(operation.id)},
                {"$set": {"status": "FAILED", "reason": str(exc)[:300]}},
            )


async def operation_loop() -> None:
    running: dict[UUID, asyncio.Task[None]] = {}
    try:
        while True:
            try:
                for operation_id, task in list(running.items()):
                    if task.done():
                        try:
                            task.result()
                        except Exception as exc:
                            log.warning("Operation task will be recovered after error: %s", exc)
                        running.pop(operation_id)
                while len(running) < settings.operation_worker_concurrency:
                    document = await database.operations.find_one_and_update(
                        {"status": OperationStatus.PENDING},
                        {
                            "$set": {
                                "status": OperationStatus.RUNNING,
                                "started_at": datetime.now(UTC),
                                "progress": 10,
                                "message": "Preparing distributed operation",
                            }
                        },
                        sort=[("created_at", 1)],
                        return_document=ReturnDocument.AFTER,
                    )
                    if not document:
                        query: dict[str, object] = {"status": OperationStatus.RUNNING}
                        if running:
                            query["_id"] = {"$nin": [str(operation_id) for operation_id in running]}
                        document = await database.operations.find_one(
                            query, sort=[("created_at", 1)]
                        )
                    operation = from_document(Operation, document)
                    if not operation or operation.id in running:
                        break
                    running[operation.id] = asyncio.create_task(run_operation(operation))
                if running:
                    await asyncio.wait(
                        set(running.values()),
                        timeout=settings.operation_poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(settings.operation_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Operation worker retrying after control-plane error: %s", exc)
                await asyncio.sleep(settings.operation_poll_seconds)
    finally:
        for task in running.values():
            task.cancel()
        if running:
            await asyncio.gather(*running.values(), return_exceptions=True)
