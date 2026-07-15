import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import { useAuth } from "../store/auth";
import { Card, PageHeader, Status } from "../components/ui";
import type { Instance, Metric } from "../types";

const wsBase = import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws";

export function MonitorPage() {
  const { instanceId = "" } = useParams();
  const token = useAuth((state) => state.accessToken);
  const [data, setData] = useState<Metric[]>([]);
  const [state, setState] = useState("Waiting for VM");
  const { data: item } = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () =>
      api
        .get<Instance>(`/instances/${instanceId}`)
        .then((response) => response.data),
    refetchInterval: (query) =>
      query.state.data?.status === "RUNNING" ? 15_000 : 2_000,
  });

  useEffect(() => {
    if (item?.status !== "RUNNING") {
      setState(item?.status === "ERROR" ? "Instance error" : "Waiting for VM");
      return;
    }
    let socket: WebSocket | undefined;
    let timer: number;
    let closed = false;
    const connect = () => {
      socket = new WebSocket(`${wsBase}/instances/${instanceId}/metrics`, [
        `localec2-token.${token ?? ""}`,
      ]);
      socket.onopen = () => setState("Connected");
      socket.onmessage = (event) => {
        const point = JSON.parse(event.data) as Metric & { type: string };
        if (point.type === "metrics")
          setData((old) => [...old, point].slice(-60));
        if (point.type === "stopped") setState("Instance stopped");
      };
      socket.onclose = () => {
        if (!closed) {
          setState("Reconnecting");
          timer = window.setTimeout(connect, 2000);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, [instanceId, item?.status, token]);

  const charts = [
    ["CPU", "cpu_percent", "%"],
    ["Memory", "memory_percent", "%"],
    ["Disk", "disk_percent", "%"],
    ["Network receive", "network_rx_bytes", "B"],
    ["Network transmit", "network_tx_bytes", "B"],
  ] as const;
  return (
    <>
      <PageHeader
        title="Live monitoring"
        description="A rolling 2-minute view from inside the virtual machine."
        action={
          <Status
            value={
              state === "Connected"
                ? "RUNNING"
                : state === "Instance stopped"
                  ? "STOPPED"
                  : "PENDING"
            }
          />
        }
      />
      <div className="charts">
        {charts.map(([title, key, unit]) => (
          <Card key={key}>
            <div className="chart-head">
              <h3>{title}</h3>
              <strong>
                {data.at(-1)?.[key]?.toFixed(1) ?? "—"} {unit}
              </strong>
            </div>
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={data}>
                <XAxis dataKey="timestamp" hide />
                <YAxis
                  domain={unit === "%" ? [0, 100] : ["auto", "auto"]}
                  width={35}
                />
                <Tooltip
                  labelFormatter={(value) =>
                    new Date(String(value)).toLocaleTimeString()
                  }
                />
                <Line
                  dataKey={key}
                  stroke="#16a6a1"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </Card>
        ))}
      </div>
    </>
  );
}

export function TerminalPage() {
  const { instanceId = "" } = useParams();
  const { data: item } = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () =>
      api
        .get<Instance>(`/instances/${instanceId}`)
        .then((response) => response.data),
  });

  return (
    <>
      <PageHeader
        title={`Terminal · ${item?.display_name ?? ""}`}
        description="Distributed interactive terminal streaming is disabled until a reverse agent PTY channel is configured."
        action={<Status value="DISABLED" />}
      />
      {item && item.status !== "RUNNING" ? (
        <Card className="stopped">
          <h2>Instance is stopped</h2>
          <p>Start it before opening a terminal session.</p>
        </Card>
      ) : <Card className="stopped"><h2>Connect with SSH</h2><p>Use the downloaded private key from a network that can reach the selected node's guest network.</p><code>ssh {item?.ssh_username ?? "vagrant"}@{item?.ipv4_address ?? "&lt;instance-ip&gt;"}</code></Card>}
    </>
  );
}
