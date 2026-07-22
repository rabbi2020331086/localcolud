import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Link, Link2Off, Plus, Trash2 } from "lucide-react";
import { api, message } from "../api/client";
import {
  Button,
  Card,
  Empty,
  ErrorAlert,
  PageHeader,
  Status,
} from "../components/ui";
import {
  useAmis,
  useInstances,
  useKeyPairs,
  useSecurityGroups,
  useVolumes,
  useVolumeStorageNodes,
} from "../hooks/data";

export function AmisPage() {
  const { data = [], isLoading } = useAmis();
  const { data: instances = [] } = useInstances();
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [amiBusy, setAmiBusy] = useState("");
  const capturingSources = new Set(
    data.filter((ami) => ami.status === "CREATING").map((ami) => ami.source_instance_id),
  );
  const stopped = instances.filter(
    (item) => item.status === "STOPPED" && !capturingSources.has(item.id),
  );
  return (
    <>
      <PageHeader
        title="AMIs"
        description="Capture a stopped instance once and launch the replicated image on any compute node."
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <Card>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const formElement = event.currentTarget;
            setError(""); setAmiBusy("create");
            const request = api.post("/amis", {
              name: form.get("name"),
              source_instance_id: form.get("source_instance_id"),
              description: form.get("description"),
            });
            window.setTimeout(() => {
              void qc.invalidateQueries({ queryKey: ["amis"] });
            }, 500);
            void request.then(() => {
              formElement.reset();
              void qc.invalidateQueries({ queryKey: ["amis"] });
            }).catch((value) => setError(message(value))).finally(() => setAmiBusy(""));
          }}
        >
          <div className="fields">
            <label>
              Name
              <input name="name" required />
            </label>
            <label>
              Source instance
              <select name="source_instance_id" required defaultValue="">
                <option value="" disabled>Select a stopped instance</option>
                {stopped.map((item) => (
                  <option key={item.id} value={item.id}>{item.display_name}</option>
                ))}
              </select>
            </label>
            <label>
              Description
              <input name="description" />
            </label>
          </div>
          <div className="form-actions">
            <Button disabled={!stopped.length || amiBusy === "create"}>
              <Plus />
              {amiBusy === "create" ? "Capturing and replicating…" : "Create AMI"}
            </Button>
          </div>
          {stopped.length === 0 && <small>Stop an instance before creating an AMI.</small>}
        </form>
      </Card>
      <Card>
        {isLoading ? (
          <p>Loading AMIs…</p>
        ) : data.length === 0 ? (
          <Empty title="No AMIs" detail="Create an image from a stopped instance." />
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Name</th><th>Status</th><th>Base</th><th>Size</th><th>Created</th><th>Actions</th></tr></thead>
              <tbody>
                {data.map((ami) => (
                  <tr key={ami.id}>
                    <td><strong>{ami.name}</strong><small>{ami.description}</small></td>
                    <td>
                      <Status value={ami.status} />
                      {ami.status === "CREATING" && <small>Capturing and replicating…</small>}
                      {ami.status === "FAILED" && <small>Capture failed</small>}
                    </td>
                    <td>Ubuntu {ami.image}</td>
                    <td>{ami.cpu_count} vCPU · {ami.memory_mb / 1024} GB · {ami.disk_gb} GB</td>
                    <td>{new Date(ami.created_at).toLocaleDateString()}</td>
                    <td><button className="danger-icon" disabled={amiBusy === ami.id || ami.status === "CREATING"} title={ami.status === "CREATING" ? "AMI capture in progress" : "Delete replicated AMI"} onClick={async () => {
                      if (!confirm(`Delete AMI ${ami.name}?`)) return;
                      try {
                        setAmiBusy(ami.id); setError("");
                        await api.delete(`/amis/${ami.id}`);
                        void qc.invalidateQueries({ queryKey: ["amis"] });
                      } catch (value) { setError(message(value)); }
                      finally { setAmiBusy(""); }
                    }}><Trash2 /></button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = -1;
  do {
    amount /= 1024;
    unit += 1;
  } while (amount >= 1024 && unit < units.length - 1);
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unit]}`;
}

export function KeyPairsPage() {
  const { data = [] } = useKeyPairs();
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const create = useMutation({
    mutationFn: (form: FormData) =>
      api.post("/key-pairs", { name: form.get("name") }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["key-pairs"] }),
    onError: (value) => setError(message(value)),
  });
  const download = async (id: string, name: string) => {
    try {
      const response = await api.get(`/key-pairs/${id}/download`, {
        responseType: "blob",
      });
      saveBlob(response.data, `${name}.pem`);
    } catch (value) {
      setError(message(value));
    }
  };
  return (
    <>
      <PageHeader
        title="Key pairs"
        description="Reusable SSH identities. Each private key can be downloaded only once."
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <Card>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate(new FormData(event.currentTarget));
          }}
        >
          <div className="fields">
            <label>
              Key pair name
              <input name="name" required />
            </label>
          </div>
          <div className="form-actions">
            <Button disabled={create.isPending}>
              <Plus />
              Create key pair
            </Button>
          </div>
        </form>
      </Card>
      <Card>
        {data.length === 0 ? (
          <Empty
            title="No key pairs"
            detail="Create a reusable SSH key for your instances."
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Fingerprint</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.map((key) => (
                  <tr key={key.id}>
                    <td>
                      <strong>{key.name}</strong>
                    </td>
                    <td>
                      <code>{key.fingerprint}</code>
                    </td>
                    <td className="row-actions">
                      <button
                        title="Download private key once"
                        onClick={() => void download(key.id, key.name)}
                      >
                        <Download />
                      </button>
                      <button
                        className="danger-icon"
                        onClick={async () => {
                          if (confirm("Delete key pair?")) {
                            await api.delete(`/key-pairs/${key.id}`);
                            void qc.invalidateQueries({
                              queryKey: ["key-pairs"],
                            });
                          }
                        }}
                      >
                        <Trash2 />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

export function SecurityGroupsPage() {
  const { data = [] } = useSecurityGroups();
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [restrictOutbound, setRestrictOutbound] = useState(false);
  const create = useMutation({
    mutationFn: (form: FormData) =>
      api.post("/security-groups", {
        name: form.get("name"),
        description: form.get("description"),
        ingress_rules: [
          {
            protocol: form.get("protocol"),
            from_port: Number(form.get("from_port")),
            to_port: Number(form.get("to_port")),
            source: form.get("source"),
            description: "",
          },
        ],
        egress_rules:
          form.get("restrict_outbound") === "on"
            ? [
                {
                  protocol: form.get("egress_protocol"),
                  from_port: Number(form.get("egress_from_port")),
                  to_port: Number(form.get("egress_to_port")),
                  source: form.get("destination"),
                  description: "",
                },
              ]
            : [],
      }),
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ["security-groups"] }),
    onError: (value) => setError(message(value)),
  });
  return (
    <>
      <PageHeader
        title="Security groups"
        description="Reusable inbound and outbound firewall policies applied with UFW at launch."
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <Card>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate(new FormData(event.currentTarget));
          }}
        >
          <h3>General</h3>
          <div className="fields">
            <label>
              Name
              <input name="name" required />
            </label>
            <label>
              Description
              <input name="description" />
            </label>
          </div>
          <h3>Inbound rule</h3>
          <div className="fields">
            <label>
              Protocol
              <select name="protocol">
                <option>tcp</option>
                <option>udp</option>
              </select>
            </label>
            <label>
              From port
              <input
                name="from_port"
                type="number"
                min="1"
                max="65535"
                defaultValue="22"
              />
            </label>
            <label>
              To port
              <input
                name="to_port"
                type="number"
                min="1"
                max="65535"
                defaultValue="22"
              />
            </label>
            <label>
              Source CIDR
              <input name="source" defaultValue="0.0.0.0/0" />
            </label>
          </div>
          <h3>Outbound policy</h3>
          <label className="check">
            <input
              name="restrict_outbound"
              type="checkbox"
              checked={restrictOutbound}
              onChange={(event) => setRestrictOutbound(event.target.checked)}
            />
            Restrict outbound traffic (otherwise all outbound traffic is
            allowed)
          </label>
          {restrictOutbound && (
            <div className="fields">
              <label>
                Protocol
                <select name="egress_protocol">
                  <option>tcp</option>
                  <option>udp</option>
                </select>
              </label>
              <label>
                From port
                <input
                  name="egress_from_port"
                  type="number"
                  min="1"
                  max="65535"
                  defaultValue="443"
                />
              </label>
              <label>
                To port
                <input
                  name="egress_to_port"
                  type="number"
                  min="1"
                  max="65535"
                  defaultValue="443"
                />
              </label>
              <label>
                Destination CIDR
                <input name="destination" defaultValue="0.0.0.0/0" />
              </label>
            </div>
          )}
          <div className="form-actions">
            <Button disabled={create.isPending}>
              <Plus />
              Create security group
            </Button>
          </div>
        </form>
      </Card>
      <div className="resource-grid">
        {data.length === 0 ? (
          <Card>
            <Empty
              title="No security groups"
              detail="Create an inbound access policy."
            />
          </Card>
        ) : (
          data.map((group) => (
            <Card key={group.id}>
              <div className="resource-title">
                <div>
                  <h3>{group.name}</h3>
                  <p>{group.description}</p>
                </div>
                <button
                  className="danger-icon"
                  onClick={async () => {
                    if (confirm("Delete security group?")) {
                      await api.delete(`/security-groups/${group.id}`);
                      void qc.invalidateQueries({
                        queryKey: ["security-groups"],
                      });
                    }
                  }}
                >
                  <Trash2 />
                </button>
              </div>
              <h4>Inbound</h4>
              <div className="chips">
                {group.ingress_rules.map((rule, index) => (
                  <span key={index}>
                    {rule.protocol.toUpperCase()} {rule.from_port}
                    {rule.to_port !== rule.from_port
                      ? `–${rule.to_port}`
                      : ""}{" "}
                    · from {rule.source}
                  </span>
                ))}
              </div>
              <h4>Outbound</h4>
              <div className="chips">
                {group.egress_rules.length === 0 ? (
                  <span>All outbound traffic allowed</span>
                ) : (
                  group.egress_rules.map((rule, index) => (
                    <span key={index}>
                      {rule.protocol.toUpperCase()} {rule.from_port}
                      {rule.to_port !== rule.from_port
                        ? `–${rule.to_port}`
                        : ""}{" "}
                      · to {rule.source}
                    </span>
                  ))
                )}
              </div>
            </Card>
          ))
        )}
      </div>
    </>
  );
}

export function VolumesPage() {
  const { data = [] } = useVolumes();
  const storageNodes = useVolumeStorageNodes();
  const instances = useInstances();
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const refresh = () => void qc.invalidateQueries({ queryKey: ["volumes"] });
  return (
    <>
      <PageHeader
        title="Volumes"
        description="Create block storage on a selected compute node, then synchronize it securely when attaching to an instance on any other node."
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <Card>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const formElement = event.currentTarget;
            const form = new FormData(formElement);
            setBusy("create");
            setError("");
            void api.post("/volumes", {
              name: form.get("name"),
              size_gb: Number(form.get("size")),
              node_id: form.get("node_id"),
            }).then(() => {
              formElement.reset();
              refresh();
            }).catch((value) => setError(message(value))).finally(() => setBusy(""));
          }}
        >
          <div className="fields">
            <label>
              Volume name
              <input name="name" required />
            </label>
            <label>
              Capacity GB
              <input
                name="size"
                type="number"
                min="1"
                max="1024"
                defaultValue="10"
              />
            </label>
            <label>
              Storage placement
              <select name="node_id" required defaultValue="">
                <option value="" disabled>Select an online node</option>
                {storageNodes.data?.map((node) => (
                  <option value={node.id} key={node.id}>
                    {node.name} · {node.availability_zone} · {Math.max(0, node.storage_total_gb - node.storage_allocated_gb)} GB free
                  </option>
                ))}
              </select>
              {!storageNodes.isLoading && !storageNodes.data?.length && (
                <small>Deploy agent 0.5.12 or newer to an online approved node to enable node-backed volume creation.</small>
              )}
            </label>
          </div>
          <div className="form-actions">
            <Button disabled={busy === "create" || !storageNodes.data?.length}>
              <Plus />
              Create volume
            </Button>
          </div>
        </form>
      </Card>
      <div className="resource-grid">
        {data.length === 0 ? (
          <Card>
            <Empty
              title="No volumes"
              detail="Create durable storage independent of a VM."
            />
          </Card>
        ) : (
          data.map((volume) => (
            <Card key={volume.id}>
              <div className="resource-title">
                <div>
                  <h3>{volume.name}</h3>
                  <Status
                    value={volume.attached_instance_id ? "RUNNING" : "STOPPED"}
                  />
                </div>
                <button
                  className="danger-icon"
                  disabled={Boolean(volume.attached_instance_id) || busy === volume.id}
                  onClick={async () => {
                    if (
                      confirm("Permanently delete this volume and its data?")
                    ) {
                      try {
                        setBusy(volume.id);
                        await api.delete(`/volumes/${volume.id}`);
                        refresh();
                      } catch (value) { setError(message(value)); }
                      finally { setBusy(""); }
                    }
                  }}
                >
                  <Trash2 />
                </button>
              </div>
              <p>
                {volume.size_gb} GB logical capacity ·{" "}
                {volume.attached_instance_id
                  ? `${volume.device_name ?? "block device"} mounted at ${volume.mount_path} in ${volume.attachment_zone ?? "assigned zone"}`
                  : `Stored on ${volume.current_node_name ?? volume.origin_node_name ?? "selected node"} · attachable across nodes`}
              </p>
              <div className="chips">
                <span>{volume.storage_backend.replaceAll("-", " ")}</span>
                <span>
                  Origin: {volume.origin_node_name ?? volume.availability_zone}
                </span>
                {volume.attachment_zone && (
                  <span>Attached in {volume.attachment_zone}</span>
                )}
              </div>
              <div>
                <progress value={volume.usage_percent} max="100" />
                <p>
                  {formatBytes(volume.used_bytes)} used ·{" "}
                  {formatBytes(volume.available_bytes)} remaining (
                  {volume.usage_percent}%)
                </p>
              </div>
              {volume.attached_instance_id ? (
                <Button
                  className="secondary"
                  disabled={busy === volume.id}
                  onClick={async () => {
                    try {
                      setBusy(volume.id);
                      await api.post(`/volumes/${volume.id}/detach`);
                      refresh();
                    } catch (value) {
                      setError(message(value));
                    } finally { setBusy(""); }
                  }}
                >
                  <Link2Off />
                  Detach
                </Button>
              ) : (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    const form = new FormData(event.currentTarget);
                    setBusy(volume.id);
                    setError("");
                    void api.post(`/volumes/${volume.id}/attach`, {
                      instance_id: form.get("instance"),
                      mount_path: form.get("path"),
                    }).then(refresh).catch((value) => setError(message(value))).finally(() => setBusy(""));
                  }}
                >
                  <label>
                    Instance
                    <select name="instance">
                      {instances.data
                        ?.filter((item) => item.status === "RUNNING")
                        .map((item) => (
                          <option value={item.id} key={item.id}>
                            {item.display_name}
                          </option>
                        ))}
                    </select>
                  </label>
                  <label>
                    Mount path
                    <input name="path" defaultValue="/mnt/data" />
                  </label>
                  <div className="form-actions">
                    <Button
                      disabled={busy === volume.id || !instances.data?.some((item) => item.status === "RUNNING")}
                    >
                      <Link />
                      Attach
                    </Button>
                  </div>
                </form>
              )}
            </Card>
          ))
        )}
      </div>
    </>
  );
}
