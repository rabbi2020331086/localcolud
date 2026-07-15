export type User = {
  id: string;
  email: string;
  display_name: string;
  is_platform_admin: boolean;
  platform_roles: string[];
  must_change_password: boolean;
};
export type Instance = {
  id: string;
  display_name: string;
  description: string;
  status:
    | "PROVISIONING"
    | "STARTING"
    | "RUNNING"
    | "STOPPING"
    | "STOPPED"
    | "HIBERNATING"
    | "HIBERNATED"
    | "TERMINATING"
    | "ERROR"
    | "UNKNOWN";
  ipv4_address: string | null;
  cpu_count: number;
  memory_mb: number;
  disk_gb: number;
  instance_type: string;
  image: string;
  ami_id: string | null;
  ssh_username: string;
  key_fingerprint: string | null;
  has_key_pair: boolean;
  key_pair_id: string | null;
  security_group_ids: string[];
  autoscaling_group_id: string | null;
  availability_zone: string;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};
export type ZoneCapacity = {
  availability_zone: string;
  available: boolean;
};
export type Operation = {
  id: string;
  instance_id: string | null;
  operation_type: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  progress: number;
  message: string;
  result_payload: Record<string, string>;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};
export type Template = {
  id: string;
  name: string;
  description: string;
  image: string;
  cpu_count: number;
  memory_mb: number;
  disk_gb: number;
  user_data: string;
  key_pair_id: string | null;
  create_key_pair: boolean;
};
export type Group = {
  id: string;
  name: string;
  launch_template_id: string;
  minimum_size: number;
  desired_size: number;
  maximum_size: number;
  target_cpu_percent: number;
  scale_out_threshold: number;
  scale_in_threshold: number;
  evaluation_periods: number;
  cooldown_seconds: number;
  enabled: boolean;
  last_scaling_at: string | null;
};
export type LoadBalancer = {
  id: string;
  name: string;
  autoscaling_group_id: string;
  algorithm: "round_robin" | "least_connections";
  target_port: number;
  health_check_path: string;
  health_check_interval_seconds: number;
  enabled: boolean;
  healthy_targets: number;
  total_targets: number;
  created_at: string;
  updated_at: string;
};
export type LoadBalancerTarget = {
  instance_id: string;
  name: string;
  address: string | null;
  status: string;
  healthy: boolean;
  active_connections: number;
  cpu_percent: number | null;
};
export type Metric = {
  timestamp: string;
  cpu_percent: number;
  memory_percent: number;
  disk_percent: number;
  network_rx_bytes: number;
  network_tx_bytes: number;
};
export type KeyPair = {
  id: string;
  name: string;
  public_key: string;
  fingerprint: string;
  created_at: string;
};
export type SecurityRule = {
  protocol: "tcp" | "udp";
  from_port: number;
  to_port: number;
  source: string;
  description: string;
};
export type SecurityGroup = {
  id: string;
  name: string;
  description: string;
  ingress_rules: SecurityRule[];
  egress_rules: SecurityRule[];
  created_at: string;
  updated_at: string;
};
export type Volume = {
  id: string;
  name: string;
  size_gb: number;
  attached_instance_id: string | null;
  mount_path: string | null;
  created_at: string;
  updated_at: string;
  used_bytes: number;
  available_bytes: number;
  usage_percent: number;
};
export type Ami = {
  id: string;
  name: string;
  description: string;
  source_instance_id: string;
  status: "AVAILABLE";
  image: string;
  cpu_count: number;
  memory_mb: number;
  disk_gb: number;
  created_at: string;
  updated_at: string;
};
