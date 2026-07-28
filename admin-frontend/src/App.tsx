import { FormEvent, ReactNode, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, AlertTriangle, Bot, Boxes, ChevronRight, CloudCog,
  Copy, Database, Gauge, HardDrive, KeyRound, LogOut,
  Network, Pause, Play, Plus, RefreshCw, Server, ShieldCheck, Snowflake,
  RotateCcw, Save, StopCircle, Stethoscope, Trash2, Users, Workflow,
} from "lucide-react";
import { api, apiBaseUrl, errorMessage } from "./api";
import { AgentPage } from "./AgentPage";
import { NodeDetails } from "./NodeDetails";
import type {
  AdminOperation, AdminVolume, CloudNode, Enrollment, InstanceDiagnostics,
  NodeContribution, Overview, PlatformRole, ReplicationStatus, TeamMember, User, Workload,
} from "./types";

type Page = "overview" | "nodes" | "workloads" | "storage" | "replication" | "operations" | "agent" | "access";
const roleOptions: { value: PlatformRole; label: string; detail: string }[] = [
  { value: "ADMINISTRATOR", label: "Administrator", detail: "Unrestricted platform and access control" },
  { value: "OPERATIONS", label: "Manager / Operations", detail: "Infrastructure health and daily operations" },
  { value: "MARKETING", label: "Marketing", detail: "Campaign and product visibility modules" },
  { value: "ANALYST_FINANCE", label: "Analyst / Finance", detail: "Reports, usage and financial modules" },
  { value: "DEVELOPER", label: "Developer", detail: "API integrations and developer tooling" },
];

function hasRole(user: User, role: PlatformRole) {
  return user.platform_roles.includes(role) || (role === "ADMINISTRATOR" && user.is_platform_admin);
}

function Login({ onLogin }: { onLogin: (token: string, user: User) => void }) {
  const [error, setError] = useState("");
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try {
      const { data } = await api.post<{ access_token: string; user: User }>("/auth/login", {
        email: form.get("email"), password: form.get("password"),
      });
      if (!data.user.platform_roles.length && !data.user.is_platform_admin)
        throw new Error("This account has no platform role");
      api.defaults.headers.common.Authorization = `Bearer ${data.access_token}`;
      onLogin(data.access_token, data.user);
    } catch (value) {
      setError(value instanceof Error && !value.message.includes("unexpected")
        ? value.message : errorMessage(value));
    }
  };
  return <main className="auth-screen"><section className="auth-copy"><div className="auth-mark"><CloudCog /></div><span>LOCAL EC2 CONTROL PLANE</span><h1>Operate your private cloud with confidence.</h1><p>A restricted workspace for infrastructure health, capacity, workloads, and access governance.</p><div className="auth-signal"><i />Control plane ready for authentication</div></section><form className="auth-card" onSubmit={submit}><div><small>AUTHORIZED PERSONNEL ONLY</small><h2>Sign in to administration</h2><p>Use your platform identity to continue.</p></div>{error && <Notice>{error}</Notice>}<label>Username or email<input name="email" defaultValue="admin" autoComplete="username" required /></label><label>Password<input name="password" type="password" autoComplete="current-password" required /></label><button className="primary">Continue <ChevronRight /></button><footer>Default development access is <code>admin / admin</code>.</footer></form></main>;
}

function ChangePassword({ done }: { done: () => void }) {
  const [error, setError] = useState("");
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    if (form.get("new_password") !== form.get("confirm")) return setError("Passwords do not match");
    try { await api.post("/auth/change-password", { current_password: form.get("current_password"), new_password: form.get("new_password") }); done(); }
    catch (value) { setError(errorMessage(value)); }
  };
  return <main className="auth-screen secure"><section className="auth-copy"><div className="auth-mark"><KeyRound /></div><span>FIRST SIGN-IN</span><h1>Secure the bootstrap account.</h1><p>Replace the development credential before accessing infrastructure controls.</p></section><form className="auth-card" onSubmit={submit}><h2>Create a private password</h2>{error && <Notice>{error}</Notice>}<label>Current password<input name="current_password" type="password" required /></label><label>New password<input name="new_password" type="password" minLength={10} required /></label><label>Confirm password<input name="confirm" type="password" minLength={10} required /></label><button className="primary">Update password</button></form></main>;
}

function Notice({ children }: { children: ReactNode }) { return <div className="notice"><AlertTriangle />{children}</div>; }
function Status({ value }: { value: string }) { return <span className={`status ${value.toLowerCase()}`}><i />{value.replaceAll("_", " ")}</span>; }
function Empty({ title, detail }: { title: string; detail: string }) { return <div className="empty"><Boxes /><h3>{title}</h3><p>{detail}</p></div>; }
function Loading() { return <div className="loading"><RefreshCw />Loading platform data…</div>; }
function percent(used: number, total: number) { return total ? Math.min(100, Math.round(used * 100 / total)) : 0; }

function CapacityBar({ label, used, total, unit }: { label: string; used: number; total: number; unit: string }) {
  const value = percent(used, total);
  return <div className="capacity-line"><div><span>{label}</span><strong>{used.toLocaleString()} / {total.toLocaleString()} {unit}</strong></div><progress value={value} max="100" /><small>{value}% allocated</small></div>;
}

function OverviewPage() {
  const overview = useQuery({ queryKey: ["admin-overview"], queryFn: () => api.get<Overview>("/admin/overview").then(r => r.data), refetchInterval: 15000 });
  const operations = useQuery({ queryKey: ["admin-operations"], queryFn: () => api.get<AdminOperation[]>("/admin/operations").then(r => r.data), refetchInterval: 10000 });
  if (overview.isLoading) return <Loading />; if (!overview.data) return <Notice>{errorMessage(overview.error)}</Notice>;
  const data = overview.data;
  return <><PageHeading eyebrow="COMMAND CENTER" title="Platform overview" detail="Live capacity, health, and activity across the private cloud." /><div className="metric-grid"><Metric icon={<Server />} label="Infrastructure" value={`${data.online_nodes}/${data.nodes}`} detail={`${data.pending_nodes} awaiting approval`} tone="blue" /><Metric icon={<Workflow />} label="Running workloads" value={data.running_instances} detail={`${data.instances} total instances`} tone="green" /><Metric icon={<Users />} label="Active accounts" value={data.users} detail="Customer and platform identities" tone="violet" /><Metric icon={<Activity />} label="Operations" value={data.active_operations} detail={`${data.failed_operations} failed`} tone={data.failed_operations ? "red" : "amber"} /></div><div className="two-column"><Panel title="Resource allocation" subtitle="Reported capacity across enrolled nodes"><CapacityBar label="Virtual CPU" used={data.cpu_allocated} total={data.cpu_total} unit="vCPU" /><CapacityBar label="Memory" used={Math.round(data.memory_allocated_mb / 1024)} total={Math.round(data.memory_total_mb / 1024)} unit="GB" /><CapacityBar label="Storage" used={data.storage_allocated_gb} total={data.storage_total_gb} unit="GB" /></Panel><Panel title="Failure domains" subtitle="Logical placement zones available to the scheduler"><div className="zone-list">{data.zones.length ? data.zones.map(zone => <div key={zone}><Database /><span><strong>{zone}</strong><small>Logical availability zone</small></span><Status value="AVAILABLE" /></div>) : <Empty title="No zones reported" detail="Enroll the first infrastructure node." />}</div></Panel></div><Panel title="Recent platform activity" subtitle="Latest operations from every customer account"><OperationsTable rows={(operations.data ?? []).slice(0, 7)} /></Panel></>;
}

function Metric({ icon, label, value, detail, tone }: { icon: ReactNode; label: string; value: ReactNode; detail: string; tone: string }) { return <article className="metric"><div className={`metric-icon ${tone}`}>{icon}</div><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></article>; }
function PageHeading({ eyebrow, title, detail, action }: { eyebrow: string; title: string; detail: string; action?: ReactNode }) {
  const qc = useQueryClient();
  const workloads = qc.getQueryData<Workload[]>(["admin-workloads"]) ?? [];
  const nodes = [...new Set(workloads.map(item => item.node ?? "Unassigned"))].sort();
  return <header className="page-heading"><div><span>{eyebrow}</span><h1>{title}</h1><p>{detail}</p></div><div className="workload-filters">{title === "Workload inventory" && <select className="filter" defaultValue="ALL" onChange={event => window.dispatchEvent(new CustomEvent("localec2-node-filter", { detail: event.target.value }))}><option value="ALL">All nodes</option>{nodes.map(node => <option key={node}>{node}</option>)}</select>}{action}</div></header>;
}
function Panel({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) { return <section className="panel"><div className="panel-head"><div><h2>{title}</h2><p>{subtitle}</p></div></div>{children}</section>; }

function NodeSetup({ enrollment, close }: { enrollment: Enrollment; close: () => void }) {
  const [controlPlane, setControlPlane] = useState(apiBaseUrl);
  const normalized = controlPlane.trim().replace(/\/$/, "");
  const command = `curl -fsSL '${normalized}/agent/install.sh' | sudo bash -s -- --api-url '${normalized}' --node-id '${enrollment.id}' --token '${enrollment.enrollment_token}'`;
  const localOnly = /:\/\/(localhost|127\.0\.0\.1)(:|\/)/.test(normalized);
  return <section className="setup-panel">
    <div className="setup-head"><div><span>CONNECT THE PHYSICAL HOST</span><h2>Finish setting up {enrollment.name}</h2><p>Enrollment reserved an identity. This command performs the real Ubuntu host installation.</p></div><button onClick={close}>Close</button></div>
    <div className="setup-steps">
      <div><b>1</b><span><strong>Set a reachable API address</strong><small>Use the control-plane LAN, VPN, or HTTPS address—not the node's own address.</small></span></div>
      <div><b>2</b><span><strong>Run the generated command on {enrollment.name}</strong><small>It installs KVM, libvirt, Vagrant, the NAT network, and a persistent agent service.</small></span></div>
      <div><b>3</b><span><strong>Wait for inventory, then approve</strong><small>CPU, memory, disk, interfaces, and health appear after the first heartbeat.</small></span></div>
    </div>
    <label className="control-plane-url">Control-plane API URL<input value={controlPlane} onChange={event => setControlPlane(event.target.value)} /></label>
    {localOnly && <div className="setup-warning"><AlertTriangle />localhost only works when this node is the control-plane PC. For another PC, enter this server's LAN/VPN/HTTPS address.</div>}
    <div className="install-command"><code>{command}</code><button onClick={() => void navigator.clipboard.writeText(command)}><Copy />Copy install command</button></div>
    <div className="setup-foot"><span>The node initiates outbound HTTP(S). LocalEC2 does not open an inbound admin port.</span><code>journalctl -u localec2-agent -f</code></div>
  </section>;
}

function ReservationPolicy({ node, save }: { node: CloudNode; save: (values: Record<string, unknown>) => Promise<void> }) {
  return <form className="reservation-form" onSubmit={event => { event.preventDefault(); const form = new FormData(event.currentTarget); void save({ cpu_reserved: Number(form.get("cpu_reserved")), memory_reserved_mb: Number(form.get("memory_reserved_mb")), storage_reserved_gb: Number(form.get("storage_reserved_gb")) }); }}>
    <span><strong>System reservation</strong><small>Capacity retained for Ubuntu, the agent, and control-plane services.</small></span>
    <label>vCPU<input name="cpu_reserved" type="number" min="0" max={Math.max(0, node.cpu_total - 1)} defaultValue={node.cpu_reserved} /></label>
    <label>Memory MB<input name="memory_reserved_mb" type="number" min="0" max={Math.max(0, node.memory_total_mb - 256)} step="256" defaultValue={node.memory_reserved_mb} /></label>
    <label>Disk GB<input name="storage_reserved_gb" type="number" min="0" max={Math.max(0, node.storage_total_gb - 1)} defaultValue={node.storage_reserved_gb} /></label>
    <button>Save policy</button>
  </form>;
}

function NodesPage({ token, isAdmin }: { token: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [showEnroll, setShowEnroll] = useState(false);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [agentUrls, setAgentUrls] = useState<Record<string, string>>({});
  const [updatingAgent, setUpdatingAgent] = useState("");
  const nodes = useQuery({ queryKey: ["admin-nodes"], queryFn: () => api.get<CloudNode[]>("/nodes").then(r => r.data), refetchInterval: 10000 });
  const contributions = useQuery({ queryKey: ["node-contributions"], queryFn: () => api.get<NodeContribution[]>("/admin/node-contributions").then(r => r.data), refetchInterval: 10000 });
  const update = async (id: string, values: Record<string, unknown>) => {
    try { await api.patch(`/nodes/${id}`, values); await qc.invalidateQueries({ queryKey: ["admin-nodes"] }); }
    catch (value) { setError(errorMessage(value)); }
  };
  const prepareSetup = async (node: CloudNode) => {
    try { const { data } = await api.post<Enrollment>(`/nodes/${node.id}/rotate-token`); setEnrollment(data); setError(""); }
    catch (value) { setError(errorMessage(value)); }
  };
  const enroll = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try {
      const { data } = await api.post<Enrollment>("/nodes", { name: form.get("name"), availability_zone: form.get("availability_zone"), provider_name: form.get("provider_name") });
      setEnrollment(data); setShowEnroll(false); setError("");
      await qc.invalidateQueries({ queryKey: ["admin-nodes"] });
    } catch (value) { setError(errorMessage(value)); }
  };
  const forceRemoveNode = async (node: CloudNode) => {
    if (!confirm(`FORCE REMOVE ${node.name}? All assigned instances and active jobs will be removed. Unsynchronized volume data may be lost.`)) return;
    if (!confirm(`Final confirmation: permanently remove ${node.name} from the control plane?`)) return;
    try {
      const { data } = await api.post<{ warning: string }>(`/admin/nodes/${node.id}/force-remove`);
      setError(data.warning);
      if (selectedNodeId === node.id) setSelectedNodeId(null);
      await qc.invalidateQueries({ queryKey: ["admin-nodes"] });
      await qc.invalidateQueries({ queryKey: ["node-contributions"] });
      await qc.invalidateQueries({ queryKey: ["admin-workloads"] });
      await qc.invalidateQueries({ queryKey: ["admin-volumes"] });
      await qc.invalidateQueries({ queryKey: ["admin-operations"] });
    } catch (value) { setError(errorMessage(value)); }
  };
  const removeNode = async (node: CloudNode) => {
    if (!confirm(`Remove ${node.name}?`)) return;
    try {
      await api.delete(`/nodes/${node.id}`);
      setSelectedNodeId(null);
      await qc.invalidateQueries({ queryKey: ["admin-nodes"] });
    } catch (value) { setError(errorMessage(value)); }
  };
  const updateAgent = async (node: CloudNode) => {
    const controlPlane = (agentUrls[node.id] ?? apiBaseUrl).trim().replace(/\/$/, "");
    setUpdatingAgent(node.id); setError("");
    try {
      const { data } = await api.post<{ version: string; message: string }>(`/admin/nodes/${node.id}/update-agent`, { control_plane_url: controlPlane });
      setError(`${data.message}. ${node.name} will reconnect as agent ${data.version}.`);
      window.setTimeout(() => { void qc.invalidateQueries({ queryKey: ["admin-nodes"] }); }, 15000);
    } catch (value) { setError(errorMessage(value)); }
    finally { setUpdatingAgent(""); }
  };
  const list = nodes.data ?? [];
  const selectedNode = list.find(node => node.id === selectedNodeId);
  if (selectedNode) return <>{error && <Notice>{error}</Notice>}{enrollment && <NodeSetup enrollment={enrollment} close={() => setEnrollment(null)} />}<NodeDetails node={selectedNode} contribution={contributions.data?.find(item => item.node_id === selectedNode.id)} token={token} isAdmin={isAdmin} agentUrl={agentUrls[selectedNode.id] ?? apiBaseUrl} updating={updatingAgent === selectedNode.id} reservationPolicy={<ReservationPolicy node={selectedNode} save={values => update(selectedNode.id, values)} />} onBack={() => setSelectedNodeId(null)} onAgentUrl={value => setAgentUrls(current => ({ ...current, [selectedNode.id]: value }))} onUpdateAgent={() => void updateAgent(selectedNode)} onSetup={() => void prepareSetup(selectedNode)} onApprove={() => void update(selectedNode.id, { approved: true })} onToggleScheduling={() => void update(selectedNode.id, { schedulable: !selectedNode.schedulable })} onRemove={() => void removeNode(selectedNode)} onForceRemove={() => void forceRemoveNode(selectedNode)} /></>;
  return <>
    <PageHeading eyebrow="INFRASTRUCTURE" title="Compute nodes" detail="Connect real Ubuntu hosts, approve scheduling, and inspect reported capacity and networking." action={isAdmin ? <button className="primary" onClick={() => setShowEnroll(x => !x)}><Plus />Enroll node</button> : <span className="approval-badge"><ShieldCheck />Administrator approval required for changes</span>} />
    {error && <Notice>{error}</Notice>}
    {enrollment && <NodeSetup enrollment={enrollment} close={() => setEnrollment(null)} />}
    {showEnroll && <Panel title="New infrastructure identity" subtitle="After enrollment, run the generated installer on the dedicated Ubuntu host."><form className="inline-form" onSubmit={enroll}><label>Node name<input name="name" placeholder="compute-01" required /></label><label>Logical zone<input name="availability_zone" defaultValue="local-region-1a" required /></label><label>Provider<select name="provider_name"><option value="vagrant-libvirt">Vagrant + libvirt/KVM</option></select></label><button className="primary">Continue to installation</button></form></Panel>}
    <div className="node-grid">{list.map(node => {
      const computeReady = ["jobs", "vagrant", "libvirt", "kvm"].every(capability => node.capabilities.includes(capability));
      const contribution = contributions.data?.find(item => item.node_id === node.id);
      return <article className="node-card" key={node.id}>
      <header><div className="node-icon"><Server /></div><div><h3>{node.name}</h3><span>{node.hostname ?? "Agent not installed"}</span></div><div className="node-card-header-actions"><Status value={node.status} /><button onClick={() => setSelectedNodeId(node.id)}>View details<ChevronRight /></button></div></header>
      {(!node.last_seen_at || !computeReady) && <div className="agent-missing"><AlertTriangle /><span><strong>{node.last_seen_at ? "Compute agent upgrade required" : "No host inventory received"}</strong><small>{node.last_seen_at ? "Open node details to update its agent." : "Open node details for setup instructions."}</small></span></div>}
      <dl><div><dt>Placement</dt><dd>{node.availability_zone}</dd></div><div><dt>Provider</dt><dd>{node.provider_name}</dd></div><div><dt>Management address</dt><dd>{node.management_address ?? "Waiting for agent"}</dd></div><div><dt>Last heartbeat</dt><dd>{node.last_seen_at ? new Date(node.last_seen_at).toLocaleString() : "Never"}</dd></div></dl>
      <div className="network-summary"><Network /><span><strong>{node.virtual_networks.length ? `libvirt: ${node.virtual_networks.join(", ")}` : "Virtual network not reported"}</strong><small>{node.network_interfaces.length ? node.network_interfaces.filter(item => item.family === "IPv4").slice(0, 4).map(item => `${item.name} ${item.address}`).join(" · ") || "No IPv4 address reported" : "Interfaces appear after agent connection"}</small></span></div>
      <div className="capability-list">{node.capabilities.length ? node.capabilities.map(item => <span key={item}>{item}</span>) : <span className="muted">Awaiting capability check</span>}</div>
      <div className="node-capacity"><CapacityBar label="CPU allocatable" used={node.cpu_allocated} total={Math.max(0, node.cpu_total - node.cpu_reserved)} unit="vCPU" /><CapacityBar label="Memory allocatable" used={Math.round(node.memory_allocated_mb / 1024)} total={Math.max(0, Math.round((node.memory_total_mb - node.memory_reserved_mb) / 1024))} unit="GB" /><CapacityBar label="Storage allocatable" used={node.storage_allocated_gb} total={Math.max(0, node.storage_total_gb - node.storage_reserved_gb)} unit="GB" /></div>
      {contribution && <div className="capacity"><span>Instances <strong>{contribution.instances}</strong></span><span>Attached volumes <strong>{contribution.attached_volumes}</strong><small>{contribution.attached_volume_gb} GB</small></span><span>Originated volumes <strong>{contribution.originated_volumes}</strong></span></div>}
    </article>})}{!list.length && <Panel title="No infrastructure registered" subtitle="Enroll a dedicated host to start building capacity."><Empty title="Your node fleet is empty" detail="The installer will register real host resources here." /></Panel>}</div>
  </>;
}

function WorkloadsPage({ isAdmin }: { isAdmin: boolean }) {
  void isAdmin;
  const qc = useQueryClient(); const [filter, setFilter] = useState("ALL"); const [nodeFilter, setNodeFilter] = useState("ALL"); const [selected, setSelected] = useState<string | null>(null); const [error, setError] = useState(""); const workloads = useQuery({ queryKey: ["admin-workloads"], queryFn: () => api.get<Workload[]>("/admin/instances").then(r => r.data), refetchInterval: 10000 });
  const diagnostics = useQuery({ queryKey: ["admin-diagnostics", selected], queryFn: () => api.get<InstanceDiagnostics>(`/admin/instances/${selected}/diagnostics`).then(r => r.data), enabled: Boolean(selected), retry: false });
  useEffect(() => { const listener = (event: Event) => setNodeFilter((event as CustomEvent<string>).detail); window.addEventListener("localec2-node-filter", listener); return () => window.removeEventListener("localec2-node-filter", listener); }, []);
  const rows = (workloads.data ?? []).filter(x => (filter === "ALL" || x.status === filter) && (nodeFilter === "ALL" || (x.node ?? "Unassigned") === nodeFilter)).sort((a, b) => (a.node ?? "~").localeCompare(b.node ?? "~") || a.name.localeCompare(b.name));
  const control = async (row: Workload, action: string) => { if (action === "terminate" && !confirm(`Terminate ${row.name}?`)) return; const path = action === "terminate" && confirm("Force removal? Choose Cancel for a normal graceful termination.") ? "force/remove" : action; try { await api.post(`/admin/instances/${row.id}/${path}`); setError(""); await qc.invalidateQueries({ queryKey: ["admin-workloads"] }); await qc.invalidateQueries({ queryKey: ["admin-operations"] }); } catch (value) { setError(errorMessage(value)); } };
  return <><PageHeading eyebrow="CUSTOMER COMPUTE" title="Workload inventory" detail="Physical placement, diagnostics, and emergency controls across every customer account." action={<select className="filter" value={filter} onChange={e => setFilter(e.target.value)}><option>ALL</option><option>RUNNING</option><option>STOPPED</option><option>ERROR</option><option>UNKNOWN</option></select>} />{error && <Notice>{error}</Notice>}<Panel title="All instances" subtitle={`${rows.length} workloads match the current view`}><div className="table-wrap"><table><thead><tr><th>Instance</th><th>Owner</th><th>Status</th><th>Physical placement</th><th>Resources</th><th>Address</th><th>Diagnostics & control</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><strong>{row.name}</strong><small>{row.instance_type} · {row.provider}</small>{row.last_error && <small>{row.last_error}</small>}</td><td>{row.owner}</td><td><Status value={row.status} /></td><td><strong>{row.node ?? "Unassigned"}</strong><small>{row.zone ?? "Legacy local provider"}</small></td><td>{row.cpu_count} vCPU · {(row.memory_mb / 1024).toFixed(1)} GB<small>{row.disk_gb} GB root disk</small></td><td><code>{row.ipv4_address ?? "—"}</code></td><td><div className="row-actions"><button title="Run diagnostics" onClick={() => setSelected(selected === row.id ? null : row.id)}><Stethoscope /></button>{row.status === "RUNNING" && <><button title="Stop" onClick={() => void control(row, "stop")}><StopCircle /></button><button title="Hibernate" onClick={() => void control(row, "hibernate")}><Snowflake /></button></>}{["STOPPED","HIBERNATED"].includes(row.status) && <button title="Start" onClick={() => void control(row, "start")}><Play /></button>}<button className="danger" title="Terminate" onClick={() => void control(row, "terminate")}><Trash2 /></button></div></td></tr>)}</tbody></table>{!rows.length && <Empty title="No workloads" detail="Customer instances will appear here." />}</div></Panel>{selected && <Panel title="Instance diagnostics" subtitle={diagnostics.isFetching ? "Querying the assigned compute agent…" : diagnostics.error ? errorMessage(diagnostics.error) : "Live provider state, guest metrics, and recent node jobs"}>{diagnostics.data && <><div className="metric-grid"><Metric icon={<Server />} label="Provider state" value={diagnostics.data.inspect.state} detail={diagnostics.data.inspect.ipv4_address ?? "No guest address"} tone="blue" /><Metric icon={<Activity />} label="Guest CPU" value={diagnostics.data.metrics ? `${diagnostics.data.metrics.cpu_percent.toFixed(1)}%` : "—"} detail="Live guest sample" tone="green" /><Metric icon={<Database />} label="Guest memory" value={diagnostics.data.metrics ? `${diagnostics.data.metrics.memory_percent.toFixed(1)}%` : "—"} detail={`Root disk ${diagnostics.data.metrics?.root_disk_percent ?? "—"}`} tone="violet" /></div><div className="table-wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Error</th><th>Created</th></tr></thead><tbody>{diagnostics.data.recent_jobs.map(job => <tr key={job.id}><td>{job.type}</td><td><Status value={job.status} /></td><td>{job.error ?? "—"}</td><td>{new Date(job.created_at).toLocaleString()}</td></tr>)}</tbody></table></div></>}</Panel>}</>;
}

function StoragePage({ isAdmin }: { isAdmin: boolean }) {
  void isAdmin;
  const qc = useQueryClient(); const [error, setError] = useState(""); const volumes = useQuery({ queryKey: ["admin-volumes"], queryFn: () => api.get<AdminVolume[]>("/admin/volumes").then(r => r.data), refetchInterval: 10000 });
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["admin-volumes"] }); void qc.invalidateQueries({ queryKey: ["node-contributions"] }); };
  return <><PageHeading eyebrow="DISTRIBUTED STORAGE" title="Volume inventory" detail="Cross-tenant backing ownership, node synchronization history, placement, and administrative controls." />{error && <Notice>{error}</Notice>}<Panel title="All volumes" subtitle={`${volumes.data?.length ?? 0} customer volumes`}><div className="table-wrap"><table><thead><tr><th>Volume</th><th>Owner</th><th>Scope</th><th>Origin / synchronization</th><th>Attachment</th><th>Backing</th><th>Control</th></tr></thead><tbody>{volumes.data?.map(volume => <tr key={volume.id}><td><strong>{volume.name}</strong><small>{volume.size_gb} GB · {volume.storage_backend}</small></td><td>{volume.owner}</td><td>{volume.availability_zone === "regional" ? "Regional shared" : volume.availability_zone}</td><td><strong>{volume.origin_node ?? "Not materialized"}</strong><small>Last node: {volume.last_node ?? "—"}</small></td><td>{volume.attached_instance ?? "Available"}<small>{volume.current_node ?? "No current node"} {volume.device_name ? `· ${volume.device_name}` : ""} {volume.mount_path ?? ""}</small></td><td><Status value={volume.canonical_present ? "AVAILABLE" : volume.attached_instance ? "RUNNING" : "PENDING"} /></td><td><div className="row-actions">{volume.attached_instance_id && <button title="Detach and synchronize" onClick={async () => { try { await api.post(`/admin/volumes/${volume.id}/detach`); refresh(); } catch (value) { setError(errorMessage(value)); } }}><Pause /></button>}<button className="danger" disabled={Boolean(volume.attached_instance_id)} title="Delete volume" onClick={async () => { if (!confirm(`Delete ${volume.name}?`)) return; try { await api.delete(`/admin/volumes/${volume.id}`); refresh(); } catch (value) { setError(errorMessage(value)); } }}><Trash2 /></button></div></td></tr>)}</tbody></table>{!volumes.data?.length && <Empty title="No volumes" detail="Customer volume creation will appear here with physical synchronization history." />}</div></Panel></>;
}

function ReplicationPage({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient(); const [error, setError] = useState(""); const [busy, setBusy] = useState("");
  const status = useQuery({ queryKey: ["admin-replication"], queryFn: () => api.get<ReplicationStatus>("/admin/replication").then(r => r.data), refetchInterval: 1000 });
  const nodes = useQuery({ queryKey: ["admin-nodes"], queryFn: () => api.get<CloudNode[]>("/nodes").then(r => r.data), refetchInterval: 5000 });
  const workloads = useQuery({ queryKey: ["admin-workloads"], queryFn: () => api.get<Workload[]>("/admin/instances").then(r => r.data), refetchInterval: 5000 });
  const refresh = async () => { await qc.invalidateQueries({ queryKey: ["admin-replication"] }); await qc.invalidateQueries({ queryKey: ["admin-workloads"] }); await qc.invalidateQueries({ queryKey: ["admin-nodes"] }); };
  const save = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); const form = new FormData(event.currentTarget); setBusy("policy"); try { await api.put("/admin/replication/policy", { enabled: form.get("enabled") === "on", interval_minutes: Number(form.get("interval_minutes")), replica_count: Number(form.get("replica_count")), retention_points: Number(form.get("retention_points")), pause_seconds_warning: Number(form.get("pause_seconds_warning")) }); setError(""); await refresh(); } catch (value) { setError(errorMessage(value)); } finally { setBusy(""); } };
  const capture = async (instanceId: string) => { setBusy(`capture-${instanceId}`); try { await api.post(`/admin/instances/${instanceId}/recovery-points`); setError(""); await refresh(); } catch (value) { setError(errorMessage(value)); } finally { setBusy(""); } };
  // The instance id is retained for recovery confirmation context in the caller.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const recover = async (pointId: string, instanceId: string) => { const eligible = (nodes.data ?? []).filter(node => node.status === "ONLINE" && node.approved && node.schedulable); const choices = eligible.map(node => `${node.id} = ${node.name}`).join("\n"); const target = prompt(`Enter a target node ID, or leave blank for automatic placement:\n${choices}`, ""); if (target === null || !confirm("Recover this instance from the selected point? The source host must be offline/fenced and changes newer than this point will be lost.")) return; setBusy(`recover-${pointId}`); try { await api.post(`/admin/recovery-points/${pointId}/recover`, { target_node_id: target.trim() || null }); setError(""); await refresh(); } catch (value) { setError(errorMessage(value)); } finally { setBusy(""); } };
  const remove = async (pointId: string) => { if (!confirm("Delete this canonical recovery point and all reachable node copies?")) return; setBusy(`delete-${pointId}`); try { await api.delete(`/admin/recovery-points/${pointId}`); setError(""); await refresh(); } catch (value) { setError(errorMessage(value)); } finally { setBusy(""); } };
  const policy = status.data?.policy;
  return <><PageHeading eyebrow="DISASTER RECOVERY" title="Asynchronous replication" detail="Scheduled root-disk recovery points, physical replica placement, retention, and administrator-controlled failover." />{error && <Notice>{error}</Notice>}{policy && <Panel title="Global replication policy" subtitle="Backups briefly pause active VM disk I/O; this is recovery-point protection, not zero-loss high availability."><form className="reservation-form replication-policy" onSubmit={save}><label className="toggle"><input name="enabled" type="checkbox" defaultChecked={policy.enabled} disabled={!isAdmin} /> Automatic replication</label><label>Interval (minutes)<input name="interval_minutes" type="number" min="5" max="1440" defaultValue={policy.interval_minutes} disabled={!isAdmin} /></label><label>Node copies<select name="replica_count" defaultValue={policy.replica_count} disabled={!isAdmin}><option value="1">1 laptop</option><option value="2">2 laptops</option></select></label><label>Points retained<input name="retention_points" type="number" min="1" max="20" defaultValue={policy.retention_points} disabled={!isAdmin} /></label><label>Pause warning (seconds)<input name="pause_seconds_warning" type="number" min="5" max="600" defaultValue={policy.pause_seconds_warning} disabled={!isAdmin} /></label>{isAdmin && <button disabled={busy === "policy"}><Save />{busy === "policy" ? "Saving…" : "Save policy"}</button>}</form></Panel>}<Panel title="Protected workloads" subtitle="Create an immediate recovery point independently of the schedule"><div className="table-wrap"><table><thead><tr><th>Instance</th><th>State</th><th>Placement</th><th>Latest point</th><th>Control</th></tr></thead><tbody>{(workloads.data ?? []).map(row => { const latest = status.data?.points.find(point => point.instance_id === row.id && point.status === "AVAILABLE"); return <tr key={row.id}><td><strong>{row.name}</strong><small>{row.disk_gb} GB root disk</small></td><td><Status value={row.status} /></td><td>{row.node ?? "Unassigned"}<small>{row.zone ?? "—"}</small></td><td>{latest?.completed_at ? new Date(latest.completed_at).toLocaleString() : "Never"}<small>{latest ? `${latest.replica_nodes.length} node copies` : "Unprotected"}</small></td><td>{isAdmin && <button disabled={!['RUNNING','STOPPED','HIBERNATED'].includes(row.status) || busy === `capture-${row.id}`} onClick={() => void capture(row.id)}><Copy />Snapshot now</button>}</td></tr>; })}</tbody></table></div></Panel><Panel title="Recovery-point catalogue" subtitle={`${status.data?.points.length ?? 0} retained and attempted points`}><div className="table-wrap"><table><thead><tr><th>Recovery point</th><th>Instance / source</th><th>Copies</th><th>Integrity</th><th>Status</th><th>Ultimate control</th></tr></thead><tbody>{status.data?.points.map(point => <tr key={point.id}><td><strong>{point.completed_at ? new Date(point.completed_at).toLocaleString() : new Date(point.created_at).toLocaleString()}</strong><small>{point.automatic ? "Scheduled" : "Administrator-created"} · {(point.size_bytes / 1024 ** 3).toFixed(2)} GB</small></td><td>{point.instance}<small>{point.source_node ?? "Missing source node"}</small></td><td>{point.replica_nodes.join(", ") || "No laptop copy"}<small>{point.canonical_present ? "Canonical copy present" : "Canonical copy missing"}</small></td><td><code>{point.sha256?.slice(0, 16) ?? "—"}</code><small>SHA-256</small></td><td><Status value={point.status} />{point.error_message && <small>{point.error_message}</small>}</td><td><div className="row-actions">{isAdmin && point.status === "AVAILABLE" && <button disabled={busy === `recover-${point.id}`} title="Recover to another node" onClick={() => void recover(point.id, point.instance_id)}><RotateCcw /></button>}{isAdmin && point.status !== "CREATING" && <button className="danger" disabled={busy === `delete-${point.id}`} title="Delete all copies" onClick={() => void remove(point.id)}><Trash2 /></button>}</div></td></tr>)}</tbody></table>{!status.data?.points.length && <Empty title="No recovery points" detail="Enable the policy or take an administrator snapshot." />}</div></Panel></>;
}

function OperationsTable({ rows }: { rows: AdminOperation[] }) { return <div className="table-wrap"><table><thead><tr><th>Operation</th><th>Owner</th><th>Status</th><th>Progress</th><th>Message</th><th>Time</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><strong>{row.type}</strong></td><td>{row.owner}</td><td><Status value={row.status} /></td><td><progress value={row.progress} max="100" /><small>{row.progress}%</small></td><td>{row.error_message ?? row.message}</td><td>{new Date(row.created_at).toLocaleString()}</td></tr>)}</tbody></table>{!rows.length && <Empty title="No operations" detail="Platform activity will appear here." />}</div>; }
function OperationsPage() { const operations = useQuery({ queryKey: ["admin-operations"], queryFn: () => api.get<AdminOperation[]>("/admin/operations").then(r => r.data), refetchInterval: 8000 }); return <><PageHeading eyebrow="AUDIT & DELIVERY" title="Platform operations" detail="Cross-tenant execution history and failure visibility." /><Panel title="Operation history" subtitle="The latest 250 control-plane operations"><OperationsTable rows={operations.data ?? []} /></Panel></>; }

function AccessPage() {
  const qc = useQueryClient(); const [error, setError] = useState(""); const [showAdd, setShowAdd] = useState(false);
  const team = useQuery({ queryKey: ["admin-team"], queryFn: () => api.get<TeamMember[]>("/admin/team").then(r => r.data) });
  const toggle = async (member: TeamMember, role: PlatformRole) => { const selected = member.platform_roles.includes(role) ? member.platform_roles.filter(x => x !== role) : [...member.platform_roles, role]; try { await api.patch(`/admin/team/${member.id}`, { platform_roles: selected, is_active: member.is_active, can_view_agent_code: member.can_view_agent_code, can_edit_agent_code: member.can_edit_agent_code }); await qc.invalidateQueries({ queryKey: ["admin-team"] }); } catch (value) { setError(errorMessage(value)); } };
  const add = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.post("/admin/team", { email: form.get("email"), display_name: form.get("display_name"), temporary_password: form.get("temporary_password"), platform_roles: [form.get("role")] }); setShowAdd(false); await qc.invalidateQueries({ queryKey: ["admin-team"] }); } catch (value) { setError(errorMessage(value)); } };
  const members = team.data ?? [];
  return <>
    <PageHeading eyebrow="IDENTITY & GOVERNANCE" title="Team & access" detail="Manage admin-portal identities separately from customer accounts." action={<button className="primary" onClick={() => setShowAdd(x => !x)}><Plus />Create admin credential</button>} />
    <div className="access-summary">
      <article><Users /><span><strong>{members.length}</strong><small>Admin identities</small></span></article>
      <article><ShieldCheck /><span><strong>{members.filter(member => member.platform_roles.includes("ADMINISTRATOR")).length}</strong><small>Administrators</small></span></article>
      <div><strong>Customer isolation enforced</strong><small>Customer accounts cannot sign in here and are not shown in this directory.</small></div>
    </div>
    {error && <Notice>{error}</Notice>}{team.error && <Notice>Could not load the admin team: {errorMessage(team.error)}</Notice>}
    {showAdd && <Panel title="Create an admin-portal credential" subtitle="Choose a temporary password and the minimum role needed. A password change is required at first sign-in."><form className="inline-form member" onSubmit={add}><label>Full name<input name="display_name" placeholder="e.g. Samira Khan" required /></label><label>Work email<input name="email" type="email" placeholder="name@company.com" required /></label><label>Temporary password<input name="temporary_password" type="password" minLength={10} autoComplete="new-password" required /></label><label>Initial role<select name="role">{roleOptions.map(role => <option value={role.value} key={role.value}>{role.label}</option>)}</select></label><button className="primary">Create credential</button></form></Panel>}
    <div className="access-layout"><Panel title="Admin portal team" subtitle={team.isLoading ? "Loading identities…" : `${members.length} authorized ${members.length === 1 ? "identity" : "identities"}`}><div className="member-list">{members.map(member => <article key={member.id}><div className="avatar">{member.display_name.slice(0, 2).toUpperCase()}</div><div className="member-main"><h3>{member.display_name}</h3><p>{member.email}</p><div className="role-pills">{member.platform_roles.map(role => <span key={role}>{roleOptions.find(x => x.value === role)?.label ?? role}</span>)}</div></div><Status value={member.is_active ? "ACTIVE" : "DISABLED"} /><div className="role-menu">{roleOptions.map(role => <label title={role.detail} key={role.value}><input type="checkbox" checked={member.platform_roles.includes(role.value)} onChange={() => void toggle(member, role.value)} /><span><strong>{role.label}</strong><small>{role.detail}</small></span></label>)}</div></article>)}</div>{!team.isLoading && !members.length && <Empty title="No admin identities" detail="Create the first admin-portal credential." />}</Panel><Panel title="Role policy" subtitle="Default access boundaries"><div className="policy-list">{roleOptions.map(role => <div key={role.value}><ShieldCheck /><span><strong>{role.label}</strong><small>{role.detail}</small></span></div>)}</div></Panel></div>
  </>;
}

function Shell({ user, token, logout }: { user: User; token: string; logout: () => void }) {
  const [page, setPage] = useState<Page>("overview"); const qc = useQueryClient(); const isAdmin = hasRole(user, "ADMINISTRATOR"); const canOperate = isAdmin || hasRole(user, "OPERATIONS");
  const canViewAgent = isAdmin || user.can_view_agent_code || user.can_edit_agent_code;
  const nav = useMemo(() => [
    ...(canOperate ? [
    ["overview", Gauge, "Overview"], ["nodes", Server, "Infrastructure"],
    ["workloads", Boxes, "Workloads"], ["storage", HardDrive, "Storage"],
    ["replication", Copy, "Replication"],
    ["operations", Activity, "Operations"],
    ...(isAdmin ? [["access", Users, "Team & access"]] : []),
    ] : []),
    ...(canViewAgent ? [["agent", Bot, "Agent"]] : []),
  ] as [Page, typeof Gauge, string][], [canOperate, canViewAgent, isAdmin]);
  const content = page === "agent" && canViewAgent ? <AgentPage user={user} /> : !canOperate ? <div className="restricted"><ShieldCheck /><h1>Role workspace not available yet</h1><p>Your role is active, but its specialized module has not been enabled.</p></div> : page === "overview" ? <OverviewPage /> : page === "nodes" ? <NodesPage token={token} isAdmin={isAdmin} /> : page === "workloads" ? <WorkloadsPage isAdmin={isAdmin} /> : page === "storage" ? <StoragePage isAdmin={isAdmin} /> : page === "replication" ? <ReplicationPage isAdmin={isAdmin} /> : page === "operations" ? <OperationsPage /> : <AccessPage />;
  return <div className="shell auto-sidebar"><aside><div className="brand"><div><CloudCog /></div><span>LocalEC2<small>CONTROL PLANE</small></span></div><div className="nav-label">PLATFORM</div><nav>{nav.map(([id, Icon, label]) => <button title={label} className={page === id ? "active" : ""} onClick={() => setPage(id)} key={id}><Icon /><span>{label}</span></button>)}</nav><div className="side-health"><span><i />Management API</span><strong>Connected</strong></div><div className="identity"><div className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</div><span><strong>{user.display_name}</strong><small>{isAdmin ? "Platform administrator" : "Operations manager"}</small></span><button title="Sign out" onClick={logout}><LogOut /></button></div></aside><div className="workspace"><div className="topbar"><div><span className="breadcrumb">LocalEC2 / <strong>{nav.find(x => x[0] === page)?.[2] ?? "Restricted"}</strong></span></div><div><span className="utc">{new Date().toLocaleDateString(undefined, { dateStyle: "medium" })}</span><button title="Refresh data" onClick={() => void qc.invalidateQueries()}><RefreshCw /></button><button className="topbar-logout" onClick={logout}><LogOut />Sign out</button></div></div><main>{content}</main></div></div>;
}

export default function App() {
  const [session, setSession] = useState<{ token: string; user: User } | null>(null); const [ready, setReady] = useState(false);
  useEffect(() => { api.post<{ access_token: string; user: User }>("/auth/refresh").then(({ data }) => { if (data.user.is_platform_admin || data.user.platform_roles.length) { api.defaults.headers.common.Authorization = `Bearer ${data.access_token}`; setSession({ token: data.access_token, user: data.user }); } }).finally(() => setReady(true)); }, []);
  if (!ready) return <div className="loading full"><RefreshCw />Connecting to control plane…</div>;
  if (!session) return <Login onLogin={(token, user) => setSession({ token, user })} />;
  if (session.user.must_change_password) return <ChangePassword done={() => setSession({ ...session, user: { ...session.user, must_change_password: false } })} />;
  return <Shell user={session.user} token={session.token} logout={() => { void api.post("/auth/logout"); delete api.defaults.headers.common.Authorization; setSession(null); }} />;
}
