import { Activity, CloudOff, Server, Timer, Workflow } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Card, Loading, PageHeader, Status } from "../components/ui";
import { useGroups, useInstances, useOperations } from "../hooks/data";
export function Dashboard() {
  const instances = useInstances();
  const operations = useOperations();
  const groups = useGroups();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () =>
      api
        .get<{
          status: string;
          multipass: boolean;
        }>("http://localhost:8000/health/readiness")
        .then((r) => r.data),
    refetchInterval: 15000,
  });
  if (instances.isLoading) return <Loading />;
  const all = instances.data ?? [];
  return (
    <>
      <PageHeader
        title="Overview"
        description="Your local cloud at a glance."
      />
      {health.data && !health.data.multipass && (
        <div className="provider-alert">
          <CloudOff />
          Multipass is unavailable. Install or start Multipass to use VM
          actions.
        </div>
      )}
      <div className="summary">
        <Card>
          <Server />
          <span>Total instances</span>
          <strong>{all.length}</strong>
        </Card>
        <Card>
          <Activity />
          <span>Running</span>
          <strong>{all.filter((x) => x.status === "RUNNING").length}</strong>
        </Card>
        <Card>
          <Timer />
          <span>In transition</span>
          <strong>
            {
              all.filter((x) =>
                [
                  "PROVISIONING",
                  "STARTING",
                  "STOPPING",
                  "HIBERNATING",
                  "TERMINATING",
                ].includes(x.status),
              ).length
            }
          </strong>
        </Card>
        <Card>
          <Workflow />
          <span>Autoscaling groups</span>
          <strong>{groups.data?.filter((x) => x.enabled).length ?? 0}</strong>
        </Card>
      </div>
      <div className="dashboard-grid">
        <Card>
          <h2>Recent operations</h2>
          {operations.data?.slice(0, 6).map((op) => (
            <div className="operation-row" key={op.id}>
              <Status value={op.status} />
              <strong>{op.operation_type}</strong>
              <span>{op.message}</span>
            </div>
          ))}
        </Card>
        <Card>
          <h2>Provider</h2>
          <div className="provider-state">
            <span className={health.data?.multipass ? "orb good" : "orb"} />
            <div>
              <strong>
                {health.data?.multipass
                  ? "Multipass ready"
                  : "Multipass unavailable"}
              </strong>
                <p>VMs run locally on this Ubuntu computer.</p>
            </div>
          </div>
        </Card>
      </div>
    </>
  );
}
