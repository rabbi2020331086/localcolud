import asyncio
import hashlib
import os
import shlex
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pymongo import ReturnDocument

from app.core.config import settings
from app.core.security import fernet, opaque_token, token_hash
from app.infrastructure.database import database, from_document, to_document
from app.infrastructure.models import (
    Ami,
    Instance,
    InstanceStatus,
    KeyPair,
    OneTimeSecret,
    Operation,
    OperationStatus,
    OperationType,
)
from app.infrastructure.vm import ProviderUnavailable, provider

worker_task: asyncio.Task[None] | None = None


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


async def run_operation(operation: Operation) -> None:
    if not operation.instance_id:
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
        if operation.operation_type in (OperationType.LAUNCH, OperationType.SCALE_OUT):
            private = public = fingerprint = None
            cloud_file = None
            if instance.key_pair_id:
                reusable = from_document(
                    KeyPair,
                    await database.key_pairs.find_one({"_id": str(instance.key_pair_id)}),
                )
                if not reusable:
                    raise RuntimeError("Selected key pair no longer exists")
                public, fingerprint = reusable.public_key, reusable.fingerprint
            elif operation.request_payload.get("create_key_pair"):
                await update(operation.id, 20, "Generating SSH key")
                private, public, fingerprint = key_pair()
            if public:
                content = (
                    "#cloud-config\nusers:\n  - name: ubuntu\n"
                    "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
                    "    shell: /bin/bash\n"
                    "    ssh_authorized_keys:\n"
                    f"      - {public}\n"
                )
                runtime_root = settings.runtime_root.expanduser().resolve()
                runtime_root.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    "w",
                    suffix=".yaml",
                    delete=False,
                    encoding="utf-8",
                    dir=runtime_root,
                )
                handle.write(content)
                handle.close()
                os.chmod(handle.name, 0o644)
                cloud_file = handle.name
            await update(operation.id, 35, "Creating VM")
            try:
                # A failed launch can leave a partially-created Multipass VM.
                # Removing it makes retry idempotent and avoids name conflicts.
                await provider.delete(instance.multipass_name)
                if instance.ami_id:
                    ami = from_document(
                        Ami, await database.amis.find_one({"_id": str(instance.ami_id)})
                    )
                    if not ami:
                        raise RuntimeError("Selected AMI no longer exists")
                    provider_instance = await provider.clone(
                        ami.backing_instance_name,
                        instance.multipass_name,
                        instance.cpu_count,
                        instance.memory_mb,
                        instance.disk_gb,
                    )
                    if public:
                        quoted_key = shlex.quote(public)
                        await provider.exec(
                            instance.multipass_name,
                            [
                                "sh",
                                "-lc",
                                "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                                "touch ~/.ssh/authorized_keys && "
                                f"key={quoted_key}; "
                                'grep -qxF -- "$key" ~/.ssh/authorized_keys || '
                                "printf '%s\\n' \"$key\" >> ~/.ssh/authorized_keys; "
                                "chmod 600 ~/.ssh/authorized_keys",
                            ],
                        )
                else:
                    provider_instance = await provider.launch(
                        instance.multipass_name,
                        instance.image,
                        instance.cpu_count,
                        instance.memory_mb,
                        instance.disk_gb,
                        cloud_file,
                    )
            finally:
                if cloud_file:
                    Path(cloud_file).unlink(missing_ok=True)
            instance_update: dict[str, object] = {
                "status": InstanceStatus.RUNNING,
                "ipv4_address": provider_instance.ipv4,
                "key_fingerprint": fingerprint,
                "has_key_pair": public is not None,
                "updated_at": datetime.now(UTC),
            }
            user_data = operation.request_payload.get("user_data")
            if isinstance(user_data, str) and user_data.strip():
                await update(operation.id, 70, "Running template startup commands")
                await provider.exec(
                    instance.multipass_name,
                    ["sh", "-lc", user_data],
                    timeout=900,
                )
            result: dict[str, object] = {"instance_id": str(instance.id)}
            if private:
                token = opaque_token()
                secret = OneTimeSecret(
                    user_id=instance.user_id,
                    instance_id=instance.id,
                    token_hash=token_hash(token),
                    encrypted_value=fernet().encrypt(private),
                    filename=f"localec2-{instance.display_name}.pem",
                    expires_at=datetime.now(UTC)
                    + timedelta(minutes=settings.one_time_secret_minutes),
                )
                await database.one_time_secrets.insert_one(to_document(secret))
                result.update(key_download_token=token, key_filename=secret.filename)
            await database.instances.update_one(
                {"_id": str(instance.id)}, {"$set": instance_update}
            )
            if instance.security_group_ids:
                groups = await database.security_groups.find(
                    {"_id": {"$in": [str(value) for value in instance.security_group_ids]}}
                ).to_list(None)
                commands = [
                    "sudo ufw --force reset",
                    "sudo ufw default deny incoming",
                    "sudo ufw default allow outgoing",
                ]
                egress_rules: list[dict[str, object]] = []
                for group in groups:
                    for rule in group.get("ingress_rules", []):
                        port = str(rule["from_port"])
                        if rule["to_port"] != rule["from_port"]:
                            port += f":{rule['to_port']}"
                        commands.append(
                            f"sudo ufw allow from {rule['source']} to any port {port} "
                            f"proto {rule['protocol']}"
                        )
                    egress_rules.extend(group.get("egress_rules", []))
                # Security groups are additive. A group with no explicit
                # egress rules represents allow-all and therefore overrides
                # restrictions from another attached group.
                restrict_egress = bool(groups) and all(
                    group.get("egress_rules") for group in groups
                )
                if restrict_egress:
                    commands.append("sudo ufw default deny outgoing")
                    for rule in egress_rules:
                        port = str(rule["from_port"])
                        if rule["to_port"] != rule["from_port"]:
                            port += f":{rule['to_port']}"
                        commands.append(
                            f"sudo ufw allow out to {rule['source']} port {port} "
                            f"proto {rule['protocol']}"
                        )
                    # DNS is required to resolve hostnames for normal network use.
                    commands.extend(
                        [
                            "sudo ufw allow out 53/udp",
                            "sudo ufw allow out 53/tcp",
                        ]
                    )
                commands.extend(["sudo ufw allow 22/tcp", "sudo ufw --force enable"])
                await provider.exec(instance.multipass_name, ["sh", "-lc", "; ".join(commands)])
            operation.result_payload = result
        elif operation.operation_type == OperationType.START:
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {"$set": {"status": InstanceStatus.STARTING}},
            )
            await update(operation.id, 40, "Starting instance")
            await provider.start(instance.multipass_name)
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {
                    "$set": {
                        "status": InstanceStatus.RUNNING,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
        elif operation.operation_type == OperationType.STOP:
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {"$set": {"status": InstanceStatus.STOPPING}},
            )
            await update(operation.id, 40, "Stopping instance")
            await provider.stop(instance.multipass_name)
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {
                    "$set": {
                        "status": InstanceStatus.STOPPED,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
        elif operation.operation_type == OperationType.HIBERNATE:
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {"$set": {"status": InstanceStatus.HIBERNATING}},
            )
            await update(operation.id, 40, "Saving memory state")
            await provider.suspend(instance.multipass_name)
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {
                    "$set": {
                        "status": InstanceStatus.HIBERNATED,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
        elif operation.operation_type in (
            OperationType.TERMINATE,
            OperationType.SCALE_IN,
        ):
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {"$set": {"status": InstanceStatus.TERMINATING}},
            )
            await update(operation.id, 40, "Deleting VM")
            await provider.delete(instance.multipass_name)
            await database.volumes.update_many(
                {"attached_instance_id": str(instance.id)},
                {
                    "$set": {
                        "attached_instance_id": None,
                        "mount_path": None,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            await database.instances.update_one(
                {"_id": str(instance.id)},
                {
                    "$set": {
                        "deleted_at": datetime.now(UTC),
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
                    "result_payload": operation.result_payload,
                    "finished_at": datetime.now(UTC),
                }
            },
        )
    except Exception as exc:
        await database.instances.update_one(
            {"_id": str(instance.id)},
            {
                "$set": {
                    "status": InstanceStatus.ERROR,
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
                        "PROVIDER_ERROR"
                        if isinstance(exc, ProviderUnavailable)
                        else "OPERATION_FAILED"
                    ),
                    "error_message": str(exc)[:1000],
                    "message": "Failed",
                    "finished_at": datetime.now(UTC),
                }
            },
        )


async def operation_loop() -> None:
    while True:
        document = await database.operations.find_one_and_update(
            {"status": OperationStatus.PENDING},
            {
                "$set": {
                    "status": OperationStatus.RUNNING,
                    "started_at": datetime.now(UTC),
                    "progress": 10,
                    "message": "Preparing operation",
                }
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if document:
            operation = from_document(Operation, document)
            if operation:
                await run_operation(operation)
        else:
            await asyncio.sleep(settings.operation_poll_seconds)
