export type User = {
  id: string;
  email: string;
  display_name: string;
  is_platform_admin: boolean;
  platform_roles: PlatformRole[];
  must_change_password: boolean;
};

export type PlatformRole =
  | "ADMINISTRATOR"
  | "OPERATIONS"
  | "MARKETING"
  | "ANALYST_FINANCE"
  | "DEVELOPER";

export type TeamMember = User & { is_active: boolean; created_at: string };

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
  cpu_allocated: number;
  memory_total_mb: number;
  memory_allocated_mb: number;
  storage_total_gb: number;
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
  zone: string | null; provider: string; ipv4_address: string | null;
  cpu_count: number; memory_mb: number; disk_gb: number; created_at: string;
};

export type AdminOperation = {
  id: string; instance_id: string | null; owner: string; type: string; status: string;
  progress: number; message: string; error_message: string | null; created_at: string;
  started_at: string | null; finished_at: string | null;
};
