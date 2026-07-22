import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
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
import { InstanceSubnav } from "./Instances";

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
      socket.onopen = () => setState("Collecting first sample");
      socket.onmessage = (event) => {
        const point = JSON.parse(event.data) as Metric & { type: string };
        if (point.type === "metrics") {
          setState("Connected");
          setData((old) => [...old, point].slice(-60));
        }
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
        title={`Monitor · ${item?.display_name ?? "Instance"}`}
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
      <InstanceSubnav instanceId={instanceId} />
      {state === "Collecting first sample" && (
        <Card>
          <p>Connected to the compute node. Collecting the first guest metric sample…</p>
        </Card>
      )}
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
  const token = useAuth((state) => state.accessToken);
  const host = useRef<HTMLDivElement>(null);
  const [connection, setConnection] = useState("Waiting for VM");
  const { data: item } = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () =>
      api
        .get<Instance>(`/instances/${instanceId}`)
        .then((response) => response.data),
  });

  useEffect(() => {
    if (!host.current || item?.status !== "RUNNING") return;
    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', monospace",
      fontSize: 14,
      theme: { background: "#08151e", foreground: "#d8e7ec", cursor: "#47c5ce" },
      scrollback: 5000,
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(host.current);
    fit.fit();
    terminal.focus();
    const socket = new WebSocket(`${wsBase}/instances/${instanceId}/terminal`, [
      `localec2-token.${token ?? ""}`,
    ]);
    const encoder = new TextEncoder();
    const decoder = new TextDecoder();
    const encode = (value: string) => {
      const bytes = encoder.encode(value);
      let binary = "";
      for (const byte of bytes) binary += String.fromCharCode(byte);
      return btoa(binary);
    };
    const input = terminal.onData((data) => {
      let value = data.replace(/\r?\n/g, "\r");
      if (value.includes("\x1b[200~") && value.endsWith("\x1b[201~")) value += "\r";
      if (socket.readyState === WebSocket.OPEN)
        socket.send(JSON.stringify({ type: "input", data: encode(value) }));
    });
    socket.onopen = () => {
      setConnection("Connecting");
      socket.send(
        JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols }),
      );
    };
    socket.onmessage = (event) => {
      const message = JSON.parse(String(event.data)) as {
        type: string;
        data?: string;
        message?: string;
        code?: number;
      };
      if (message.type === "ready") {
        setConnection("Connected");
        terminal.focus();
      } else if (message.type === "output" && message.data) {
        const binary = atob(message.data);
        const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
        terminal.write(decoder.decode(bytes, { stream: true }));
      } else if (message.type === "error") {
        setConnection("Error");
        terminal.writeln(`\r\n\x1b[31m${message.message ?? "Terminal error"}\x1b[0m`);
      } else if (message.type === "exit") {
        setConnection("Closed");
        terminal.writeln(`\r\n\x1b[90mSession ended (${message.code ?? 0}).\x1b[0m`);
      }
    };
    socket.onerror = () => setConnection("Error");
    socket.onclose = (event) => {
      if (event.code === 4410)
        terminal.writeln("\r\nCompute node terminal channel is offline. Upgrade its agent.");
      setConnection((value) => (value === "Closed" ? value : "Disconnected"));
    };
    const resize = new ResizeObserver(() => {
      fit.fit();
      if (socket.readyState === WebSocket.OPEN)
        socket.send(
          JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols }),
        );
    });
    resize.observe(host.current);
    return () => {
      resize.disconnect();
      input.dispose();
      socket.close();
      terminal.dispose();
    };
  }, [instanceId, item?.status, token]);

  return (
    <>
      <PageHeader
        title={`Terminal · ${item?.display_name ?? ""}`}
        description="Authenticated interactive console relayed through the instance's compute agent."
        action={<Status value={connection === "Connected" ? "RUNNING" : connection === "Error" ? "ERROR" : "PENDING"} />}
      />
      <InstanceSubnav instanceId={instanceId} />
      {item && item.status !== "RUNNING" ? (
        <Card className="stopped">
          <h2>Instance is stopped</h2>
          <p>Start it before opening a terminal session.</p>
        </Card>
      ) : <div className="terminal" ref={host} aria-label="Instance terminal" />}
    </>
  );
}
