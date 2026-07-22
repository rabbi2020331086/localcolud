import {
  Activity,
  ArrowLeft,
  BarChart3,
  Cpu,
  ExternalLink,
  Gauge,
  HardDrive,
  KeyRound,
  ListChecks,
  MemoryStick,
  Monitor,
  Network,
  Play,
  RotateCw,
  Server,
  Settings2,
  Snowflake,
  Plus,
  SquareTerminal,
  StopCircle,
  Trash2,
} from "lucide-react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, message } from "../api/client";
import {
  Button,
  Card,
  Empty,
  ErrorAlert,
  Loading,
  PageHeader,
  Status,
} from "../components/ui";
import {
  useInstanceAction,
  useAmis,
  useInstances,
  useKeyPairs,
  useOperations,
  useSecurityGroups,
  useTemplates,
} from "../hooks/data";
import type { Instance, Metric, Operation, ZoneCapacity } from "../types";
function useKeyDownloads(operations: Operation[]) {
  const handled = useRef(new Set<string>());
  useEffect(() => {
    for (const operation of operations) {
      const token = operation.result_payload.key_download_token;
      if (
        operation.status !== "SUCCEEDED" ||
        !token ||
        handled.current.has(token)
      )
        continue;
      handled.current.add(token);
      void api
        .get(`/key-downloads/${token}`, { responseType: "blob" })
        .then(({ data }) => {
          const url = URL.createObjectURL(data as Blob);
          const link = document.createElement("a");
          link.href = url;
          link.download =
            operation.result_payload.key_filename ?? "localec2-key.pem";
          link.click();
          URL.revokeObjectURL(url);
        });
    }
  }, [operations]);
}
async function downloadKey(token: string, filename: string) {
  const { data } = await api.get(`/key-downloads/${token}`, { responseType: "blob" });
  const url = URL.createObjectURL(data as Blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
export function InstancesPage() {
  const { data = [], isLoading, error } = useInstances();
  const { data: operations = [] } = useOperations();
  useKeyDownloads(operations);
  return (
    <>
      <PageHeader
        title="Instances"
        description="Virtual machines placed into administrator-managed availability zones."
        action={
          <Link className="button" to="/instances/new">
            <Plus />
            Launch instance
          </Link>
        }
      />
      {error && <ErrorAlert>{message(error)}</ErrorAlert>}
      <Card>
        {isLoading ? (
          <Loading />
        ) : data.length === 0 ? (
          <Empty
            title="No instances yet"
            detail="Launch after an administrator makes cloud capacity available."
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Instance ID</th>
                  <th>Type</th>
                  <th>Availability zone</th>
                  <th>State</th>
                  <th>Public endpoint</th>
                </tr>
              </thead>
              <tbody>
                {data.map((item) => {
                  return <tr key={item.id}>
                    <td>
                      <Link className="instance-name-link" to={`/instances/${item.id}`}>
                        <strong>{item.display_name}</strong>
                      </Link>
                    </td>
                    <td><Link to={`/instances/${item.id}`}><code>{item.id}</code></Link></td>
                    <td><strong>{item.instance_type}</strong><small>{item.cpu_count} vCPU · {(item.memory_mb / 1024).toFixed(1)} GB</small></td>
                    <td>{item.availability_zone}</td>
                    <td><Status value={item.status} /></td>
                    <td>
                      {item.public_endpoint ? (
                        <a className="endpoint-link" href={item.public_endpoint} target="_blank" rel="noreferrer">
                          <span>{item.tailscale_dns_name ?? item.public_endpoint}</span><ExternalLink />
                        </a>
                      ) : <small>{item.public_endpoint_status === "CONFIGURING" ? "Publishing…" : "—"}</small>}
                    </td>
                  </tr>;
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
type Launch = {
  name: string;
  description: string;
  launch_template_id: string;
  image: string;
  image_choice: string;
  cpu_count: number;
  memory_mb: number;
  disk_gb: number;
  create_key_pair: boolean;
  instance_type: string;
  key_pair_id: string;
  security_group_ids: string[];
  availability_zone: string;
  user_data: string;
};
export function LaunchPage() {
  const nav = useNavigate();
  const {
    register,
    watch,
    setValue,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Launch>({
    defaultValues: {
      launch_template_id: "",
      image: "24.04",
      image_choice: "base:24.04",
      cpu_count: 2,
      memory_mb: 2048,
      disk_gb: 10,
      create_key_pair: true,
      instance_type: "t2.small",
      key_pair_id: "",
      security_group_ids: [],
      availability_zone: "",
      user_data: "",
    },
  });
  const [cpu, memory, disk, instanceType, imageChoice] = watch([
    "cpu_count",
    "memory_mb",
    "disk_gb",
    "instance_type",
    "image_choice",
  ]);
  const preset = {
    "t2.micro": [1, 1024],
    "t2.small": [1, 2048],
    "t2.medium": [2, 4096],
    "t4.micro": [2, 1024],
    "t4.small": [2, 2048],
    "t4.medium": [2, 4096],
    "m5.large": [2, 8192],
    "m5.xlarge": [4, 16384],
  }[instanceType];
  const allocatedCpu = preset?.[0] ?? cpu;
  const allocatedMemory = preset?.[1] ?? memory;
  const keyPairs = useKeyPairs();
  const securityGroups = useSecurityGroups();
  const amis = useAmis();
  const templates = useTemplates();
  const selectedTemplateId = watch("launch_template_id");
  const selectedTemplate = templates.data?.find((template) => template.id === selectedTemplateId);
  const templateLocked = Boolean(selectedTemplate);
  useEffect(() => {
    if (!selectedTemplate) return;
    setValue("description", selectedTemplate.description, { shouldDirty: true });
    setValue("image", selectedTemplate.image, { shouldDirty: true });
    setValue("image_choice", `base:${selectedTemplate.image}`, { shouldDirty: true });
    setValue("instance_type", selectedTemplate.instance_type, { shouldDirty: true });
    setValue("cpu_count", selectedTemplate.cpu_count, { shouldDirty: true });
    setValue("memory_mb", selectedTemplate.memory_mb, { shouldDirty: true });
    setValue("disk_gb", selectedTemplate.disk_gb, { shouldDirty: true });
    setValue("availability_zone", selectedTemplate.availability_zones[0] ?? "", { shouldDirty: true });
    setValue("security_group_ids", selectedTemplate.security_group_ids, { shouldDirty: true });
    setValue("user_data", selectedTemplate.user_data, { shouldDirty: true });
    setValue("key_pair_id", selectedTemplate.key_pair_id ?? "", { shouldDirty: true });
    setValue("create_key_pair", selectedTemplate.create_key_pair, { shouldDirty: true });
  }, [selectedTemplate, setValue]);
  const selectedAmi = imageChoice?.startsWith("ami:")
    ? amis.data?.find((ami) => ami.id === imageChoice.slice(4))
    : undefined;
  useEffect(() => {
    if (selectedAmi && disk < selectedAmi.disk_gb) {
      setValue("disk_gb", selectedAmi.disk_gb, {
        shouldDirty: true,
        shouldValidate: true,
      });
    }
  }, [disk, selectedAmi, setValue]);
  const zones = useQuery({
    queryKey: ["capacity-zones"],
    queryFn: () => api.get<ZoneCapacity[]>("/instances/capacity/zones").then(response => response.data),
    refetchInterval: 10000,
  });
  const requestedZone = watch("availability_zone");
  const userData = watch("user_data");
  const eligibleZones = (zones.data ?? []).filter((zone) => zone.available);
  const requestedCapacityAvailable = requestedZone
    ? eligibleZones.some((zone) => zone.availability_zone === requestedZone)
    : eligibleZones.length > 0;
  const submit = handleSubmit(async (values) => {
    const effective = selectedTemplate ? {
      ...values,
      description: selectedTemplate.description,
      image: selectedTemplate.image,
      image_choice: `base:${selectedTemplate.image}`,
      instance_type: selectedTemplate.instance_type,
      cpu_count: selectedTemplate.cpu_count,
      memory_mb: selectedTemplate.memory_mb,
      disk_gb: selectedTemplate.disk_gb,
      availability_zone: selectedTemplate.availability_zones[0] ?? "",
      security_group_ids: selectedTemplate.security_group_ids,
      user_data: selectedTemplate.user_data,
      key_pair_id: selectedTemplate.key_pair_id ?? "",
      create_key_pair: selectedTemplate.create_key_pair,
    } : values;
    const isCustomAmi = effective.image_choice.startsWith("ami:");
    const amiId = isCustomAmi ? effective.image_choice.slice(4) : null;
    const image = isCustomAmi
      ? (amis.data?.find((ami) => ami.id === amiId)?.image ?? effective.image)
      : effective.image_choice.slice(5);
    const { image_choice: _imageChoice, ...payload } = effective;
    void _imageChoice;
    const { data } = await api.post<{ instance_id: string }>("/instances", {
      ...payload,
      image,
      ami_id: amiId,
      key_pair_id: effective.key_pair_id || null,
      create_key_pair: effective.key_pair_id ? false : effective.create_key_pair,
      availability_zone: effective.availability_zone || null,
    });
    nav(`/instances/${data.instance_id}`);
  });
  return (
    <>
      <PageHeader
        title="Launch instance"
        description="Choose a failure domain; the scheduler reserves an approved node before launch."
      />
      <div className="form-grid">
        <Card>
          <form onSubmit={submit}>
            <label className="template-launch-picker">
              Launch template
              <select {...register("launch_template_id")}>
                <option value="">Configure manually</option>
                {templates.data?.map((template) => <option value={template.id} key={template.id}>{template.name} · {template.instance_type} · {template.image}</option>)}
              </select>
              <small>{selectedTemplate ? `Applied ${selectedTemplate.cpu_count} vCPU, ${(selectedTemplate.memory_mb / 1024).toFixed(1)} GB RAM, ${selectedTemplate.disk_gb} GB disk, networking, keys, and user data.` : "Select a reusable template or configure the instance manually."}</small>
            </label>
            {selectedTemplate && <div className="template-lock-notice"><KeyRound/><div><strong>Configuration locked by {selectedTemplate.name}</strong><span>Placement, image, instance type, compute, storage, networking, SSH policy, and user data are controlled by the selected template. Choose “Configure manually” to customize them.</span></div></div>}
            <label>
              Name
              <input
                {...register("name", {
                  required: true,
                  pattern: /^[A-Za-z0-9 _-]{1,40}$/,
                })}
              />
              <small>
                {errors.name &&
                  "Use 1–40 letters, numbers, spaces, hyphens, or underscores."}
              </small>
            </label>
            <label>
              Description
              <textarea disabled={templateLocked} {...register("description", { maxLength: 500 })} />
            </label>
            <div className="fields">
              <label>
                Availability zone
                <select disabled={templateLocked} {...register("availability_zone")}>
                  <option value="">Automatic placement</option>
                  {zones.data?.map(zone => <option disabled={!zone.available} value={zone.availability_zone} key={zone.availability_zone}>{zone.availability_zone}{zone.available ? "" : " · unavailable"}</option>)}
                </select>
                {!requestedCapacityAvailable && <small>No approved capacity is currently available in this placement.</small>}
              </label>
              <label>
                Machine image
                <select disabled={templateLocked} {...register("image_choice")}>
                  <optgroup label="Base images">
                    <option value="base:24.04">Ubuntu 24.04 LTS</option>
                    <option value="base:22.04">Ubuntu 22.04 LTS</option>
                    <option value="base:debian-12">Debian 12</option>
                  </optgroup>
                  {amis.data?.length ? (
                    <optgroup label="Custom AMIs">
                      {amis.data.map((ami) => (
                        <option disabled={ami.status !== "AVAILABLE"} value={`ami:${ami.id}`} key={ami.id}>
                          {ami.name} · {ami.disk_gb} GB{ami.status === "AVAILABLE" ? "" : ` · ${ami.status.toLowerCase()}`}
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                </select>
                {amis.isLoading && <small>Loading custom AMIs…</small>}
                {!amis.isLoading && !amis.data?.length && (
                  <small>No custom AMIs are available yet.</small>
                )}
                {selectedAmi && (
                  <small>
                    Custom AMI · {selectedAmi.cpu_count} vCPU ·{" "}
                    {(selectedAmi.memory_mb / 1024).toFixed(1)} GB RAM · minimum{" "}
                    {selectedAmi.disk_gb} GB disk
                  </small>
                )}
              </label>
              <label>
                Instance type
                <select disabled={templateLocked} {...register("instance_type")}>
                  <optgroup label="Burstable T2">
                    <option value="t2.micro">t2.micro · 1 vCPU · 1 GB</option>
                    <option value="t2.small">t2.small · 1 vCPU · 2 GB</option>
                    <option value="t2.medium">t2.medium · 2 vCPU · 4 GB</option>
                  </optgroup>
                  <optgroup label="Burstable T4">
                    <option value="t4.micro">t4.micro · 2 vCPU · 1 GB</option>
                    <option value="t4.small">t4.small · 2 vCPU · 2 GB</option>
                    <option value="t4.medium">t4.medium · 2 vCPU · 4 GB</option>
                  </optgroup>
                  <optgroup label="General purpose M">
                    <option value="m5.large">m5.large · 2 vCPU · 8 GB</option>
                    <option value="m5.xlarge">
                      m5.xlarge · 4 vCPU · 16 GB
                    </option>
                  </optgroup>
                  <option value="custom">Custom</option>
                </select>
              </label>
              {(templateLocked || instanceType === "custom") && (
                <>
                  <label>
                    vCPUs
                    <input
                      type="number"
                      min="1"
                      max="8"
                      disabled={templateLocked}
                      {...register("cpu_count", { valueAsNumber: true })}
                    />
                  </label>
                  <label>
                    Memory (MB)
                    <input
                      type="number"
                      min="512"
                      max="16384"
                      step="512"
                      disabled={templateLocked}
                      {...register("memory_mb", { valueAsNumber: true })}
                    />
                  </label>
                  <label>
                    Disk (GB)
                    <input
                      type="number"
                      min={selectedAmi?.disk_gb ?? 5}
                      max="100"
                      disabled={templateLocked}
                      {...register("disk_gb", { valueAsNumber: true })}
                    />
                    {selectedAmi && (
                      <small>Must be at least {selectedAmi.disk_gb} GB for this AMI.</small>
                    )}
                  </label>
                </>
              )}
            </div>
            <div className="fields">
              <label>
                Reusable key pair
                <select disabled={templateLocked} {...register("key_pair_id")}>
                  <option value="">Generate one-time key</option>
                  {keyPairs.data?.map((key) => (
                    <option value={key.id} key={key.id}>
                      {key.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <fieldset className="security-group-picker">
              <legend>Security groups</legend>
              <p>Select one or more inbound firewall policies.</p>
              {securityGroups.isLoading ? (
                <span className="security-group-empty">
                  Loading security groups…
                </span>
              ) : securityGroups.data?.length ? (
                <div className="security-group-options">
                  {securityGroups.data.map((group) => (
                    <label className="security-group-option" key={group.id}>
                      <input
                        type="checkbox"
                        value={group.id}
                        disabled={templateLocked}
                        {...register("security_group_ids")}
                      />
                      <span>
                        <strong>{group.name}</strong>
                        <small>
                          {group.description ||
                            `${group.ingress_rules.length} inbound rule${group.ingress_rules.length === 1 ? "" : "s"}`}
                        </small>
                      </span>
                    </label>
                  ))}
                </div>
              ) : (
                <span className="security-group-empty">
                  No security groups yet.{" "}
                  <Link to="/security-groups">Create one</Link>
                </span>
              )}
            </fieldset>
            <label className="check">
              <input type="checkbox" disabled={templateLocked} {...register("create_key_pair")} /> Create
              a unique SSH key pair
            </label>
            <details className="advanced-details">
              <summary>Advanced details</summary>
              <div className="advanced-details-content">
                <div className="field-heading">
                  <div>
                    <strong>User data</strong>
                    <small>
                      Runs once as root during the initial instance launch, before
                      Tailscale publishing.
                    </small>
                  </div>
                  <Button
                    type="button"
                    className="secondary"
                    disabled={templateLocked}
                    onClick={() =>
                      setValue(
                        "user_data",
                        `#!/usr/bin/env bash
set -e

apt-get update
apt-get install -y nginx

cat >/var/www/html/index.html <<HTML
<!doctype html>
<html>
<body>
  <h1>Hello from $(hostname)</h1>
  <p>This page was installed by LocalEC2 user data.</p>
</body>
</html>
HTML

systemctl enable --now nginx
`,
                        { shouldDirty: true },
                      )
                    }
                  >
                    Use web-server example
                  </Button>
                </div>
                <textarea
                  className="user-data-editor"
                  spellCheck={false}
                  disabled={templateLocked}
                  placeholder={"#!/usr/bin/env bash\nset -e\n\n# Add non-interactive startup commands here"}
                  {...register("user_data", { maxLength: 20000 })}
                />
                <div className="user-data-help">
                  <small>
                    Use non-interactive commands and package flags such as{" "}
                    <code>-y</code>. The launch fails if this script exits with an
                    error.
                  </small>
                  <small>{userData.length.toLocaleString()} / 20,000</small>
                </div>
                {errors.user_data && (
                  <small className="field-error">
                    User data cannot exceed 20,000 characters.
                  </small>
                )}
              </div>
            </details>
            <div className="form-actions">
              <Link to="/instances">Cancel</Link>
              <Button disabled={isSubmitting || !requestedCapacityAvailable}>
                {isSubmitting ? "Queuing…" : "Launch instance"}
              </Button>
            </div>
          </form>
        </Card>
        <Card className="estimate">
          <h3>Requested allocation</h3>
          <div>
            <strong>{allocatedCpu}</strong>
            <span>vCPUs</span>
          </div>
          <div>
            <strong>{(allocatedMemory / 1024).toFixed(1)} GB</strong>
            <span>memory</span>
          </div>
          <div>
            <strong>{disk} GB</strong>
            <span>disk</span>
          </div>
          <p>
            The control plane reserves these resources inside the selected
            availability zone. Launch is rejected when placement cannot be satisfied.
          </p>
        </Card>
      </div>
    </>
  );
}
export type InstanceSection = "description" | "resources" | "analytics" | "actions";

export function InstanceSubnav({ instanceId }: { instanceId: string }) {
  const tabs = [
    ["", Server, "Description"],
    ["monitor", Monitor, "Monitor"],
    ["resources", Gauge, "Resources"],
    ["analytics", BarChart3, "Analytics & performance"],
    ["actions", ListChecks, "Actions"],
    ["terminal", SquareTerminal, "Terminal"],
  ] as const;
  return <nav className="instance-subnav" aria-label="Instance sections">{tabs.map(([path, Icon, label]) => <NavLink end={!path} to={`/instances/${instanceId}${path ? `/${path}` : ""}`} key={path}><Icon />{label}</NavLink>)}</nav>;
}

function MetricChart({ title, data, dataKey, color = "#087d9d", unit = "%" }: { title: string; data: Metric[]; dataKey: keyof Metric; color?: string; unit?: string }) {
  const latest = data.at(-1)?.[dataKey];
  return <Card className="instance-chart-card"><div className="chart-head"><h3>{title}</h3><strong>{typeof latest === "number" ? latest.toFixed(unit === "%" ? 1 : 0) : "—"} {unit}</strong></div><ResponsiveContainer width="100%" height={190}><AreaChart data={data}><defs><linearGradient id={`fill-${String(dataKey)}`} x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor={color} stopOpacity={0.3}/><stop offset="95%" stopColor={color} stopOpacity={0}/></linearGradient></defs><CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5edf0"/><XAxis dataKey="timestamp" hide/><YAxis domain={unit === "%" ? [0, 100] : ["auto", "auto"]} width={34}/><Tooltip labelFormatter={(value) => new Date(String(value)).toLocaleTimeString()}/><Area type="monotone" dataKey={dataKey} stroke={color} fill={`url(#fill-${String(dataKey)})`} strokeWidth={2} isAnimationActive={false}/></AreaChart></ResponsiveContainer></Card>;
}

export function InstanceDetailsPage({ section = "description" }: { section?: InstanceSection }) {
  const { instanceId = "" } = useParams();
  const navigate = useNavigate();
  const { data: item, isLoading, error } = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () =>
      api.get<Instance>(`/instances/${instanceId}`).then((r) => r.data),
    refetchInterval: 3000,
  });
  const { data: operations = [] } = useOperations();
  const { data: amis = [] } = useAmis();
  const action = useInstanceAction();
  const [samples, setSamples] = useState<Metric[]>([]);
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState("");
  const metricQuery = useQuery({
    queryKey: ["instance-metric-snapshot", instanceId],
    queryFn: () => api.get<Metric>(`/instances/${instanceId}/metrics/snapshot`).then(response => response.data),
    enabled: item?.status === "RUNNING" && ["resources", "analytics"].includes(section),
    refetchInterval: 5000,
    retry: false,
  });
  useEffect(() => {
    const sample = metricQuery.data;
    if (sample) setSamples(old => [...old, sample].slice(-60));
  }, [metricQuery.data]);
  useKeyDownloads(
    operations.filter((operation) => operation.instance_id === instanceId),
  );
  if (isLoading) return <Loading />;
  if (error || !item) return <ErrorAlert>{message(error)}</ErrorAlert>;
  const active = operations.find(
    (x) =>
      x.instance_id === item.id && ["PENDING", "RUNNING"].includes(x.status),
  );
  const captureInProgress = amis.some(ami => ami.status === "CREATING" && ami.source_instance_id === item.id);
  const itemOperations = operations.filter(operation => operation.instance_id === item.id);
  const latest = samples.at(-1);
  const memoryUsed = latest ? item.memory_mb * latest.memory_percent / 100 : 0;
  const diskUsed = latest ? item.disk_gb * latest.disk_percent / 100 : 0;
  const average = (key: "cpu_percent" | "memory_percent" | "disk_percent") => samples.length ? samples.reduce((sum, sample) => sum + sample[key], 0) / samples.length : 0;
  const peak = (key: "cpu_percent" | "memory_percent" | "disk_percent") => samples.length ? Math.max(...samples.map(sample => sample[key])) : 0;
  const run = (kind: "start" | "stop" | "hibernate" | "retry" | "terminate") => {
    if (kind === "terminate" && !confirm("Terminate this instance permanently?")) return;
    action.mutate({ id: item.id, action: kind }, { onSuccess: () => { if (kind === "terminate") navigate("/instances"); } });
  };
  return (
    <>
      <Link className="instance-back" to="/instances"><ArrowLeft />All instances</Link>
      <PageHeader
        title={item.display_name}
        description={`${item.id} · ${item.instance_type} · ${item.availability_zone}`}
        action={<Status value={item.status} />}
      />
      <InstanceSubnav instanceId={item.id} />
      {active && (
        <Card className="operation-banner">
          <div>
            <Status value={active.status} />
            <strong>{active.message}</strong>
          </div>
          <progress value={active.progress} max="100" />
          <span>{active.progress}%</span>
        </Card>
      )}
      {section === "description" && <><div className="instance-summary-strip"><div><span>State</span><Status value={item.status}/></div><div><span>Private IPv4</span><strong>{item.ipv4_address ?? "Pending"}</strong></div><div><span>Public endpoint</span>{item.public_endpoint ? <a href={item.public_endpoint} target="_blank" rel="noreferrer">Open endpoint <ExternalLink/></a> : <strong>{item.public_endpoint_status}</strong>}</div><div><span>Created</span><strong>{new Date(item.created_at).toLocaleString()}</strong></div></div><div className="details-grid"><Card><h2>Description</h2><p>{item.description || "No description was provided for this instance."}</p><dl><dt>Instance ID</dt><dd><code>{item.id}</code></dd><dt>Availability zone</dt><dd>{item.availability_zone}</dd><dt>Image</dt><dd>{item.image === "debian-12" ? "Debian 12" : `Ubuntu ${item.image}`}</dd><dt>AMI</dt><dd>{item.ami_id ?? "Base image"}</dd><dt>Autoscaling</dt><dd>{item.autoscaling_group_id ?? "Manually managed"}</dd></dl></Card><Card><h2>Networking and access</h2><dl><dt>Guest IPv4</dt><dd><code>{item.ipv4_address ?? "Not assigned"}</code></dd><dt>Tailscale IPv4</dt><dd><code>{item.tailscale_ipv4_address ?? "Not published"}</code></dd><dt>DNS name</dt><dd className="break">{item.tailscale_dns_name ?? "Not published"}</dd><dt>SSH command</dt><dd><code>ssh {item.ssh_username}@{item.ipv4_address ?? "<pending>"}</code></dd><dt>Security groups</dt><dd>{item.security_group_ids.length || "None"}</dd></dl></Card><Card><h2>Configuration</h2><dl><dt>Instance type</dt><dd>{item.instance_type}</dd><dt>Compute</dt><dd>{item.cpu_count} vCPU</dd><dt>Memory</dt><dd>{(item.memory_mb / 1024).toFixed(1)} GB</dd><dt>Root storage</dt><dd>{item.disk_gb} GB</dd><dt>Key fingerprint</dt><dd className="break">{item.key_fingerprint ?? "No key pair"}</dd></dl></Card><Card><h2>Recent activity</h2>{itemOperations.slice(0, 5).map(op => <div className="operation-row compact" key={op.id}><Status value={op.status}/><strong>{op.operation_type}</strong><span>{op.error_message ?? op.message}</span><span>{op.progress}%</span></div>)}{!itemOperations.length && <Empty title="No operations" detail="Instance activity will appear here."/>}</Card></div></>}
      {section === "resources" && <><div className="resource-kpi-grid"><div><Cpu/><span>CPU allocation</span><strong>{item.cpu_count} vCPU</strong><small>{latest ? `${latest.cpu_percent.toFixed(1)}% currently used` : "Waiting for live sample"}</small></div><div><MemoryStick/><span>Memory</span><strong>{(memoryUsed / 1024).toFixed(2)} / {(item.memory_mb / 1024).toFixed(1)} GB</strong><small>{latest ? `${latest.memory_percent.toFixed(1)}% used` : "Waiting for live sample"}</small></div><div><HardDrive/><span>Root storage</span><strong>{diskUsed.toFixed(1)} / {item.disk_gb} GB</strong><small>{latest ? `${latest.disk_percent.toFixed(1)}% used` : "Waiting for live sample"}</small></div><div><Network/><span>Network totals</span><strong>{latest ? `${(latest.network_rx_bytes / 1048576).toFixed(1)} MB ↓` : "—"}</strong><small>{latest ? `${(latest.network_tx_bytes / 1048576).toFixed(1)} MB uploaded` : "Waiting for live sample"}</small></div></div>{item.status !== "RUNNING" && <Card><Empty title="Live resources unavailable" detail="Start the instance to collect guest utilization."/></Card>}<div className="charts"><MetricChart title="CPU utilization" data={samples} dataKey="cpu_percent"/><MetricChart title="Memory utilization" data={samples} dataKey="memory_percent" color="#7c3aed"/><MetricChart title="Storage utilization" data={samples} dataKey="disk_percent" color="#d97706"/><MetricChart title="Network receive" data={samples} dataKey="network_rx_bytes" color="#12805c" unit="bytes"/></div></>}
      {section === "analytics" && <><div className="performance-grid"><Card><Activity/><span>Average CPU</span><strong>{average("cpu_percent").toFixed(1)}%</strong><small>Peak {peak("cpu_percent").toFixed(1)}%</small></Card><Card><MemoryStick/><span>Average memory</span><strong>{average("memory_percent").toFixed(1)}%</strong><small>Peak {peak("memory_percent").toFixed(1)}%</small></Card><Card><HardDrive/><span>Storage pressure</span><strong>{average("disk_percent").toFixed(1)}%</strong><small>Peak {peak("disk_percent").toFixed(1)}%</small></Card><Card><RotateCw/><span>Samples analyzed</span><strong>{samples.length}</strong><small>Rolling five-minute window</small></Card></div><Card><div className="chart-head"><div><h3>Performance comparison</h3><p>CPU, memory, and disk utilization over the active analysis window.</p></div><strong>{item.status}</strong></div><ResponsiveContainer width="100%" height={320}><LineChart data={samples}><CartesianGrid strokeDasharray="3 3" vertical={false}/><XAxis dataKey="timestamp" tickFormatter={value => new Date(value).toLocaleTimeString()} minTickGap={30}/><YAxis domain={[0,100]} width={38}/><Tooltip labelFormatter={value => new Date(String(value)).toLocaleTimeString()}/><Line dataKey="cpu_percent" name="CPU" stroke="#087d9d" strokeWidth={2} dot={false}/><Line dataKey="memory_percent" name="Memory" stroke="#7c3aed" strokeWidth={2} dot={false}/><Line dataKey="disk_percent" name="Disk" stroke="#d97706" strokeWidth={2} dot={false}/></LineChart></ResponsiveContainer></Card><div className="analytics-insights"><Card><h2>Performance assessment</h2><p>{!samples.length ? "Waiting for live samples." : peak("cpu_percent") > 85 ? "CPU pressure is high. Consider a larger instance type or distributing the workload." : average("memory_percent") > 80 ? "Memory utilization is consistently high. Consider increasing RAM." : peak("disk_percent") > 85 ? "Root storage is nearing capacity. Clean up data or increase disk size." : "Resource utilization is within normal operating thresholds."}</p></Card><Card><h2>Availability</h2><p>{item.status === "RUNNING" ? "The guest is running and responding to metric collection." : `Live collection is paused while the instance is ${item.status.toLowerCase()}.`}</p></Card></div></>}
      {section === "actions" && <>{keyError && <ErrorAlert>{keyError}</ErrorAlert>}<div className="instance-action-grid"><Card><Settings2/><h2>Lifecycle controls</h2><p>Change the power state of this virtual machine.</p><div className="instance-action-buttons">{["STOPPED","HIBERNATED"].includes(item.status) && <Button disabled={captureInProgress || action.isPending} onClick={() => run("start")}><Play/>Start instance</Button>}{["ERROR","UNKNOWN"].includes(item.status) && <Button disabled={captureInProgress || action.isPending} onClick={() => run("retry")}><RotateCw/>Retry launch</Button>}{item.status === "RUNNING" && <><Button className="secondary" disabled={captureInProgress || action.isPending} onClick={() => run("stop")}><StopCircle/>Stop</Button><Button className="secondary" disabled={captureInProgress || action.isPending} onClick={() => run("hibernate")}><Snowflake/>Hibernate</Button></>}</div>{captureInProgress && <small>Lifecycle controls are locked while AMI capture is running.</small>}</Card><Card><KeyRound/><h2>Access credentials</h2><p>Install and download a replacement private SSH key.</p><Button className="secondary" disabled={item.status !== "RUNNING" || keyBusy} onClick={async () => { setKeyBusy(true); setKeyError(""); try { const { data } = await api.post<{key_download_token:string;key_filename:string}>(`/instances/${item.id}/ssh-key`); await downloadKey(data.key_download_token,data.key_filename); } catch (error) { setKeyError(message(error)); } finally { setKeyBusy(false); } }}><KeyRound/>{keyBusy ? "Generating…" : "Generate replacement key"}</Button></Card><Card className="danger-action-card"><Trash2/><h2>Terminate instance</h2><p>Permanently remove the VM and release its reserved capacity.</p><Button disabled={captureInProgress || action.isPending} onClick={() => run("terminate")}><Trash2/>Terminate permanently</Button></Card></div><Card><h2>Operation history</h2>{itemOperations.map(op => <div className="operation-row" key={op.id}><Status value={op.status}/><strong>{op.operation_type}</strong><span>{op.error_message ?? op.message}</span><span>{op.progress}%</span></div>)}{!itemOperations.length && <Empty title="No operations" detail="Lifecycle operations will appear here."/>}</Card></>}
    </>
  );
}
