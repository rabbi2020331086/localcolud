import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Network, Plus, Trash2 } from "lucide-react";
import { api, message } from "../api/client";
import { Button, Card, Empty, ErrorAlert, PageHeader, Status } from "../components/ui";
import { useGroups, useLoadBalancers } from "../hooks/data";
import type { LoadBalancer, LoadBalancerTarget } from "../types";

export function LoadBalancersPage() {
  const { data = [] } = useLoadBalancers();
  const groups = useGroups();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const targets = useQuery({
    queryKey: ["load-balancer-targets", selected],
    queryFn: () => api.get<LoadBalancerTarget[]>(`/load-balancers/${selected}/targets`).then((response) => response.data),
    enabled: Boolean(selected),
    refetchInterval: 5000,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["load-balancers"] });
    void qc.invalidateQueries({ queryKey: ["load-balancer-targets"] });
  };
  const create = useMutation({
    mutationFn: (form: FormData) => api.post("/load-balancers", {
      name: form.get("name"),
      autoscaling_group_id: form.get("group"),
      algorithm: form.get("algorithm"),
      target_port: Number(form.get("target_port")),
      health_check_path: form.get("health_path"),
      health_check_interval_seconds: Number(form.get("interval")),
      enabled: true,
    }),
    onSuccess: () => { setOpen(false); setError(""); refresh(); },
    onError: (value) => setError(message(value)),
  });
  const toggle = async (loadBalancer: LoadBalancer) => {
    try {
      await api.patch(`/load-balancers/${loadBalancer.id}`, {
        name: loadBalancer.name,
        autoscaling_group_id: loadBalancer.autoscaling_group_id,
        algorithm: loadBalancer.algorithm,
        target_port: loadBalancer.target_port,
        health_check_path: loadBalancer.health_check_path,
        health_check_interval_seconds: loadBalancer.health_check_interval_seconds,
        enabled: !loadBalancer.enabled,
      });
      refresh();
    } catch (value) { setError(message(value)); }
  };
  return <>
    <PageHeader title="Load balancers" description="Configure target groups, routing policy, ports, and HTTP health checks from one AWS-style control surface." action={<Button disabled={!groups.data?.length} onClick={() => setOpen(true)}><Plus />Create load balancer</Button>} />
    {error && <ErrorAlert>{error}</ErrorAlert>}
    <ErrorAlert>Target registration and health checks are active. A public traffic-listener gateway is the next networking layer; this page does not claim a public endpoint yet.</ErrorAlert>
    {open && <Card><form onSubmit={(event) => { event.preventDefault(); create.mutate(new FormData(event.currentTarget)); }}>
      <div className="fields">
        <label>Name<input name="name" required /></label>
        <label>Target autoscaling group<select name="group">{groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>
        <label>Routing algorithm<select name="algorithm" defaultValue="round_robin"><option value="round_robin">Round robin</option><option value="least_connections">Least connections</option></select></label>
        <label>Target port<input name="target_port" type="number" min="1" max="65535" defaultValue="80" required /></label>
        <label>HTTP health path<input name="health_path" defaultValue="/" required /></label>
        <label>Check interval (seconds)<input name="interval" type="number" min="5" max="300" defaultValue="10" required /></label>
      </div>
      <div className="form-actions"><Button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button>Create load balancer</Button></div>
    </form></Card>}
    <div className="resource-grid">{data.length === 0 ? <Card><Empty title="No load balancers" detail={groups.data?.length ? "Create one for an autoscaling target group." : "Create an autoscaling group first."} /></Card> : data.map((loadBalancer) => <Card key={loadBalancer.id}>
      <div className="resource-title"><div><h3>{loadBalancer.name}</h3><Status value={loadBalancer.enabled ? "RUNNING" : "STOPPED"} /></div><button className="danger-icon" onClick={async () => { if (confirm(`Delete ${loadBalancer.name}?`)) { await api.delete(`/load-balancers/${loadBalancer.id}`); refresh(); } }}><Trash2 /></button></div>
      <div className="capacity"><span>Healthy <strong>{loadBalancer.healthy_targets}/{loadBalancer.total_targets}</strong></span><span>Target port <strong>{loadBalancer.target_port}</strong></span><span>Policy <strong>{loadBalancer.algorithm.replace("_", " ")}</strong></span></div>
      <p>HTTP health check <code>{loadBalancer.health_check_path}</code> every {loadBalancer.health_check_interval_seconds} seconds.</p>
      <div className="form-actions"><Button className="secondary" onClick={() => void toggle(loadBalancer)}>{loadBalancer.enabled ? "Disable" : "Enable"}</Button><Button className="secondary" onClick={() => setSelected(selected === loadBalancer.id ? null : loadBalancer.id)}><Activity />Target health</Button></div>
      {selected === loadBalancer.id && <div className="table-wrap"><table><thead><tr><th>Target</th><th>Guest address</th><th>CPU</th><th>Health</th></tr></thead><tbody>{targets.data?.map((target) => <tr key={target.instance_id}><td><Network /> {target.name}</td><td><code>{target.address}</code></td><td>{target.cpu_percent?.toFixed(1) ?? "—"}%</td><td><Status value={target.healthy ? "RUNNING" : "ERROR"} /></td></tr>)}</tbody></table></div>}
    </Card>)}</div>
  </>;
}
