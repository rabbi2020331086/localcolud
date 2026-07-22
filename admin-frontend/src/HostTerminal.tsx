import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

const wsBase = String(import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws").replace(/\/$/, "");

function encode(value: string) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export function HostTerminal({ nodeId, token }: { nodeId: string; token: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState("Connecting");
  useEffect(() => {
    if (!host.current) return;
    const terminal = new Terminal({ cursorBlink: true, convertEol: true, fontSize: 14,
      fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', monospace", scrollback: 5000,
      theme: { background: "#0b1220", foreground: "#d1fadf", cursor: "#84adff" } });
    const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(host.current); fit.fit(); terminal.focus();
    const socket = new WebSocket(`${wsBase}/admin/nodes/${nodeId}/terminal`, [`localec2-token.${token}`]);
    const decoder = new TextDecoder();
    const input = terminal.onData(data => {
      let value = data.replace(/\r?\n/g, "\r");
      if (value.includes("\x1b[200~") && value.endsWith("\x1b[201~")) value += "\r";
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data: encode(value) }));
    });
    socket.onopen = () => { setState("Connecting to host shell"); socket.send(JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols })); };
    socket.onmessage = event => {
      const message = JSON.parse(String(event.data)) as { type: string; data?: string; message?: string; code?: number };
      if (message.type === "ready") { setState("Connected"); terminal.focus(); }
      else if (message.type === "output" && message.data) { const binary = atob(message.data); terminal.write(decoder.decode(Uint8Array.from(binary, c => c.charCodeAt(0)), { stream: true })); }
      else if (message.type === "error") { setState("Error"); terminal.writeln(`\r\n\x1b[31m${message.message ?? "Terminal error"}\x1b[0m`); }
      else if (message.type === "exit") { setState("Closed"); terminal.writeln(`\r\n\x1b[90mSession ended (${message.code ?? 0}).\x1b[0m`); }
    };
    socket.onerror = () => setState("Error");
    socket.onclose = () => setState(current => current === "Closed" ? current : "Disconnected");
    const resize = new ResizeObserver(() => { fit.fit(); if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", rows: terminal.rows, cols: terminal.cols })); });
    resize.observe(host.current);
    return () => { resize.disconnect(); input.dispose(); socket.close(); terminal.dispose(); };
  }, [nodeId, token]);
  return <div><div className="terminal-state">{state}</div><div className="host-terminal-live" ref={host} /></div>;
}
