import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, CheckCircle2, Network, Plus, Server, Trash2 } from "lucide-react";
import { api, message } from "../api/client";
import { Button, Card, Empty, ErrorAlert, PageHeader, Status } from "../components/ui";
import { useGroups, useInstances, useLoadBalancers, useTargetGroups } from "../hooks/data";
import type { LoadBalancer, LoadBalancerTarget, TargetGroup } from "../types";

type View = "load-balancers" | "target-groups";

export function LoadBalancersPage() {
  const [view, setView] = useState<View>("load-balancers");
  const [createType, setCreateType] = useState<View | null>(null);
  const [step, setStep] = useState(1);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [registrationType, setRegistrationType] = useState("manual");
  const [chosenInstances, setChosenInstances] = useState<string[]>([]);
  const lbs = useLoadBalancers();
  const targetGroups = useTargetGroups();
  const instances = useInstances();
  const groups = useGroups();
  const qc = useQueryClient();
  const running = instances.data?.filter((item) => item.status === "RUNNING") ?? [];
  const targets = useQuery({
    queryKey: ["load-balancer-targets", selected],
    queryFn: () => api.get<LoadBalancerTarget[]>(`/load-balancers/${selected}/targets`).then((r) => r.data),
    enabled: Boolean(selected),
    refetchInterval: 5000,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["load-balancers"] });
    void qc.invalidateQueries({ queryKey: ["target-groups"] });
    void qc.invalidateQueries({ queryKey: ["load-balancer-targets"] });
  };
  const close = () => { setCreateType(null); setStep(1); setError(""); };
  const createTargetGroup = useMutation({
    mutationFn: (form: FormData) => api.post("/target-groups", {
      name: form.get("name"),
      protocol: "HTTP",
      port: Number(form.get("port")),
      target_type: "instance",
      registration_type: registrationType,
      instance_ids: registrationType === "manual" ? chosenInstances : [],
      autoscaling_group_id: registrationType === "autoscaling" ? form.get("autoscaling_group_id") : null,
      health_check_path: form.get("health_check_path"),
      health_check_interval_seconds: Number(form.get("health_check_interval_seconds")),
    }),
    onSuccess: () => { close(); refresh(); },
    onError: (value) => setError(message(value)),
  });
  const createLoadBalancer = useMutation({
    mutationFn: (form: FormData) => api.post("/load-balancers", {
      name: form.get("name"),
      target_group_id: form.get("target_group_id"),
      algorithm: form.get("algorithm"),
      target_port: 80,
      health_check_path: "/",
      health_check_interval_seconds: 10,
      enabled: true,
    }),
    onSuccess: () => { close(); refresh(); },
    onError: (value) => setError(message(value)),
  });
  const remove = async (kind: View, id: string, name: string) => {
    if (!confirm(`Delete ${name}?`)) return;
    try {
      await api.delete(kind === "load-balancers" ? `/load-balancers/${id}` : `/target-groups/${id}`);
      refresh();
    } catch (value) { setError(message(value)); }
  };
  const retryRegistration = async (id: string) => {
    try {
      await api.post(`/target-groups/${id}/register-targets`);
      refresh();
    } catch (value) { setError(message(value)); }
  };

  return <>
    <PageHeader
      title="Elastic Load Balancing"
      description="Distribute incoming application traffic across healthy LocalEC2 instances."
      action={<Button onClick={() => { setCreateType(view); setStep(1); }}><Plus />{view === "load-balancers" ? "Create load balancer" : "Create target group"}</Button>}
    />
    <div className="aws-tabs">
      <button className={view === "load-balancers" ? "active" : ""} onClick={() => setView("load-balancers")}>Load balancers</button>
      <button className={view === "target-groups" ? "active" : ""} onClick={() => setView("target-groups")}>Target groups</button>
    </div>
    {error && <ErrorAlert>{error}</ErrorAlert>}

    {createType === "target-groups" && <Card>
      <div className="wizard-heading"><div><small>Step {step} of 2</small><h2>{step === 1 ? "Specify group details" : "Register targets"}</h2></div></div>
      <form onSubmit={(event) => { event.preventDefault(); createTargetGroup.mutate(new FormData(event.currentTarget)); }}>
        <div className="fields">
          <label>Target group name<input name="name" required placeholder="production-web-targets" /></label>
          <label>Protocol<select disabled><option>HTTP</option></select></label>
          <label>Port<input name="port" type="number" min="1" max="65535" defaultValue="80" required /></label>
          <label>Registration source<select value={registrationType} onChange={(e) => setRegistrationType(e.target.value)}><option value="manual">Instances</option><option value="autoscaling">Auto Scaling group</option></select></label>
          <label>Health check path<input name="health_check_path" defaultValue="/" required /></label>
          <label>Health check interval<input name="health_check_interval_seconds" type="number" min="5" max="300" defaultValue="10" required /></label>
        </div>
        {registrationType === "manual" ? <div className="target-picker"><h3>Available running instances</h3>
          {running.length === 0 ? <Empty title="No running instances" detail="Launch and start an instance before registering targets." /> :
            running.map((item) => <label className="target-choice" key={item.id}><input type="checkbox" checked={chosenInstances.includes(item.id)} onChange={(e) => setChosenInstances(e.target.checked ? [...chosenInstances, item.id] : chosenInstances.filter((id) => id !== item.id))} /><Server /><span><strong>{item.display_name}</strong><small>{item.ipv4_address} · {item.availability_zone}</small></span><Status value={item.status} /></label>)}
        </div> : <label>Auto Scaling group<select name="autoscaling_group_id" required>{groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>}
        <div className="form-actions"><Button type="button" className="secondary" onClick={close}>Cancel</Button><Button disabled={registrationType === "manual" && !chosenInstances.length}>Create target group</Button></div>
      </form>
    </Card>}

    {createType === "load-balancers" && <Card>
      <div className="wizard-heading"><div><small>Application Load Balancer</small><h2>Create load balancer</h2><p>Configure the listener, routing policy, and destination target group.</p></div></div>
      <form onSubmit={(event) => { event.preventDefault(); createLoadBalancer.mutate(new FormData(event.currentTarget)); }}>
        <section className="wizard-section"><h3>Basic configuration</h3><div className="fields">
          <label>Load balancer name<input name="name" required placeholder="production-alb" /></label>
          <label>Scheme<select disabled><option>Internet-facing</option></select></label>
          <label>IP address type<select disabled><option>IPv4 / Tailscale Funnel</option></select></label>
        </div></section>
        <section className="wizard-section"><h3>Listener and routing</h3><div className="fields">
          <label>Protocol<select disabled><option>HTTPS</option></select></label>
          <label>Public listener port<select disabled><option>443</option></select></label>
          <label>Forward to target group<select name="target_group_id" required>{targetGroups.data?.map((group) => <option key={group.id} value={group.id}>{group.name} ({group.protocol}:{group.port})</option>)}</select></label>
          <label>Routing algorithm<select name="algorithm" defaultValue="round_robin"><option value="round_robin">Round robin</option><option value="least_connections">Least outstanding requests</option><option value="least_cpu">Least sampled CPU</option><option value="random">Random</option><option value="source_ip_hash">Source IP hash</option></select></label>
        </div></section>
        <section className="review-box"><CheckCircle2 /><div><strong>Summary</strong><p>A public HTTPS listener will forward requests to healthy registered targets.</p></div></section>
        <div className="form-actions"><Button type="button" className="secondary" onClick={close}>Cancel</Button><Button disabled={!targetGroups.data?.length}>Create load balancer</Button></div>
      </form>
    </Card>}

    {!createType && view === "target-groups" && <Card>
      {!targetGroups.data?.length ? <Empty title="No target groups" detail="Create a target group and register running instances or an Auto Scaling group." /> :
      <div className="table-wrap"><table><thead><tr><th>Name</th><th>Protocol : port</th><th>Target source</th><th>Targets</th><th>Health check</th><th /></tr></thead><tbody>
        {targetGroups.data.map((group: TargetGroup) => <tr key={group.id}><td><strong>{group.name}</strong><small>{group.id}</small></td><td>{group.protocol}:{group.port}</td><td>{group.registration_type === "manual" ? "Instances" : "Auto Scaling group"}</td><td><Status value={group.health_status === "healthy" ? "RUNNING" : group.health_status === "initial" ? "PROVISIONING" : "ERROR"} /> {group.health_status === "initial" ? "Registering targets…" : `${group.healthy_targets}/${group.registered_targets} healthy`}</td><td><code>{group.health_check_path}</code></td><td><div className="row-actions">{group.health_status === "unhealthy" && <Button className="secondary" onClick={() => void retryRegistration(group.id)}>Register targets</Button>}<button className="danger-icon" onClick={() => void remove("target-groups", group.id, group.name)}><Trash2 /></button></div></td></tr>)}
      </tbody></table></div>}
    </Card>}

    {!createType && view === "load-balancers" && <div className="resource-grid">
      {!lbs.data?.length ? <Card><Empty title="No load balancers" detail={targetGroups.data?.length ? "Create an internet-facing application load balancer." : "Create a target group first."} /></Card> :
      lbs.data.map((lb: LoadBalancer) => <Card key={lb.id}>
        <div className="resource-title"><div><h3>{lb.name}</h3><Status value={!lb.enabled ? "STOPPED" : lb.public_endpoint_status === "ACTIVE" ? "RUNNING" : lb.public_endpoint_status === "ERROR" ? "ERROR" : "PROVISIONING"} /></div><button className="danger-icon" onClick={() => void remove("load-balancers", lb.id, lb.name)}><Trash2 /></button></div>
        <div className="lb-summary"><div><small>Type</small><strong>Application</strong></div><div><small>Scheme</small><strong>Internet-facing</strong></div><div><small>Targets</small><strong>{lb.healthy_targets}/{lb.total_targets} healthy</strong></div><div><small>Routing</small><strong>{lb.algorithm.replace("_", " ")}</strong></div></div>
        <label>DNS name / public endpoint<input readOnly value={lb.public_endpoint ?? (lb.public_endpoint_status === "ERROR" ? "Public endpoint provisioning failed" : "Provisioning Tailscale Funnel endpoint…")} /></label>
        <div className="listener-strip"><Network /><span><strong>HTTPS : 443</strong><small> forwards to {targetGroups.data?.find((group) => group.id === lb.target_group_id)?.name ?? "target group"} on HTTP:{lb.target_port}</small></span></div>
        <div className="form-actions"><Button className="secondary" onClick={() => setSelected(selected === lb.id ? null : lb.id)}><Activity />{selected === lb.id ? "Hide targets" : "View target health"}</Button></div>
        {selected === lb.id && <div className="table-wrap"><table><thead><tr><th>Target</th><th>Zone</th><th>Private IP</th><th>CPU</th><th>Health</th></tr></thead><tbody>{targets.data?.map((target) => <tr key={target.instance_id}><td>{target.name}</td><td>{target.availability_zone}</td><td><code>{target.address}</code></td><td>{target.cpu_percent?.toFixed(1) ?? "—"}%</td><td><Status value={target.healthy ? "RUNNING" : "ERROR"} /></td></tr>)}</tbody></table></div>}
      </Card>)}
    </div>}
  </>;
}
