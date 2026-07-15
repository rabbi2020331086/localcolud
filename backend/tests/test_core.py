from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.api.dependencies import has_platform_admin_access
from app.api.routes.nodes import observed_status
from app.core.security import create_jwt, decode_jwt, hash_password, verify_password
from app.infrastructure.models import CloudNode, Instance, LoadBalancer, NodeStatus, User
from app.infrastructure.vm import FakeProvider, VagrantProvider, safe_name
from app.services import load_balancing


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


def test_vagrant_provider_renders_libvirt_machine(tmp_path: Path):
    provider = VagrantProvider(root=tmp_path)

    content = provider.render_vagrantfile("web-01", "24.04", 4, 8192, None)

    assert 'config.vm.box = "bento/ubuntu-24.04"' in content
    assert 'config.vm.provider "libvirt"' in content
    assert "provider.cpus = 4" in content
    assert "provider.memory = 8192" in content


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


def test_platform_admin_is_an_explicit_role():
    user = User(
        email="customer@example.com",
        display_name="Customer",
        password_hash="not-used",
    )
    assert not has_platform_admin_access(user)
    user.is_platform_admin = True
    assert has_platform_admin_access(user)


async def test_fake_provider_clones_machine_images():
    provider = FakeProvider()
    source = await provider.launch("source", "24.04", 1, 1024, 10)
    await provider.stop(source.name)

    clone = await provider.clone(source.name, "clone", 2, 2048, 15)

    assert clone.name == "clone"
    assert clone.state == "Running"
    assert (clone.cpu_count, clone.memory_mb, clone.disk_gb) == (2, 2048, 15)


async def test_fake_provider_hibernates_and_resumes_instance():
    provider = FakeProvider()
    instance = await provider.launch("work", "24.04", 1, 1024, 10)

    await provider.suspend(instance.name)
    assert instance.state == "Suspended"

    await provider.start(instance.name)
    assert instance.state == "Running"


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
