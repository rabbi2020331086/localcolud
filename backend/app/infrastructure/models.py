import enum
import ipaddress
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def now() -> datetime:
    return datetime.now(UTC)


class InstanceStatus(enum.StrEnum):
    PROVISIONING = "PROVISIONING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    HIBERNATING = "HIBERNATING"
    HIBERNATED = "HIBERNATED"
    TERMINATING = "TERMINATING"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class OperationStatus(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OperationType(enum.StrEnum):
    LAUNCH = "LAUNCH"
    START = "START"
    STOP = "STOP"
    HIBERNATE = "HIBERNATE"
    TERMINATE = "TERMINATE"
    SCALE_OUT = "SCALE_OUT"
    SCALE_IN = "SCALE_IN"
    RECONCILE = "RECONCILE"


class AmiStatus(enum.StrEnum):
    AVAILABLE = "AVAILABLE"


class NodeStatus(enum.StrEnum):
    PENDING = "PENDING"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"


class ComputeProvider(enum.StrEnum):
    MULTIPASS = "multipass"
    VAGRANT_LIBVIRT = "vagrant-libvirt"


class PlatformRole(enum.StrEnum):
    ADMINISTRATOR = "ADMINISTRATOR"
    OPERATIONS = "OPERATIONS"
    MARKETING = "MARKETING"
    ANALYST_FINANCE = "ANALYST_FINANCE"
    DEVELOPER = "DEVELOPER"


class StoredModel(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=now)


class User(StoredModel):
    email: str
    display_name: str
    password_hash: str
    is_active: bool = True
    is_platform_admin: bool = False
    platform_roles: list[PlatformRole] = Field(default_factory=list)
    must_change_password: bool = False
    updated_at: datetime = Field(default_factory=now)


class RefreshToken(StoredModel):
    user_id: UUID
    jti: str
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None = None


class Instance(StoredModel):
    user_id: UUID
    multipass_name: str
    display_name: str
    description: str = ""
    status: InstanceStatus = InstanceStatus.PROVISIONING
    ipv4_address: str | None = None
    cpu_count: int
    memory_mb: int
    disk_gb: int
    instance_type: str = "custom"
    image: str
    ami_id: UUID | None = None
    ssh_username: str = "ubuntu"
    key_fingerprint: str | None = None
    has_key_pair: bool = False
    key_pair_id: UUID | None = None
    security_group_ids: list[UUID] = Field(default_factory=list)
    autoscaling_group_id: UUID | None = None
    node_id: UUID | None = None
    provider_name: ComputeProvider = ComputeProvider.MULTIPASS
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=now)
    deleted_at: datetime | None = None


class CloudNode(StoredModel):
    user_id: UUID
    name: str
    availability_zone: str = "local-region-1a"
    provider_name: ComputeProvider = ComputeProvider.VAGRANT_LIBVIRT
    status: NodeStatus = NodeStatus.PENDING
    approved: bool = False
    schedulable: bool = True
    agent_token_hash: str
    agent_version: str | None = None
    hostname: str | None = None
    management_address: str | None = None
    cpu_total: int = 0
    cpu_allocated: int = 0
    memory_total_mb: int = 0
    memory_allocated_mb: int = 0
    storage_total_gb: int = 0
    storage_allocated_gb: int = 0
    labels: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    network_interfaces: list[dict[str, str]] = Field(default_factory=list)
    virtual_networks: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    updated_at: datetime = Field(default_factory=now)


class Operation(StoredModel):
    user_id: UUID
    instance_id: UUID | None = None
    operation_type: OperationType
    status: OperationStatus = OperationStatus.PENDING
    progress: int = 0
    message: str = "Queued"
    request_payload: dict[str, Any] = Field(default_factory=dict)
    result_payload: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class OneTimeSecret(StoredModel):
    user_id: UUID
    instance_id: UUID
    token_hash: str
    encrypted_value: bytes
    filename: str
    expires_at: datetime
    consumed_at: datetime | None = None


class LaunchTemplate(StoredModel):
    user_id: UUID
    name: str
    description: str = ""
    image: str
    cpu_count: int
    memory_mb: int
    disk_gb: int
    user_data: str = ""
    key_pair_id: UUID | None = None
    create_key_pair: bool = False
    updated_at: datetime = Field(default_factory=now)


class AutoScalingGroup(StoredModel):
    user_id: UUID
    name: str
    launch_template_id: UUID
    minimum_size: int
    desired_size: int
    maximum_size: int
    target_cpu_percent: float = 50
    scale_out_threshold: float = 70
    scale_in_threshold: float = 25
    evaluation_periods: int = 3
    cooldown_seconds: int = 300
    enabled: bool = True
    last_scaling_at: datetime | None = None
    updated_at: datetime = Field(default_factory=now)


class LoadBalancer(StoredModel):
    user_id: UUID
    name: str
    autoscaling_group_id: UUID
    algorithm: Literal["round_robin", "least_connections"] = "round_robin"
    target_port: int = 80
    health_check_path: str = "/"
    health_check_interval_seconds: int = 10
    enabled: bool = True
    updated_at: datetime = Field(default_factory=now)


class ScalingEvent(StoredModel):
    autoscaling_group_id: UUID
    action: str
    previous_size: int
    new_size: int
    reason: str
    average_cpu: float | None = None
    status: str


class KeyPair(StoredModel):
    user_id: UUID
    name: str
    public_key: str
    encrypted_private_key: bytes
    fingerprint: str


class SecurityRule(BaseModel):
    protocol: Literal["tcp", "udp"] = "tcp"
    from_port: int = Field(ge=1, le=65535)
    to_port: int = Field(ge=1, le=65535)
    source: str = "0.0.0.0/0"
    description: str = ""

    @model_validator(mode="after")
    def valid_range_and_source(self) -> "SecurityRule":
        if self.from_port > self.to_port:
            raise ValueError("from_port must not exceed to_port")
        ipaddress.ip_network(self.source, strict=False)
        return self


class SecurityGroup(StoredModel):
    user_id: UUID
    name: str
    description: str = ""
    ingress_rules: list[SecurityRule] = Field(default_factory=list)
    egress_rules: list[SecurityRule] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=now)


class Volume(StoredModel):
    user_id: UUID
    name: str
    size_gb: int
    host_path: str
    attached_instance_id: UUID | None = None
    mount_path: str | None = None
    updated_at: datetime = Field(default_factory=now)


class Ami(StoredModel):
    user_id: UUID
    name: str
    description: str = ""
    source_instance_id: UUID
    backing_instance_name: str
    status: AmiStatus = AmiStatus.AVAILABLE
    image: str
    cpu_count: int
    memory_mb: int
    disk_gb: int
    updated_at: datetime = Field(default_factory=now)
