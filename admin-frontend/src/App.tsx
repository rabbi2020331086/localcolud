import { FormEvent, ReactNode, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, AlertTriangle, Boxes, Check, ChevronRight, CloudCog,
  Copy, Database, Gauge, KeyRound, LogOut,
  Network, Pause, Play, Plus, RefreshCw, Server, ShieldCheck, Trash2, Users, Workflow,
} from "lucide-react";
import { api, apiBaseUrl, errorMessage } from "./api";
import type {
  AdminOperation, CloudNode, Enrollment, Overview, PlatformRole, TeamMember,
  User, Workload,
} from "./types";

type Page = "overview" | "nodes" | "workloads" | "operations" | "access";
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
function PageHeading({ eyebrow, title, detail, action }: { eyebrow: string; title: string; detail: string; action?: ReactNode }) { return <header className="page-heading"><div><span>{eyebrow}</span><h1>{title}</h1><p>{detail}</p></div>{action}</header>; }
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

function NodesPage() {
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [showEnroll, setShowEnroll] = useState(false);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const nodes = useQuery({ queryKey: ["admin-nodes"], queryFn: () => api.get<CloudNode[]>("/nodes").then(r => r.data), refetchInterval: 10000 });
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
  const list = nodes.data ?? [];
  return <>
    <PageHeading eyebrow="INFRASTRUCTURE" title="Compute nodes" detail="Connect real Ubuntu hosts, approve scheduling, and inspect reported capacity and networking." action={<button className="primary" onClick={() => setShowEnroll(x => !x)}><Plus />Enroll node</button>} />
    {error && <Notice>{error}</Notice>}
    {enrollment && <NodeSetup enrollment={enrollment} close={() => setEnrollment(null)} />}
    {showEnroll && <Panel title="New infrastructure identity" subtitle="After enrollment, run the generated installer on the dedicated Ubuntu host."><form className="inline-form" onSubmit={enroll}><label>Node name<input name="name" placeholder="compute-01" required /></label><label>Logical zone<input name="availability_zone" defaultValue="local-region-1a" required /></label><label>Provider<select name="provider_name"><option value="vagrant-libvirt">Vagrant + libvirt/KVM</option><option value="multipass">Multipass legacy</option></select></label><button className="primary">Continue to installation</button></form></Panel>}
    <div className="node-grid">{list.map(node => <article className="node-card" key={node.id}>
      <header><div className="node-icon"><Server /></div><div><h3>{node.name}</h3><span>{node.hostname ?? "Agent not installed"}</span></div><Status value={node.status} /></header>
      {!node.last_seen_at && <div className="agent-missing"><AlertTriangle /><span><strong>No host inventory received</strong><small>Run the node installer to report real resources and networking.</small></span><button onClick={() => void prepareSetup(node)}>Setup / reconnect</button></div>}
      <dl><div><dt>Placement</dt><dd>{node.availability_zone}</dd></div><div><dt>Provider</dt><dd>{node.provider_name}</dd></div><div><dt>Management address</dt><dd>{node.management_address ?? "Waiting for agent"}</dd></div><div><dt>Last heartbeat</dt><dd>{node.last_seen_at ? new Date(node.last_seen_at).toLocaleString() : "Never"}</dd></div></dl>
      <div className="network-summary"><Network /><span><strong>{node.virtual_networks.length ? `libvirt: ${node.virtual_networks.join(", ")}` : "Virtual network not reported"}</strong><small>{node.network_interfaces.length ? `${node.network_interfaces.filter(item => item.family === "IPv4").length} IPv4 interface addresses` : "Interfaces appear after agent connection"}</small></span></div>
      <div className="capability-list">{node.capabilities.length ? node.capabilities.map(item => <span key={item}>{item}</span>) : <span className="muted">Awaiting capability check</span>}</div>
      <div className="node-capacity"><CapacityBar label="CPU" used={node.cpu_allocated} total={node.cpu_total} unit="vCPU" /><CapacityBar label="Memory" used={Math.round(node.memory_allocated_mb / 1024)} total={Math.round(node.memory_total_mb / 1024)} unit="GB" /><CapacityBar label="Storage" used={node.storage_allocated_gb} total={node.storage_total_gb} unit="GB" /></div>
      <footer>{!node.last_seen_at && <button onClick={() => void prepareSetup(node)}><RefreshCw />Install agent</button>}{!node.approved ? <button className="approve" disabled={!node.last_seen_at} title={!node.last_seen_at ? "Connect the node before approval" : "Approve for scheduling"} onClick={() => void update(node.id, { approved: true })}><Check />Approve</button> : node.schedulable ? <button onClick={() => void update(node.id, { schedulable: false })}><Pause />Drain</button> : <button onClick={() => void update(node.id, { schedulable: true })}><Play />Enable</button>}<button className="danger" onClick={async () => { if (!confirm(`Remove ${node.name}?`)) return; try { await api.delete(`/nodes/${node.id}`); await qc.invalidateQueries({ queryKey: ["admin-nodes"] }); } catch (value) { setError(errorMessage(value)); } }}><Trash2 />Remove</button></footer>
    </article>)}{!list.length && <Panel title="No infrastructure registered" subtitle="Enroll a dedicated host to start building capacity."><Empty title="Your node fleet is empty" detail="The installer will register real host resources here." /></Panel>}</div>
  </>;
}

function WorkloadsPage() {
  const [filter, setFilter] = useState("ALL"); const workloads = useQuery({ queryKey: ["admin-workloads"], queryFn: () => api.get<Workload[]>("/admin/instances").then(r => r.data), refetchInterval: 10000 });
  const rows = (workloads.data ?? []).filter(x => filter === "ALL" || x.status === filter);
  return <><PageHeading eyebrow="CUSTOMER COMPUTE" title="Workload inventory" detail="Read-only visibility into instances across every customer account." action={<select className="filter" value={filter} onChange={e => setFilter(e.target.value)}><option>ALL</option><option>RUNNING</option><option>STOPPED</option><option>ERROR</option></select>} /><Panel title="All instances" subtitle={`${rows.length} workloads match the current view`}><div className="table-wrap"><table><thead><tr><th>Instance</th><th>Owner</th><th>Status</th><th>Placement</th><th>Resources</th><th>Address</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><strong>{row.name}</strong><small>{row.provider}</small></td><td>{row.owner}</td><td><Status value={row.status} /></td><td>{row.node ?? "Unassigned"}<small>{row.zone ?? "Legacy local provider"}</small></td><td>{row.cpu_count} vCPU · {(row.memory_mb / 1024).toFixed(1)} GB<small>{row.disk_gb} GB root disk</small></td><td><code>{row.ipv4_address ?? "—"}</code></td></tr>)}</tbody></table>{!rows.length && <Empty title="No workloads" detail="Customer instances will appear here." />}</div></Panel></>;
}

function OperationsTable({ rows }: { rows: AdminOperation[] }) { return <div className="table-wrap"><table><thead><tr><th>Operation</th><th>Owner</th><th>Status</th><th>Progress</th><th>Message</th><th>Time</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><strong>{row.type}</strong></td><td>{row.owner}</td><td><Status value={row.status} /></td><td><progress value={row.progress} max="100" /><small>{row.progress}%</small></td><td>{row.error_message ?? row.message}</td><td>{new Date(row.created_at).toLocaleString()}</td></tr>)}</tbody></table>{!rows.length && <Empty title="No operations" detail="Platform activity will appear here." />}</div>; }
function OperationsPage() { const operations = useQuery({ queryKey: ["admin-operations"], queryFn: () => api.get<AdminOperation[]>("/admin/operations").then(r => r.data), refetchInterval: 8000 }); return <><PageHeading eyebrow="AUDIT & DELIVERY" title="Platform operations" detail="Cross-tenant execution history and failure visibility." /><Panel title="Operation history" subtitle="The latest 250 control-plane operations"><OperationsTable rows={operations.data ?? []} /></Panel></>; }

function AccessPage() {
  const qc = useQueryClient(); const [error, setError] = useState(""); const [showAdd, setShowAdd] = useState(false);
  const team = useQuery({ queryKey: ["admin-team"], queryFn: () => api.get<TeamMember[]>("/admin/team").then(r => r.data) });
  const toggle = async (member: TeamMember, role: PlatformRole) => { const selected = member.platform_roles.includes(role) ? member.platform_roles.filter(x => x !== role) : [...member.platform_roles, role]; try { await api.patch(`/admin/team/${member.id}`, { platform_roles: selected, is_active: member.is_active }); await qc.invalidateQueries({ queryKey: ["admin-team"] }); } catch (value) { setError(errorMessage(value)); } };
  const add = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.post("/admin/team", { email: form.get("email"), display_name: form.get("display_name"), temporary_password: form.get("temporary_password"), platform_roles: [form.get("role")] }); setShowAdd(false); await qc.invalidateQueries({ queryKey: ["admin-team"] }); } catch (value) { setError(errorMessage(value)); } };
  return <><PageHeading eyebrow="IDENTITY & GOVERNANCE" title="Team access" detail="Invite platform staff and assign the minimum permissions they require." action={<button className="primary" onClick={() => setShowAdd(x => !x)}><Plus />Add team member</button>} />{error && <Notice>{error}</Notice>}{team.error && <Notice>Could not load the platform team: {errorMessage(team.error)}</Notice>}{showAdd && <Panel title="Create platform identity" subtitle="The member must change this temporary password on first sign-in."><form className="inline-form member" onSubmit={add}><label>Name<input name="display_name" required /></label><label>Email<input name="email" type="email" required /></label><label>Temporary password<input name="temporary_password" type="password" minLength={10} required /></label><label>Initial role<select name="role">{roleOptions.map(role => <option value={role.value} key={role.value}>{role.label}</option>)}</select></label><button className="primary">Create member</button></form></Panel>}<div className="access-layout"><Panel title="Platform team" subtitle={team.isLoading ? "Loading identities…" : `${team.data?.length ?? 0} identities`}><div className="member-list">{(team.data ?? []).map(member => <article key={member.id}><div className="avatar">{member.display_name.slice(0, 2).toUpperCase()}</div><div className="member-main"><h3>{member.display_name}</h3><p>{member.email}</p><div className="role-pills">{member.platform_roles.length ? member.platform_roles.map(role => <span key={role}>{roleOptions.find(x => x.value === role)?.label ?? role}</span>) : <em>Customer only</em>}</div></div><Status value={member.is_active ? "ACTIVE" : "DISABLED"} /><div className="role-menu">{roleOptions.map(role => <label title={role.detail} key={role.value}><input type="checkbox" checked={member.platform_roles.includes(role.value)} onChange={() => void toggle(member, role.value)} /><span><strong>{role.label}</strong><small>{role.detail}</small></span></label>)}</div></article>)}</div></Panel><Panel title="Role policy" subtitle="Default access boundaries"><div className="policy-list">{roleOptions.map(role => <div key={role.value}><ShieldCheck /><span><strong>{role.label}</strong><small>{role.detail}</small></span></div>)}</div></Panel></div></>;
}

function Shell({ user, logout }: { user: User; logout: () => void }) {
  const [page, setPage] = useState<Page>("overview"); const qc = useQueryClient(); const isAdmin = hasRole(user, "ADMINISTRATOR"); const canOperate = isAdmin || hasRole(user, "OPERATIONS");
  const nav = useMemo(() => canOperate ? [
    ["overview", Gauge, "Overview"], ["nodes", Server, "Infrastructure"],
    ["workloads", Boxes, "Workloads"], ["operations", Activity, "Operations"],
    ...(isAdmin ? [["access", Users, "Team & access"]] : []),
  ] as [Page, typeof Gauge, string][] : [], [canOperate, isAdmin]);
  const content = !canOperate ? <div className="restricted"><ShieldCheck /><h1>Role workspace not available yet</h1><p>Your role is active, but its specialized module has not been enabled.</p></div> : page === "overview" ? <OverviewPage /> : page === "nodes" ? <NodesPage /> : page === "workloads" ? <WorkloadsPage /> : page === "operations" ? <OperationsPage /> : <AccessPage />;
  return <div className="shell"><aside><div className="brand"><div><CloudCog /></div><span>LocalEC2<small>CONTROL PLANE</small></span></div><div className="nav-label">PLATFORM</div><nav>{nav.map(([id, Icon, label]) => <button className={page === id ? "active" : ""} onClick={() => setPage(id)} key={id}><Icon />{label}</button>)}</nav><div className="side-health"><span><i />Management API</span><strong>Connected</strong></div><div className="identity"><div className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</div><span><strong>{user.display_name}</strong><small>{isAdmin ? "Platform administrator" : "Operations manager"}</small></span><button title="Sign out" onClick={logout}><LogOut /></button></div></aside><div className="workspace"><div className="topbar"><div><span className="breadcrumb">LocalEC2 / <strong>{nav.find(x => x[0] === page)?.[2] ?? "Restricted"}</strong></span></div><div><span className="utc">{new Date().toLocaleDateString(undefined, { dateStyle: "medium" })}</span><button title="Refresh data" onClick={() => void qc.invalidateQueries()}><RefreshCw /></button></div></div><main>{content}</main></div></div>;
}

export default function App() {
  const [session, setSession] = useState<{ token: string; user: User } | null>(null); const [ready, setReady] = useState(false);
  useEffect(() => { api.post<{ access_token: string; user: User }>("/auth/refresh").then(({ data }) => { if (data.user.is_platform_admin || data.user.platform_roles.length) { api.defaults.headers.common.Authorization = `Bearer ${data.access_token}`; setSession({ token: data.access_token, user: data.user }); } }).finally(() => setReady(true)); }, []);
  if (!ready) return <div className="loading full"><RefreshCw />Connecting to control plane…</div>;
  if (!session) return <Login onLogin={(token, user) => setSession({ token, user })} />;
  if (session.user.must_change_password) return <ChangePassword done={() => setSession({ ...session, user: { ...session.user, must_change_password: false } })} />;
  return <Shell user={session.user} logout={() => { void api.post("/auth/logout"); delete api.defaults.headers.common.Authorization; setSession(null); }} />;
}
