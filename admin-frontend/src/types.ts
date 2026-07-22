export type User = {
  id: string;
  email: string;
  display_name: string;
  is_platform_admin: boolean;
  platform_roles: PlatformRole[];
  must_change_password: boolean;
  can_view_agent_code: boolean;
  can_edit_agent_code: boolean;
};

export type PlatformRole =
  | "ADMINISTRATOR"
  | "OPERATIONS"
  | "MARKETING"
  | "ANALYST_FINANCE"
  | "DEVELOPER";

export type TeamMember = User & { is_active: boolean; created_at: string };

export type AgentWorkspace = {
  draft: string;
  draft_version: string;
  bundled_source: string;
  bundled_version: string;
  published_version: string;
  published_at: string | null;
  can_edit: boolean;
  can_deploy: boolean;
  live_release_id: string | null;
  releases: AgentRelease[];
};

export type AgentRelease = {
  id: string;
  version: string;
  sha256: string;
  created_at: string;
  created_by: string;
  action: "DEPLOY" | "PREVIOUS";
};

export type CloudNode = {
  id: string;
  name: string;
  availability_zone: string;
  provider_name: "multipass" | "vagrant-libvirt";
  status: "PENDING" | "ONLINE" | "OFFLINE" | "DRAINING";
  approved: boolean;
  schedulable: boolean;
  agent_version: string | null;
  hostname: string | null;
  management_address: string | null;
  cpu_total: number;
  cpu_reserved: number;
  cpu_allocated: number;
  memory_total_mb: number;
  memory_reserved_mb: number;
  memory_allocated_mb: number;
  storage_total_gb: number;
  storage_reserved_gb: number;
  storage_allocated_gb: number;
  capabilities: string[];
  network_interfaces: { name: string; family: string; address: string }[];
  virtual_networks: string[];
  last_seen_at: string | null;
};

export type Enrollment = CloudNode & { enrollment_token: string };

export type Overview = {
  users: number; nodes: number; online_nodes: number; pending_nodes: number;
  instances: number; running_instances: number; failed_instances: number;
  active_operations: number; failed_operations: number;
  cpu_total: number; cpu_allocated: number;
  memory_total_mb: number; memory_allocated_mb: number;
  storage_total_gb: number; storage_allocated_gb: number; zones: string[];
};

export type Workload = {
  id: string; name: string; status: string; owner: string; node: string | null;
  node_id: string | null; zone: string | null; provider: string; ipv4_address: string | null;
  instance_type: string; last_error: string | null; autoscaling_group_id: string | null;
  cpu_count: number; memory_mb: number; disk_gb: number; created_at: string;
  updated_at: string;
};

export type AdminVolume = {
  id: string; name: string; owner: string; size_gb: number;
  availability_zone: string; storage_backend: string;
  origin_node: string | null; current_node: string | null; last_node: string | null;
  attached_instance: string | null; attached_instance_id: string | null;
  device_name: string | null; mount_path: string | null;
  canonical_present: boolean; created_at: string; updated_at: string;
};

export type NodeContribution = {
  node_id: string; node: string; zone: string; status: string;
  cpu_contributed: number; cpu_allocated: number;
  memory_contributed_mb: number; memory_allocated_mb: number;
  storage_contributed_gb: number; storage_allocated_gb: number;
  instances: number; attached_volumes: number; attached_volume_gb: number;
  originated_volumes: number;
};

export type InstanceDiagnostics = {
  instance_id: string;
  inspect: { name: string; state: string; ipv4_address: string | null };
  metrics: {
    cpu_percent: number;
    memory_percent: number;
    root_disk_percent: string;
  } | null;
  recent_jobs: {
    id: string; type: string; status: string; error: string | null; created_at: string;
  }[];
};

export type AdminOperation = {
  id: string; instance_id: string | null; owner: string; type: string; status: string;
  progress: number; message: string; error_message: string | null; created_at: string;
  started_at: string | null; finished_at: string | null;
};
