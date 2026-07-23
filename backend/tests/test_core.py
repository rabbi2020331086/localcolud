import base64
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from app.agent import NodeExecutor
from app.api.dependencies import has_platform_admin_access
from app.api.routes.auth import refresh_cookie_name
from app.api.routes.nodes import job_lease_seconds, observed_status
from app.core.security import create_jwt, decode_jwt, hash_password, verify_password
from app.infrastructure.models import (
    CloudNode,
    Instance,
    InstanceStatus,
    LoadBalancer,
    NodeStatus,
    ReplicationPolicy,
    TargetGroup,
    User,
)
from app.infrastructure.vm import safe_name
from app.schemas import InstanceCreate, ReplicationPolicyIn
from app.services import load_balancing
from app.services.autoscaling import capacity_instances
from app.services.reconciliation import provider_status


def test_password_hashing():
    encoded = hash_password("SecurePass123")
    assert encoded != "SecurePass123" and verify_password("SecurePass123", encoded)
    assert not verify_password("wrong", encoded)


def test_jwt_roundtrip():
    token = create_jwt("user-1", "access", timedelta(minutes=1))
    assert decode_jwt(token, "access")["sub"] == "user-1"


def test_safe_name():
    value = safe_name("12345678-aaaa", "My Dev_VM!")
    assert value.startswith("localec2-12345678-my-dev-vm-") and " " not in value


def test_instance_create_accepts_startup_user_data():
    script = "#!/usr/bin/env bash\nset -e\necho ready\n"
    request = InstanceCreate(name="web", user_data=script)

    assert request.user_data == script


def test_admin_and_customer_refresh_sessions_are_separate():
    customer = Request({"type": "http", "headers": []})
    admin = Request({"type": "http", "headers": [(b"x-localec2-client", b"admin")]})
    assert refresh_cookie_name(customer) == "localec2_refresh"
    assert refresh_cookie_name(admin) == "localec2_admin_refresh"


def test_node_agent_renders_libvirt_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_EC2_STORAGE_ROOT", str(tmp_path))
    provider = NodeExecutor()

    content = provider.render_vagrantfile(
        {
            "name": "web-01",
            "image": "24.04",
            "cpu_count": 4,
            "memory_mb": 8192,
            "disk_gb": 25,
            "public_key": "ssh-ed25519 AAAATEST portfolio@example",
            "_firmware_loader": "/usr/share/OVMF/OVMF_CODE_4M.fd",
            "_firmware_nvram": "/var/lib/localec2/instances/web-01/OVMF_VARS.fd",
        }
    )

    assert 'config.vm.box = "bento/ubuntu-24.04"' in content
    assert 'config.vm.provider "libvirt"' in content
    assert 'config.vm.synced_folder ".", "/vagrant", disabled: true' in content
    assert 'provider.driver = "kvm"' in content
    assert 'provider.cpu_mode = "host-passthrough"' in content
    assert 'provider.loader = "/usr/share/OVMF/OVMF_CODE_4M.fd"' in content
    assert 'provider.nvram = "/var/lib/localec2/instances/web-01/OVMF_VARS.fd"' in content
    assert "provider.cpus = 4" in content
    assert "provider.memory = 8192" in content
    assert "provider.machine_virtual_size = 25" in content
    encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", content)
    assert encoded and "authorized_keys" in base64.b64decode(encoded.group(1)).decode()

    assert provider.image_boxes["debian-12"] == "bento/debian-12"


def test_node_agent_retries_transient_download_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_EC2_STORAGE_ROOT", str(tmp_path))
    provider = NodeExecutor()
    root = provider.instance_root("web-01")
    root.mkdir(parents=True)
    outcomes = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Could not resolve host: vagrantcloud-files-production.example",
            ),
            SimpleNamespace(returncode=0, stdout="downloaded", stderr=""),
        ]
    )
    delays: list[int] = []
    monkeypatch.setattr("app.agent.subprocess.run", lambda *args, **kwargs: next(outcomes))
    monkeypatch.setattr("app.agent.time.sleep", delays.append)

    assert provider.run(root, "up") == "downloaded"
    assert delays == [5]


def test_replication_policy_enforces_classroom_safety_limits():
    policy = ReplicationPolicyIn(
        enabled=True,
        interval_minutes=5,
        replica_count=2,
        retention_points=3,
    )

    assert policy.enabled and policy.replica_count == 2


def test_replication_policy_belongs_to_one_customer_instance():
    user_id, instance_id = uuid4(), uuid4()
    policy = ReplicationPolicy(user_id=user_id, instance_id=instance_id, enabled=True)

    assert policy.user_id == user_id
    assert policy.instance_id == instance_id


def test_node_agent_finds_libvirt_root_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_EC2_STORAGE_ROOT", str(tmp_path))
    disk = tmp_path / "root.qcow2"
    disk.touch()
    result = SimpleNamespace(
        returncode=0,
        stdout=f" Type   Device   Target   Source\n file   disk     vda      {disk}\n",
        stderr="",
    )
    monkeypatch.setattr("app.agent.subprocess.run", lambda *args, **kwargs: result)

    assert NodeExecutor().domain_root_disk("web-01") == disk


def test_live_snapshot_uses_shared_read_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_EC2_STORAGE_ROOT", str(tmp_path))
    command = NodeExecutor.snapshot_convert_command(
        Path("/var/lib/libvirt/images/guest.img"), tmp_path / "point.qcow2"
    )

    assert command[2:9] == ["qemu-img", "convert", "-U", "-c", "-m", "2", "-p"]


def test_snapshot_jobs_use_short_failure_detection_lease():
    assert job_lease_seconds("REPLICA_CAPTURE") == 90
    assert job_lease_seconds("LAUNCH") > job_lease_seconds("REPLICA_CAPTURE")


def test_converter_percentage_is_streamed_to_job_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_EC2_STORAGE_ROOT", str(tmp_path))
    provider = NodeExecutor()
    code = (
        "import sys,time;sys.stdout.write('(42.00/100%)');sys.stdout.flush();"
        "time.sleep(.02);sys.stdout.write('(100.00/100%)')"
    )

    returncode, _ = provider.run_conversion_with_progress(
        [sys.executable, "-c", code], "snapshot-job"
    )

    assert returncode == 0
    assert provider.progress["snapshot-job"] == (55, "Compressing root disk (100%)")


def test_agent_gc_only_removes_namespaced_untracked_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_EC2_STORAGE_ROOT", str(tmp_path))
    provider = NodeExecutor()
    desired = "localec2-user-kept"
    orphan = "localec2-user-orphan"
    (provider.root / desired).mkdir()
    (provider.root / orphan).mkdir()
    (provider.root / orphan / "disk").write_bytes(b"orphan")
    point_id = "11111111-1111-1111-1111-111111111111"
    volume_id = "22222222-2222-2222-2222-222222222222"
    ami_id = "33333333-3333-3333-3333-333333333333"
    (provider.root.parent / "recovery-points").mkdir()
    (provider.root.parent / "recovery-points" / f"{point_id}.qcow2").write_bytes(b"point")
    (provider.root.parent / "volumes").mkdir()
    (provider.root.parent / "volumes" / f"{volume_id}.qcow2").write_bytes(b"volume")
    (provider.root.parent / f"ami-{ami_id}.box").write_bytes(b"ami")

    def fake_run(command, **kwargs):
        if command[:4] == ["virsh", "--connect", "qemu:///system", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{desired}_default\n{orphan}_default\nforeign-domain\n",
                stderr="",
            )
        if command[-2:] == ["box", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"localec2/ami-{ami_id} (libvirt, 0)\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    destroyed: list[str] = []
    monkeypatch.setattr("app.agent.subprocess.run", fake_run)
    monkeypatch.setattr(provider, "destroy_orphan_domain", destroyed.append)
    inventory = {
        "instance_names": [desired],
        "recovery_point_ids": [],
        "volume_ids": [],
        "ami_ids": [],
    }
    first = provider.garbage_collect(inventory, 300)
    assert sum(first["removed"].values()) == 0
    provider.orphan_first_seen = {
        key: value - 301 for key, value in provider.orphan_first_seen.items()
    }

    second = provider.garbage_collect(inventory, 300)

    assert destroyed == [orphan]
    assert (provider.root / desired).is_dir()
    assert not (provider.root / orphan).exists()
    assert second["removed"] == {
        "domains": 1,
        "workspaces": 1,
        "snapshots": 1,
        "volumes": 1,
        "amis": 2,
    }


def test_node_observed_status_uses_approval_drain_and_heartbeat_age():
    node = CloudNode(user_id=uuid4(), name="compute-01", agent_token_hash="hash")
    now = datetime.now(UTC)
    assert observed_status(node, now) == NodeStatus.PENDING

    node.approved = True
    assert observed_status(node, now) == NodeStatus.OFFLINE

    node.last_seen_at = now
    assert observed_status(node, now) == NodeStatus.ONLINE

    node.schedulable = False
    assert observed_status(node, now) == NodeStatus.DRAINING


def test_libvirt_shutoff_maps_to_stopped():
    assert provider_status("Shutoff") == InstanceStatus.STOPPED


def test_autoscaling_error_instances_do_not_satisfy_desired_capacity():
    user_id = uuid4()
    instances = [
        Instance(
            user_id=user_id,
            multipass_name=f"vm-{status.value.lower()}",
            display_name=status.value,
            cpu_count=1,
            memory_mb=1024,
            disk_gb=5,
            image="24.04",
            status=status,
        )
        for status in (
            InstanceStatus.RUNNING,
            InstanceStatus.PROVISIONING,
            InstanceStatus.ERROR,
            InstanceStatus.TERMINATING,
        )
    ]

    assert [item.status for item in capacity_instances(instances)] == [
        InstanceStatus.RUNNING,
        InstanceStatus.PROVISIONING,
    ]


def test_platform_admin_is_an_explicit_role():
    user = User(
        email="customer@example.com",
        display_name="Customer",
        password_hash="not-used",
    )
    assert not has_platform_admin_access(user)
    user.is_platform_admin = True
    assert has_platform_admin_access(user)


async def test_load_balancer_round_robin_selects_each_healthy_target():
    user_id = uuid4()
    group_id = uuid4()
    lb = LoadBalancer(
        user_id=user_id,
        name="web",
        autoscaling_group_id=group_id,
        algorithm="round_robin",
    )
    targets = [
        Instance(
            user_id=user_id,
            multipass_name=f"vm-{index}",
            display_name=f"vm-{index}",
            cpu_count=1,
            memory_mb=1024,
            disk_gb=5,
            image="24.04",
            ipv4_address=f"10.0.0.{index}",
            status="RUNNING",
        )
        for index in (2, 3)
    ]
    for target in targets:
        load_balancing.health[(lb.id, target.id)] = True

    first = await load_balancing.select_target(lb, targets)
    second = await load_balancing.select_target(lb, targets)

    assert first and second and first.id != second.id
    await load_balancing.release_target(lb.id, first.id)
    await load_balancing.release_target(lb.id, second.id)


async def test_load_balancer_least_connections_avoids_busy_target():
    user_id = uuid4()
    lb = LoadBalancer(
        user_id=user_id,
        name="web",
        autoscaling_group_id=uuid4(),
        algorithm="least_connections",
    )
    targets = [
        Instance(
            user_id=user_id,
            multipass_name=f"vm-{index}",
            display_name=f"vm-{index}",
            cpu_count=1,
            memory_mb=1024,
            disk_gb=5,
            image="24.04",
            ipv4_address=f"10.0.0.{index}",
            status="RUNNING",
        )
        for index in (2, 3)
    ]
    for target in targets:
        load_balancing.health[(lb.id, target.id)] = True
    load_balancing.active_connections[(lb.id, targets[0].id)] = 4

    selected = await load_balancing.select_target(lb, targets)

    assert selected and selected.id == targets[1].id
    await load_balancing.release_target(lb.id, selected.id)


def test_haproxy_configuration_uses_tailscale_targets_and_selected_policy():
    user_id = uuid4()
    lb = LoadBalancer(
        user_id=user_id,
        name="production",
        target_group_id=uuid4(),
        algorithm="least_connections",
        target_port=8080,
        health_check_path="/health",
    )
    target = Instance(
        user_id=user_id,
        multipass_name="vm-web",
        display_name="web",
        cpu_count=1,
        memory_mb=1024,
        disk_gb=5,
        image="24.04",
        tailscale_ipv4_address="100.64.0.10",
    )

    config = load_balancing.haproxy_configuration(lb, [target])

    assert "balance leastconn" in config
    assert "option httpchk GET /health" in config
    assert "100.64.0.10:8080 check" in config


def test_manual_target_group_keeps_explicit_instance_registration():
    instance_ids = [uuid4(), uuid4()]
    group = TargetGroup(
        user_id=uuid4(),
        name="manual-web",
        registration_type="manual",
        instance_ids=instance_ids,
    )

    assert group.instance_ids == instance_ids
    assert group.autoscaling_group_id is None
