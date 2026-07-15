import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Activity, Plus, SquareTerminal, Trash2, X } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import { api, message } from "../api/client";
import { Button, Card, Empty, ErrorAlert, PageHeader, Status } from "../components/ui";
import { useGroups, useLoadBalancers } from "../hooks/data";
import { useAuth } from "../store/auth";
import type { LoadBalancer, LoadBalancerTarget } from "../types";

const wsBase = import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws";

function CommandTerminal({ loadBalancer, close }: { loadBalancer: LoadBalancer; close: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const token = useAuth((state) => state.accessToken);
  useEffect(() => {
    if (!host.current) return;
    const terminal = new Terminal({ cursorBlink: true, theme: { background: "#08151e", foreground: "#d8e8ef" }, fontFamily: "Cascadia Mono,Consolas,monospace", fontSize: 14 });
    const fit = new FitAddon();
    terminal.loadAddon(fit); terminal.open(host.current); fit.fit();
    terminal.writeln("LocalEC2 load-aware command terminal");
    terminal.writeln("Each command may run on a different lowest-load instance.\r\n");
    const socket = new WebSocket(`${wsBase}/load-balancers/${loadBalancer.id}/terminal`, [`localec2-token.${token ?? ""}`]);
    let line = ""; let busy = true;
    const prompt = () => { busy = false; terminal.write("localec2> "); };
    socket.onmessage = (event) => {
      const data = JSON.parse(String(event.data)) as { type: string; message?: string; target?: string; cpu_percent?: number; data?: string };
      if (data.type === "ready") { terminal.writeln(data.message ?? "Ready"); prompt(); }
      if (data.type === "selected") terminal.writeln(`\x1b[36m→ ${data.target} (${data.cpu_percent?.toFixed(1)}% CPU)\x1b[0m`);
      if (data.type === "output" && data.data) terminal.write(data.data.replace(/\n/g, "\r\n"));
      if (data.type === "done") { terminal.writeln(`\x1b[32m✓ completed on ${data.target}\x1b[0m`); prompt(); }
      if (data.type === "error") { terminal.writeln(`\x1b[31m${data.message}\x1b[0m`); prompt(); }
    };
    socket.onclose = () => terminal.writeln("\r\nDisconnected");
    const input = terminal.onData((value) => {
      if (busy || socket.readyState !== WebSocket.OPEN) return;
      if (value === "\r") {
        terminal.write("\r\n"); const command = line.trim(); line = "";
        if (command === "clear") { terminal.clear(); prompt(); return; }
        if (!command) { prompt(); return; }
        busy = true; socket.send(JSON.stringify({ command })); return;
      }
      if (value === "\u007f") { if (line) { line = line.slice(0, -1); terminal.write("\b \b"); } return; }
      if (value >= " ") { line += value; terminal.write(value); }
    });
    const resize = () => { fit.fit(); }; window.addEventListener("resize", resize);
    return () => { input.dispose(); socket.close(); terminal.dispose(); window.removeEventListener("resize", resize); };
  }, [loadBalancer.id, token]);
  return <Card><div className="resource-title"><div><h3>Command terminal · {loadBalancer.name}</h3><p>Commands are dispatched independently to the lowest-CPU running target.</p></div><button onClick={close} title="Close terminal"><X /></button></div><div className="terminal" ref={host} /></Card>;
}

export function LoadBalancersPage() {
  const { data = [] } = useLoadBalancers(); const groups = useGroups(); const qc = useQueryClient();
  const [open, setOpen] = useState(false); const [error, setError] = useState(""); const [selected, setSelected] = useState<string | null>(null); const [terminalId, setTerminalId] = useState<string | null>(null);
  const targets = useQuery({ queryKey: ["load-balancer-targets", selected], queryFn: () => api.get<LoadBalancerTarget[]>(`/load-balancers/${selected}/targets`).then((r) => r.data), enabled: !!selected, refetchInterval: 3000 });
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["load-balancers"] }); void qc.invalidateQueries({ queryKey: ["load-balancer-targets"] }); };
  const create = useMutation({ mutationFn: (form: FormData) => api.post("/load-balancers", { name: form.get("name"), autoscaling_group_id: form.get("group"), algorithm: "least_connections", target_port: 80, health_check_path: "/", health_check_interval_seconds: 10, enabled: true }), onSuccess: () => { setOpen(false); setError(""); refresh(); }, onError: (value) => setError(message(value)) });
  const toggle = async (lb: LoadBalancer) => { try { await api.patch(`/load-balancers/${lb.id}`, { name: lb.name, autoscaling_group_id: lb.autoscaling_group_id, algorithm: lb.algorithm, target_port: lb.target_port, health_check_path: lb.health_check_path, health_check_interval_seconds: lb.health_check_interval_seconds, enabled: !lb.enabled }); refresh(); } catch (value) { setError(message(value)); } };
  const activeTerminal = data.find((item) => item.id === terminalId);
  return <><PageHeader title="Command load balancers" description="Dispatch each command to the running autoscaled instance with the lowest current CPU load." action={<Button disabled={!groups.data?.length} onClick={() => setOpen(true)}><Plus />New command balancer</Button>} />
    {error && <ErrorAlert>{error}</ErrorAlert>}
    {open && <Card><form onSubmit={(event) => { event.preventDefault(); create.mutate(new FormData(event.currentTarget)); }}><div className="fields"><label>Name<input name="name" required /></label><label>Autoscaling group<select name="group">{groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label></div><p>CPU is measured before every command. Shell state does not persist between commands.</p><div className="form-actions"><Button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button>Create command balancer</Button></div></form></Card>}
    {activeTerminal && <CommandTerminal loadBalancer={activeTerminal} close={() => setTerminalId(null)} />}
    <div className="resource-grid">{data.length === 0 ? <Card><Empty title="No command balancers" detail={groups.data?.length ? "Connect one to an autoscaling group." : "Create an autoscaling group first."} /></Card> : data.map((lb) => <Card key={lb.id}><div className="resource-title"><div><h3>{lb.name}</h3><Status value={lb.enabled ? "RUNNING" : "STOPPED"} /></div><button className="danger-icon" onClick={async () => { if (confirm(`Delete ${lb.name}?`)) { await api.delete(`/load-balancers/${lb.id}`); refresh(); } }}><Trash2 /></button></div><p><strong>{lb.healthy_targets}</strong> available of {lb.total_targets} running targets</p><div className="form-actions"><Button disabled={!lb.enabled || lb.healthy_targets === 0} onClick={() => setTerminalId(lb.id)}><SquareTerminal />Open terminal</Button><Button className="secondary" onClick={() => void toggle(lb)}>{lb.enabled ? "Disable" : "Enable"}</Button><Button className="secondary" onClick={() => setSelected(selected === lb.id ? null : lb.id)}><Activity />Targets</Button></div>{selected === lb.id && <div className="table-wrap"><table><thead><tr><th>Target</th><th>Address</th><th>CPU</th><th>Status</th></tr></thead><tbody>{targets.data?.map((target) => <tr key={target.instance_id}><td>{target.name}</td><td><code>{target.address}</code></td><td>{target.cpu_percent?.toFixed(1) ?? "—"}%</td><td><Status value={target.healthy ? "RUNNING" : "ERROR"} /></td></tr>)}</tbody></table></div>}</Card>)}</div></>;
}
