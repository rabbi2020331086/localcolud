import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Code2, Copy, KeyRound, RotateCcw, ShieldCheck } from "lucide-react";
import { api, errorMessage } from "./api";
import type { AgentWorkspace, TeamMember, User } from "./types";

export function AgentPage({ user }: { user: User }) {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [authorizationCode, setAuthorizationCode] = useState("");
  const [showCodeGenerator, setShowCodeGenerator] = useState(false);
  const [adminPassword, setAdminPassword] = useState("");
  const [revealedCodes, setRevealedCodes] = useState<{ codes: string[]; expires_at: string } | null>(null);
  const workspace = useQuery({
    queryKey: ["agent-workspace"],
    queryFn: () => api.get<AgentWorkspace>("/admin/agent").then(response => response.data),
  });
  const team = useQuery({
    queryKey: ["admin-team"],
    queryFn: () => api.get<TeamMember[]>("/admin/team").then(response => response.data),
    enabled: user.is_platform_admin || user.platform_roles.includes("ADMINISTRATOR"),
  });
  useEffect(() => { if (workspace.data) setCode(workspace.data.draft); }, [workspace.data]);

  const save = async () => {
    setBusy("save"); setNotice("");
    try {
      const { data } = await api.put<{ version: string }>("/admin/agent/draft", { code });
      setNotice(`Draft ${data.version} saved. It is not deployed yet.`);
      await qc.invalidateQueries({ queryKey: ["agent-workspace"] });
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setBusy(""); }
  };
  const deploy = async () => {
    if (!authorizationCode.trim()) { setNotice("Enter a one-time deployment authorization code."); return; }
    if (!confirm("Deploy this saved agent release to the node update channel?")) return;
    setBusy("deploy"); setNotice("");
    try {
      const { data } = await api.post<{ version: string }>("/admin/agent/deploy", { authorization_code: authorizationCode.trim() });
      setNotice(`Agent ${data.version} deployed. Online agents will update automatically.`);
      setAuthorizationCode("");
      await qc.invalidateQueries({ queryKey: ["agent-workspace"] });
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setBusy(""); }
  };
  const createDeployCodes = async () => {
    if (!adminPassword) { setNotice("Enter your administrator password to generate codes."); return; }
    setBusy("code"); setNotice("");
    try {
      const { data } = await api.post<{ codes: string[]; expires_at: string }>("/admin/agent/deploy-code", { admin_password: adminPassword });
      setRevealedCodes(data); setAdminPassword("");
      setNotice("Five single-use deployment codes were created. Any previous unused codes you created are now invalid.");
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setBusy(""); }
  };
  const rollback = async (releaseId: string, version: string) => {
    if (!authorizationCode.trim()) { setNotice("Enter a one-time deployment authorization code before rollback."); return; }
    if (!confirm(`Roll back the live update channel to agent ${version}?`)) return;
    setBusy(`rollback-${releaseId}`); setNotice("");
    try {
      const { data } = await api.post<{ version: string }>(`/admin/agent/releases/${releaseId}/rollback`, { authorization_code: authorizationCode.trim() });
      setNotice(`Agent ${data.version} is now live. Online agents will return to this release automatically.`);
      setAuthorizationCode("");
      await qc.invalidateQueries({ queryKey: ["agent-workspace"] });
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setBusy(""); }
  };
  const permission = async (member: TeamMember, field: "view" | "edit", checked: boolean) => {
    const canEdit = field === "edit" ? checked : member.can_edit_agent_code;
    const canView = field === "view" ? checked : member.can_view_agent_code;
    await api.patch(`/admin/agent/permissions/${member.id}`, { can_view: canView, can_edit: canEdit });
    await qc.invalidateQueries({ queryKey: ["admin-team"] });
  };

  if (workspace.isLoading) return <div className="loading full">Loading agent workspace…</div>;
  if (workspace.error) return <div className="notice">{errorMessage(workspace.error)}</div>;
  const data = workspace.data!;
  return <>
    <header className="page-heading"><div><span>AGENT DELIVERY</span><h1>{editorOpen ? "Agent editor" : "Agent"}</h1><p>{editorOpen ? "Edit the agent source, save a draft, then publish it through the protected deployment workflow." : "Manage published releases, deployment authorization, rollback history, and source access."}</p></div><button className={editorOpen ? "editor-back-button" : "primary"} onClick={() => setEditorOpen(value => !value)}>{editorOpen ? <><ArrowLeft />Back to agent dashboard</> : <><Code2 />Go to agent editor</>}</button></header>
    {notice && <div className="notice">{notice}</div>}
    {!editorOpen && <section className="panel agent-dashboard-summary"><div><span>CURRENTLY PUBLISHED</span><strong>{data.published_version}</strong><small>{data.published_at ? `Published ${new Date(data.published_at).toLocaleString()}` : "Bundled source"}</small></div><div><span>WORKING DRAFT</span><strong>{data.draft_version}</strong><small>{data.draft_version === data.published_version ? "Version matches published release" : "Draft is awaiting deployment"}</small></div><div><span>ARCHIVED RELEASES</span><strong>{data.releases.length}</strong><small>{data.releases.length ? "Available for immediate rollback" : "Created after the first deployment"}</small></div></section>}
    {editorOpen && <section className="panel agent-editor-panel">
      <div className="panel-head"><div><h2>Agent source</h2><p>Change AGENT_VERSION before deploying modified source. Compatibility metadata is generated automatically.</p></div><div className="agent-code-actions">{data.can_edit && data.bundled_version !== data.draft_version && <button onClick={() => { setCode(data.bundled_source); setNotice(`Built-in agent ${data.bundled_version} loaded. Save the draft before deploying.`); }}><RotateCcw />Load built-in {data.bundled_version}</button>}<button className="editor-back-button" onClick={() => setEditorOpen(false)}><ArrowLeft />Close editor</button></div></div>
      <div className="agent-release-summary"><span>Draft <strong>{data.draft_version}</strong></span><span>Published <strong>{data.published_version}</strong></span><span>Published at <strong>{data.published_at ? new Date(data.published_at).toLocaleString() : "Bundled source"}</strong></span></div>
      <textarea className="agent-code-editor" value={code} onChange={event => setCode(event.target.value)} readOnly={!data.can_edit} spellCheck={false} aria-label="Agent Python source" />
      <div className="agent-code-actions">
        {data.can_edit && <button className="primary" disabled={Boolean(busy)} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save draft"}</button>}
        {data.can_deploy && <button className="primary" disabled={Boolean(busy)} onClick={() => void deploy()}>{busy === "deploy" ? "Deploying…" : "Deploy agent"}</button>}
      </div>
    </section>}
    {data.can_deploy && <section className="panel deploy-security-panel">
      <div className="panel-head"><div><h2><KeyRound /> Deployment authorization</h2><p>Use one code per deploy or rollback. Each generated code expires after 72 hours and is shown only in this session.</p></div><ShieldCheck /></div>
      <div className="authorization-action-row"><div><h3>Authorize an action</h3><p>During development, your administrator password can also be used here without expiring.</p></div><input className="authorization-code-input" type="password" autoComplete="off" value={authorizationCode} onChange={event => setAuthorizationCode(event.target.value)} placeholder="Authorization code or development admin password" /><button onClick={() => { setShowCodeGenerator(value => !value); setRevealedCodes(null); }}>{showCodeGenerator ? "Close generator" : "Generate authorization codes"}</button></div>
      {showCodeGenerator && <div className="code-generator-card"><div><h3>Generate five single-use codes</h3><p>Confirm your administrator password. Generating a new batch invalidates all unused codes from your previous batch.</p></div><div className="deploy-code-controls"><input type="password" autoComplete="current-password" value={adminPassword} onChange={event => setAdminPassword(event.target.value)} placeholder="Administrator password" /><button className="primary" disabled={Boolean(busy)} onClick={() => void createDeployCodes()}>{busy === "code" ? "Generating…" : "Generate 5 codes"}</button></div></div>}
      {revealedCodes && <div className="one-time-code code-batch"><div><span>FIVE SINGLE-USE CODES — COPY THEM NOW</span><div className="one-time-code-list">{revealedCodes.codes.map((code, index) => <code key={code}>{index + 1}. {code}</code>)}</div><small>All expire {new Date(revealedCodes.expires_at).toLocaleString()}</small></div><button onClick={() => void navigator.clipboard.writeText(revealedCodes.codes.join("\n"))}><Copy /> Copy all</button><button onClick={() => { setRevealedCodes(null); setShowCodeGenerator(false); }}>Hide permanently</button></div>}
    </section>}
    {data.can_deploy && <section className="panel"><div className="panel-head"><div><h2>Agent release archive</h2><p>Immutable source snapshots with integrity hashes. Any earlier release can immediately replace the live update channel.</p></div></div><div className="table-wrap"><table><thead><tr><th>Version</th><th>State</th><th>Released by</th><th>Created</th><th>Integrity</th><th>Action</th></tr></thead><tbody>{data.releases.map(release => { const live = data.live_release_id === release.id; return <tr key={release.id}><td><strong>{release.version}</strong><small>{release.action}</small></td><td><span className={`status ${live ? "active" : "available"}`}><i />{live ? "LIVE" : "ARCHIVED"}</span></td><td>{release.created_by}</td><td>{new Date(release.created_at).toLocaleString()}</td><td><code>{release.sha256.slice(0, 16)}…</code></td><td><button className="rollback-button" disabled={live || Boolean(busy)} onClick={() => void rollback(release.id, release.version)}><RotateCcw />{busy === `rollback-${release.id}` ? "Rolling back…" : "Deploy this version"}</button></td></tr>; })}</tbody></table>{!data.releases.length && <div className="empty"><h3>No archived releases yet</h3><p>The first protected deployment will create the first immutable snapshot.</p></div>}</div></section>}
    {data.can_deploy && <section className="panel"><h2>Source permissions</h2><p>Editors may save drafts. Only administrators can deploy releases.</p><div className="table-wrap"><table><thead><tr><th>Person</th><th>View code</th><th>Edit draft</th></tr></thead><tbody>{(team.data ?? []).map(member => <tr key={member.id}><td><strong>{member.display_name}</strong><small>{member.email}</small></td><td><input type="checkbox" checked={member.can_view_agent_code || member.can_edit_agent_code} onChange={event => void permission(member, "view", event.target.checked)} /></td><td><input type="checkbox" checked={member.can_edit_agent_code} onChange={event => void permission(member, "edit", event.target.checked)} /></td></tr>)}</tbody></table></div></section>}
  </>;
}
