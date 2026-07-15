import {
  KeyRound,
  Monitor,
  MoreHorizontal,
  Play,
  Snowflake,
  Plus,
  SquareTerminal,
  StopCircle,
  Trash2,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { useQuery } from "@tanstack/react-query";
import { api, message } from "../api/client";
import {
  Button,
  Card,
  Empty,
  ErrorAlert,
  Loading,
  PageHeader,
  Status,
} from "../components/ui";
import {
  useInstanceAction,
  useInstances,
  useKeyPairs,
  useOperations,
  useSecurityGroups,
} from "../hooks/data";
import type { Instance, Operation, ZoneCapacity } from "../types";
function useKeyDownloads(operations: Operation[]) {
  const handled = useRef(new Set<string>());
  useEffect(() => {
    for (const operation of operations) {
      const token = operation.result_payload.key_download_token;
      if (
        operation.status !== "SUCCEEDED" ||
        !token ||
        handled.current.has(token)
      )
        continue;
      handled.current.add(token);
      void api
        .get(`/key-downloads/${token}`, { responseType: "blob" })
        .then(({ data }) => {
          const url = URL.createObjectURL(data as Blob);
          const link = document.createElement("a");
          link.href = url;
          link.download =
            operation.result_payload.key_filename ?? "localec2-key.pem";
          link.click();
          URL.revokeObjectURL(url);
        });
    }
  }, [operations]);
}
async function downloadKey(token: string, filename: string) {
  const { data } = await api.get(`/key-downloads/${token}`, { responseType: "blob" });
  const url = URL.createObjectURL(data as Blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
export function InstancesPage() {
  const { data = [], isLoading, error } = useInstances();
  const { data: operations = [] } = useOperations();
  useKeyDownloads(operations);
  const action = useInstanceAction();
  const run = (
    id: string,
    kind: "start" | "stop" | "hibernate" | "retry" | "terminate",
  ) => {
    if (
      kind === "terminate" &&
      !confirm("Terminate this instance permanently?")
    )
      return;
    action.mutate({ id, action: kind });
  };
  return (
    <>
      <PageHeader
        title="Instances"
        description="Virtual machines placed into administrator-managed availability zones."
        action={
          <Link className="button" to="/instances/new">
            <Plus />
            Launch instance
          </Link>
        }
      />
      {error && <ErrorAlert>{message(error)}</ErrorAlert>}
      <Card>
        {isLoading ? (
          <Loading />
        ) : data.length === 0 ? (
          <Empty
            title="No instances yet"
            detail="Launch after an administrator makes cloud capacity available."
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th>IP address</th>
                  <th>Resources</th>
                  <th>Key</th>
                  <th>Created</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <Link to={`/instances/${item.id}`}>
                        <strong>{item.display_name}</strong>
                      </Link>
                      <small>{item.description}</small>
                    </td>
                    <td>
                      <Status value={item.status} />
                    </td>
                    <td>
                      <code>{item.ipv4_address ?? "—"}</code>
                    </td>
                    <td>
                      {item.cpu_count} vCPU · {item.memory_mb / 1024} GB ·{" "}
                      {item.disk_gb} GB
                    </td>
                    <td>
                      {item.has_key_pair ? (
                        <KeyRound aria-label="SSH key" />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{new Date(item.created_at).toLocaleDateString()}</td>
                    <td>
                      <div className="row-actions">
                        <Link title="Details" to={`/instances/${item.id}`}>
                          <MoreHorizontal />
                        </Link>
                        <Link
                          aria-disabled={item.status !== "RUNNING"}
                          title="SSH instructions"
                          to={`/instances/${item.id}/connect`}
                        >
                          <SquareTerminal />
                        </Link>
                        <Link
                          title="Monitor"
                          to={`/instances/${item.id}/monitor`}
                        >
                          <Monitor />
                        </Link>
                        {["STOPPED", "HIBERNATED"].includes(item.status) && (
                          <button
                            title="Start"
                            onClick={() => run(item.id, "start")}
                          >
                            <Play />
                          </button>
                        )}
                        {["ERROR", "UNKNOWN"].includes(item.status) && (
                          <button
                            title="Retry launch"
                            onClick={() => run(item.id, "retry")}
                          >
                            <Play />
                          </button>
                        )}
                        {item.status === "RUNNING" && (
                          <>
                            <button
                              title="Hibernate"
                              onClick={() => run(item.id, "hibernate")}
                            >
                              <Snowflake />
                            </button>
                            <button
                              title="Stop"
                              onClick={() => run(item.id, "stop")}
                            >
                              <StopCircle />
                            </button>
                          </>
                        )}
                        <button
                          className="danger-icon"
                          title="Terminate"
                          onClick={() => run(item.id, "terminate")}
                        >
                          <Trash2 />
                        </button>
                      </div>
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
type Launch = {
  name: string;
  description: string;
  image: string;
  cpu_count: number;
  memory_mb: number;
  disk_gb: number;
  create_key_pair: boolean;
  instance_type: string;
  key_pair_id: string;
  security_group_ids: string[];
  availability_zone: string;
};
export function LaunchPage() {
  const nav = useNavigate();
  const {
    register,
    watch,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Launch>({
    defaultValues: {
      image: "24.04",
      cpu_count: 2,
      memory_mb: 2048,
      disk_gb: 10,
      create_key_pair: true,
      instance_type: "t2.small",
      key_pair_id: "",
      security_group_ids: [],
      availability_zone: "",
    },
  });
  const [cpu, memory, disk, instanceType] = watch([
    "cpu_count",
    "memory_mb",
    "disk_gb",
    "instance_type",
  ]);
  const preset = {
    "t2.micro": [1, 1024],
    "t2.small": [1, 2048],
    "t2.medium": [2, 4096],
    "t4.micro": [2, 1024],
    "t4.small": [2, 2048],
    "t4.medium": [2, 4096],
    "m5.large": [2, 8192],
    "m5.xlarge": [4, 16384],
  }[instanceType];
  const allocatedCpu = preset?.[0] ?? cpu;
  const allocatedMemory = preset?.[1] ?? memory;
  const keyPairs = useKeyPairs();
  const securityGroups = useSecurityGroups();
  const zones = useQuery({
    queryKey: ["capacity-zones"],
    queryFn: () => api.get<ZoneCapacity[]>("/instances/capacity/zones").then(response => response.data),
    refetchInterval: 10000,
  });
  const requestedZone = watch("availability_zone");
  const eligibleZones = (zones.data ?? []).filter((zone) => zone.available);
  const requestedCapacityAvailable = requestedZone
    ? eligibleZones.some((zone) => zone.availability_zone === requestedZone)
    : eligibleZones.length > 0;
  const submit = handleSubmit(async (values) => {
    const { data } = await api.post<{ instance_id: string }>("/instances", {
      ...values,
      key_pair_id: values.key_pair_id || null,
      create_key_pair: values.key_pair_id ? false : values.create_key_pair,
      availability_zone: values.availability_zone || null,
    });
    nav(`/instances/${data.instance_id}`);
  });
  return (
    <>
      <PageHeader
        title="Launch instance"
        description="Choose a failure domain; the scheduler reserves an approved node before launch."
      />
      <div className="form-grid">
        <Card>
          <form onSubmit={submit}>
            <label>
              Name
              <input
                {...register("name", {
                  required: true,
                  pattern: /^[A-Za-z0-9 _-]{1,40}$/,
                })}
              />
              <small>
                {errors.name &&
                  "Use 1–40 letters, numbers, spaces, hyphens, or underscores."}
              </small>
            </label>
            <label>
              Description
              <textarea {...register("description", { maxLength: 500 })} />
            </label>
            <div className="fields">
              <label>
                Availability zone
                <select {...register("availability_zone")}>
                  <option value="">Automatic placement</option>
                  {zones.data?.map(zone => <option disabled={!zone.available} value={zone.availability_zone} key={zone.availability_zone}>{zone.availability_zone}{zone.available ? "" : " · unavailable"}</option>)}
                </select>
                {!requestedCapacityAvailable && <small>No approved capacity is currently available in this placement.</small>}
              </label>
              <label>
                Machine image
                <select {...register("image")}>
                  <option value="24.04">Ubuntu 24.04 LTS</option>
                  <option value="22.04">Ubuntu 22.04 LTS</option>
                  <option value="debian-12">Debian 12</option>
                </select>
                <small>Custom distributed images require replicated image storage and are not enabled yet.</small>
              </label>
              <label>
                Instance type
                <select {...register("instance_type")}>
                  <optgroup label="Burstable T2">
                    <option value="t2.micro">t2.micro · 1 vCPU · 1 GB</option>
                    <option value="t2.small">t2.small · 1 vCPU · 2 GB</option>
                    <option value="t2.medium">t2.medium · 2 vCPU · 4 GB</option>
                  </optgroup>
                  <optgroup label="Burstable T4">
                    <option value="t4.micro">t4.micro · 2 vCPU · 1 GB</option>
                    <option value="t4.small">t4.small · 2 vCPU · 2 GB</option>
                    <option value="t4.medium">t4.medium · 2 vCPU · 4 GB</option>
                  </optgroup>
                  <optgroup label="General purpose M">
                    <option value="m5.large">m5.large · 2 vCPU · 8 GB</option>
                    <option value="m5.xlarge">
                      m5.xlarge · 4 vCPU · 16 GB
                    </option>
                  </optgroup>
                  <option value="custom">Custom</option>
                </select>
              </label>
              <label>
                vCPUs
                <input
                  type="number"
                  min="1"
                  max="8"
                  {...register("cpu_count", { valueAsNumber: true })}
                />
              </label>
              <label>
                Memory (MB)
                <input
                  type="number"
                  min="512"
                  max="16384"
                  step="512"
                  {...register("memory_mb", { valueAsNumber: true })}
                />
              </label>
              <label>
                Disk (GB)
                <input
                  type="number"
                  min="5"
                  max="100"
                  {...register("disk_gb", { valueAsNumber: true })}
                />
              </label>
            </div>
            <div className="fields">
              <label>
                Reusable key pair
                <select {...register("key_pair_id")}>
                  <option value="">Generate one-time key</option>
                  {keyPairs.data?.map((key) => (
                    <option value={key.id} key={key.id}>
                      {key.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <fieldset className="security-group-picker">
              <legend>Security groups</legend>
              <p>Select one or more inbound firewall policies.</p>
              {securityGroups.isLoading ? (
                <span className="security-group-empty">
                  Loading security groups…
                </span>
              ) : securityGroups.data?.length ? (
                <div className="security-group-options">
                  {securityGroups.data.map((group) => (
                    <label className="security-group-option" key={group.id}>
                      <input
                        type="checkbox"
                        value={group.id}
                        {...register("security_group_ids")}
                      />
                      <span>
                        <strong>{group.name}</strong>
                        <small>
                          {group.description ||
                            `${group.ingress_rules.length} inbound rule${group.ingress_rules.length === 1 ? "" : "s"}`}
                        </small>
                      </span>
                    </label>
                  ))}
                </div>
              ) : (
                <span className="security-group-empty">
                  No security groups yet.{" "}
                  <Link to="/security-groups">Create one</Link>
                </span>
              )}
            </fieldset>
            <label className="check">
              <input type="checkbox" {...register("create_key_pair")} /> Create
              a unique SSH key pair
            </label>
            <div className="form-actions">
              <Link to="/instances">Cancel</Link>
              <Button disabled={isSubmitting || !requestedCapacityAvailable}>
                {isSubmitting ? "Queuing…" : "Launch instance"}
              </Button>
            </div>
          </form>
        </Card>
        <Card className="estimate">
          <h3>Requested allocation</h3>
          <div>
            <strong>{allocatedCpu}</strong>
            <span>vCPUs</span>
          </div>
          <div>
            <strong>{(allocatedMemory / 1024).toFixed(1)} GB</strong>
            <span>memory</span>
          </div>
          <div>
            <strong>{disk} GB</strong>
            <span>disk</span>
          </div>
          <p>
            The control plane reserves these resources inside the selected
            availability zone. Launch is rejected when placement cannot be satisfied.
          </p>
        </Card>
      </div>
    </>
  );
}
export function InstanceDetailsPage() {
  const { instanceId = "" } = useParams();
  const { data: item, isLoading } = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () =>
      api.get<Instance>(`/instances/${instanceId}`).then((r) => r.data),
    refetchInterval: 3000,
  });
  const { data: operations = [] } = useOperations();
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState("");
  useKeyDownloads(
    operations.filter((operation) => operation.instance_id === instanceId),
  );
  if (isLoading || !item) return <Loading />;
  const active = operations.find(
    (x) =>
      x.instance_id === item.id && ["PENDING", "RUNNING"].includes(x.status),
  );
  return (
    <>
      <PageHeader
        title={item.display_name}
        description={item.description || "No description"}
        action={
          <div className="header-actions">
            <Link
              className="button secondary"
              to={`/instances/${item.id}/monitor`}
            >
              <Monitor />
              Monitor
            </Link>
            <Link className="button" to={`/instances/${item.id}/connect`}>
              <SquareTerminal />
              Connect
            </Link>
          </div>
        }
      />
      {active && (
        <Card className="operation-banner">
          <div>
            <Status value={active.status} />
            <strong>{active.message}</strong>
          </div>
          <progress value={active.progress} max="100" />
          <span>{active.progress}%</span>
        </Card>
      )}
      <div className="details-grid">
        {keyError && <ErrorAlert>{keyError}</ErrorAlert>}
        <Card>
          <h2>Overview</h2>
          <dl>
            <dt>Status</dt>
            <dd>
              <Status value={item.status} />
            </dd>
            <dt>IP address</dt>
            <dd>
              <code>{item.ipv4_address ?? "Not assigned"}</code>
            </dd>
            <dt>SSH command</dt>
            <dd>
              <code>ssh {item.ssh_username}@{item.ipv4_address ?? "<pending>"}</code>
            </dd>
            <dt>Created</dt>
            <dd>{new Date(item.created_at).toLocaleString()}</dd>
            <dt>Availability zone</dt>
            <dd>{item.availability_zone}</dd>
          </dl>
        </Card>
        <Card>
          <h2>Configuration</h2>
          <dl>
            <dt>Image</dt>
            <dd>{item.image === "debian-12" ? "Debian 12" : `Ubuntu ${item.image}`}</dd>
            <dt>Compute</dt>
            <dd>
              {item.cpu_count} vCPU · {item.memory_mb / 1024} GB RAM
            </dd>
            <dt>Storage</dt>
            <dd>{item.disk_gb} GB</dd>
            <dt>Key fingerprint</dt>
            <dd className="break">{item.key_fingerprint ?? "No key pair"}</dd>
            {item.status === "RUNNING" && (
              <>
                <dt>Private key</dt>
                <dd>
                  <Button
                    className="secondary"
                    disabled={keyBusy}
                    onClick={async () => {
                      setKeyBusy(true);
                      setKeyError("");
                      try {
                        const { data } = await api.post<{
                          key_download_token: string;
                          key_filename: string;
                        }>(`/instances/${item.id}/ssh-key`);
                        await downloadKey(data.key_download_token, data.key_filename);
                      } catch (error) {
                        setKeyError(message(error));
                      } finally {
                        setKeyBusy(false);
                      }
                    }}
                  >
                    <KeyRound />
                    {keyBusy ? "Installing…" : "Generate replacement key"}
                  </Button>
                </dd>
              </>
            )}
          </dl>
        </Card>
      </div>
      <Card>
        <h2>Recent operations</h2>
        {operations
          .filter((x) => x.instance_id === item.id)
          .slice(0, 6)
          .map((op) => (
            <div className="operation-row" key={op.id}>
              <Status value={op.status} />
              <strong>{op.operation_type}</strong>
              <span>{op.error_message ?? op.message}</span>
              <span>{op.progress}%</span>
            </div>
          ))}
      </Card>
    </>
  );
}
