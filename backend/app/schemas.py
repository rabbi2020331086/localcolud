import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.infrastructure.models import (
    AmiStatus,
    ComputeProvider,
    InstanceStatus,
    NodeStatus,
    OperationStatus,
    OperationType,
    PlatformRole,
    SecurityRule,
)

INSTANCE_TYPES = {
    "t2.micro": (1, 1024),
    "t2.small": (1, 2048),
    "t2.medium": (2, 4096),
    "t4.micro": (2, 1024),
    "t4.small": (2, 2048),
    "t4.medium": (2, 4096),
    "m5.large": (2, 8192),
    "m5.xlarge": (4, 16384),
}


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(OrmModel):
    id: UUID
    # Platform bootstrap identities may use an internal, non-deliverable address.
    # Registration and team creation inputs still require EmailStr validation.
    email: str
    display_name: str
    is_platform_admin: bool
    platform_roles: list[PlatformRole]
    must_change_password: bool
    can_view_agent_code: bool = False
    can_edit_agent_code: bool = False


class RegisterIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=10, max_length=256)

    @field_validator("password")
    @classmethod
    def password_rules(cls, value: str) -> str:
        if (
            not re.search(r"[A-Z]", value)
            or not re.search(r"[a-z]", value)
            or not re.search(r"\d", value)
        ):
            raise ValueError("Password requires uppercase, lowercase, and number")
        return value


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


class TeamMemberUpdate(BaseModel):
    platform_roles: list[PlatformRole] = Field(max_length=5)
    is_active: bool = True
    can_view_agent_code: bool = False
    can_edit_agent_code: bool = False


class TeamMemberCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    temporary_password: str = Field(min_length=10, max_length=256)
    platform_roles: list[PlatformRole] = Field(default_factory=list, max_length=5)


class TeamMemberOut(UserOut):
    is_active: bool
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class InstanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9 _-]+$")
    description: str = Field(default="", max_length=500)
    launch_template_id: UUID | None = None
    image: str = Field(default="24.04", max_length=50)
    ami_id: UUID | None = None
    cpu_count: int = Field(default=2, ge=1, le=8)
    memory_mb: int = Field(default=2048, ge=512, le=16384)
    disk_gb: int = Field(default=10, ge=5, le=100)
    create_key_pair: bool = True
    instance_type: str = "t2.small"
    key_pair_id: UUID | None = None
    security_group_ids: list[UUID] = Field(default_factory=list, max_length=5)
    availability_zone: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9.-]+$")
    user_data: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def apply_instance_type(self) -> "InstanceCreate":
        if self.instance_type == "custom":
            return self
        preset = INSTANCE_TYPES.get(self.instance_type)
        if not preset:
            raise ValueError("Unknown instance type")
        self.cpu_count, self.memory_mb = preset
        return self

    @field_validator("description")
    @classmethod
    def no_controls(cls, value: str) -> str:
        if any(ord(c) < 32 and c not in "\r\n\t" for c in value):
            raise ValueError("Control characters are not allowed")
        return value

    @field_validator("user_data")
    @classmethod
    def valid_user_data(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("User data cannot contain null bytes")
        return value


class InstanceOut(OrmModel):
    id: UUID
    display_name: str
    description: str
    status: InstanceStatus
    ipv4_address: str | None
    tailscale_ipv4_address: str | None
    tailscale_dns_name: str | None
    public_endpoint: str | None
    public_endpoint_status: str
    cpu_count: int
    memory_mb: int
    disk_gb: int
    instance_type: str
    image: str
    ami_id: UUID | None
    ssh_username: str
    key_fingerprint: str | None
    has_key_pair: bool
    key_pair_id: UUID | None
    security_group_ids: list[UUID]
    autoscaling_group_id: UUID | None
    availability_zone: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AcceptedOut(BaseModel):
    operation_id: UUID
    instance_id: UUID
    status: OperationStatus


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    availability_zone: str = Field(
        default="local-region-1a", min_length=1, max_length=80, pattern=r"^[a-z0-9.-]+$"
    )
    provider_name: ComputeProvider = ComputeProvider.VAGRANT_LIBVIRT
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("provider_name")
    @classmethod
    def distributed_provider_only(cls, value: ComputeProvider) -> ComputeProvider:
        if value != ComputeProvider.VAGRANT_LIBVIRT:
            raise ValueError("Compute nodes must use Vagrant with libvirt/KVM")
        return value

    @field_validator("labels")
    @classmethod
    def safe_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 30:
            raise ValueError("A node may have at most 30 labels")
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
                raise ValueError("Invalid node label name")
            if len(item) > 128:
                raise ValueError("Node label values may not exceed 128 characters")
        return value


class NodeHeartbeat(BaseModel):
    agent_version: str = Field(min_length=1, max_length=40)
    hostname: str = Field(min_length=1, max_length=255)
    management_address: str | None = Field(default=None, max_length=255)
    provider_name: ComputeProvider
    cpu_total: int = Field(ge=1, le=4096)
    memory_total_mb: int = Field(ge=256)
    storage_total_gb: int = Field(ge=1)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    network_interfaces: list[dict[str, str]] = Field(default_factory=list, max_length=100)
    virtual_networks: list[str] = Field(default_factory=list, max_length=50)


class NodeUpdate(BaseModel):
    approved: bool | None = None
    schedulable: bool | None = None
    availability_zone: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9.-]+$"
    )
    labels: dict[str, str] | None = None
    cpu_reserved: int | None = Field(default=None, ge=0, le=4096)
    memory_reserved_mb: int | None = Field(default=None, ge=0)
    storage_reserved_gb: int | None = Field(default=None, ge=0)

    @field_validator("labels")
    @classmethod
    def safe_labels(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return None if value is None else NodeCreate.safe_labels(value)


class HostCommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=20000)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    run_as_root: bool = False

    @field_validator("command")
    @classmethod
    def no_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Command cannot contain null bytes")
        return value


class AgentUpdateIn(BaseModel):
    control_plane_url: str = Field(min_length=12, max_length=500)

    @field_validator("control_plane_url")
    @classmethod
    def valid_control_plane_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s#]+/api/v1", normalized):
            raise ValueError("Control-plane URL must look like https://host/api/v1")
        return normalized


class NodeOut(OrmModel):
    id: UUID
    name: str
    availability_zone: str
    provider_name: ComputeProvider
    status: NodeStatus
    approved: bool
    schedulable: bool
    agent_version: str | None
    hostname: str | None
    management_address: str | None
    cpu_total: int
    cpu_reserved: int
    cpu_allocated: int
    memory_total_mb: int
    memory_reserved_mb: int
    memory_allocated_mb: int
    storage_total_gb: int
    storage_reserved_gb: int
    storage_allocated_gb: int
    labels: dict[str, str]
    capabilities: list[str]
    network_interfaces: list[dict[str, str]]
    virtual_networks: list[str]
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NodeEnrollmentOut(NodeOut):
    enrollment_token: str


class ZoneCapacityOut(BaseModel):
    availability_zone: str
    available: bool


class AgentJobOut(OrmModel):
    id: UUID
    node_id: UUID
    instance_id: UUID | None
    job_type: str
    payload: dict[str, object]
    attempts: int
    created_at: datetime


class AgentJobResult(BaseModel):
    succeeded: bool
    result_payload: dict[str, object] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=4000)


class OperationOut(OrmModel):
    id: UUID
    instance_id: UUID | None
    operation_type: OperationType
    status: OperationStatus
    progress: int
    message: str
    result_payload: dict[str, object]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    image: str = "24.04"
    cpu_count: int = Field(default=2, ge=1, le=8)
    # Vagrant/libvirt Ubuntu boxes frequently fail to finish their first boot
    # and SSH provisioning with only 512 MiB, leaving launches at progress 35.
    memory_mb: int = Field(default=2048, ge=1024, le=16384)
    disk_gb: int = Field(default=10, ge=5, le=100)
    instance_type: str = Field(default="custom", min_length=1, max_length=40)
    availability_zones: list[str] = Field(default_factory=list, max_length=20)
    security_group_ids: list[UUID] = Field(default_factory=list, max_length=20)
    user_data: str = Field(default="", max_length=10000)
    key_pair_id: UUID | None = None
    create_key_pair: bool = False

    @model_validator(mode="after")
    def one_key_source(self) -> "TemplateIn":
        if self.key_pair_id and self.create_key_pair:
            raise ValueError("Choose a reusable key or one-time keys, not both")
        return self

    @field_validator("availability_zones")
    @classmethod
    def valid_zones(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", value):
                raise ValueError("Availability zones must use safe cloud identifiers")
        return list(dict.fromkeys(values))


class TemplateOut(OrmModel):
    id: UUID
    name: str
    description: str
    image: str
    cpu_count: int
    memory_mb: int
    disk_gb: int
    instance_type: str
    availability_zones: list[str]
    security_group_ids: list[UUID]
    user_data: str
    key_pair_id: UUID | None
    create_key_pair: bool
    created_at: datetime
    updated_at: datetime


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    launch_template_id: UUID
    minimum_size: int = Field(ge=0)
    desired_size: int = Field(ge=0)
    maximum_size: int = Field(ge=0, le=20)
    target_cpu_percent: float = Field(default=50, ge=1, le=100)
    scale_out_threshold: float = Field(default=70, ge=1, le=100)
    scale_in_threshold: float = Field(default=25, ge=0, le=99)
    evaluation_periods: int = Field(default=3, ge=1, le=20)
    cooldown_seconds: int = Field(default=300, ge=30)
    enabled: bool = True

    @model_validator(mode="after")
    def sizes(self) -> "GroupIn":
        if not self.minimum_size <= self.desired_size <= self.maximum_size:
            raise ValueError("minimum_size <= desired_size <= maximum_size is required")
        if self.scale_in_threshold >= self.scale_out_threshold:
            raise ValueError("scale-in threshold must be below scale-out threshold")
        return self


class GroupOut(OrmModel):
    id: UUID
    name: str
    launch_template_id: UUID
    minimum_size: int
    desired_size: int
    maximum_size: int
    target_cpu_percent: float
    scale_out_threshold: float
    scale_in_threshold: float
    evaluation_periods: int
    cooldown_seconds: int
    enabled: bool
    last_scaling_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DesiredIn(BaseModel):
    desired_size: int = Field(ge=0, le=20)


class DeleteGroupIn(BaseModel):
    terminate_managed_instances: bool


class TargetGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    protocol: str = Field(default="HTTP", pattern=r"^HTTP$")
    port: int = Field(default=80, ge=1, le=65535)
    target_type: str = Field(default="instance", pattern=r"^instance$")
    registration_type: str = Field(default="manual", pattern=r"^(manual|autoscaling)$")
    instance_ids: list[UUID] = Field(default_factory=list, max_length=100)
    autoscaling_group_id: UUID | None = None
    health_check_path: str = Field(default="/", min_length=1, max_length=200)
    health_check_interval_seconds: int = Field(default=10, ge=5, le=300)

    @model_validator(mode="after")
    def valid_registration(self) -> "TargetGroupIn":
        if self.registration_type == "manual" and not self.instance_ids:
            raise ValueError("Select at least one running instance")
        if self.registration_type == "autoscaling" and not self.autoscaling_group_id:
            raise ValueError("Select an autoscaling group")
        if not self.health_check_path.startswith("/") or ".." in self.health_check_path:
            raise ValueError("Health-check path must be a safe absolute path")
        return self


class TargetGroupOut(TargetGroupIn, OrmModel):
    id: UUID
    registered_targets: int
    healthy_targets: int
    health_status: str
    created_at: datetime
    updated_at: datetime


class LoadBalancerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    target_group_id: UUID
    algorithm: str = Field(
        default="round_robin",
        pattern=r"^(round_robin|least_connections|least_cpu|random|source_ip_hash)$",
    )
    target_port: int = Field(default=80, ge=1, le=65535)
    health_check_path: str = Field(default="/", min_length=1, max_length=200)
    health_check_interval_seconds: int = Field(default=10, ge=5, le=300)
    enabled: bool = True

    @field_validator("health_check_path")
    @classmethod
    def valid_health_path(cls, value: str) -> str:
        if not value.startswith("/") or "?" in value or "#" in value or ".." in value:
            raise ValueError("Health-check path must be a safe absolute path")
        return value


class LoadBalancerOut(LoadBalancerIn, OrmModel):
    id: UUID
    endpoint_path: str
    public_endpoint: str | None
    public_endpoint_status: str
    healthy_targets: int
    total_targets: int
    created_at: datetime
    updated_at: datetime


class LoadBalancerTargetOut(BaseModel):
    instance_id: UUID
    name: str
    address: str | None
    availability_zone: str
    status: str
    healthy: bool
    active_connections: int
    cpu_percent: float | None = None


class KeyPairIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")


class KeyPairOut(OrmModel):
    id: UUID
    name: str
    public_key: str
    fingerprint: str
    created_at: datetime


class SecurityGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    description: str = Field(default="", max_length=500)
    ingress_rules: list[SecurityRule] = Field(default_factory=list, max_length=50)
    egress_rules: list[SecurityRule] = Field(default_factory=list, max_length=50)


class SecurityGroupOut(SecurityGroupIn, OrmModel):
    id: UUID
    created_at: datetime
    updated_at: datetime


class VolumeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    size_gb: int = Field(default=10, ge=1, le=1024)
    node_id: UUID


class VolumeStorageNodeOut(BaseModel):
    id: UUID
    name: str
    availability_zone: str
    storage_total_gb: int
    storage_allocated_gb: int


class VolumeOut(OrmModel):
    id: UUID
    name: str
    size_gb: int
    availability_zone: str
    storage_backend: str
    origin_node_id: UUID | None
    origin_node_name: str | None = None
    current_node_id: UUID | None = None
    current_node_name: str | None = None
    attachment_zone: str | None = None
    attached_instance_id: UUID | None
    device_name: str | None
    mount_path: str | None
    created_at: datetime
    updated_at: datetime
    used_bytes: int = 0
    available_bytes: int = 0
    usage_percent: float = 0


class VolumeAttachIn(BaseModel):
    instance_id: UUID
    mount_path: str = Field(default="/mnt/data", pattern=r"^/[-A-Za-z0-9_./]+$")


class AmiIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _.-]+$")
    description: str = Field(default="", max_length=500)
    source_instance_id: UUID


class AmiOut(OrmModel):
    id: UUID
    name: str
    description: str
    source_instance_id: UUID
    status: AmiStatus
    image: str
    cpu_count: int
    memory_mb: int
    disk_gb: int
    created_at: datetime
    updated_at: datetime
