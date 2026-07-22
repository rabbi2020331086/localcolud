import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Boxes,
  Cpu,
  HardDrive,
  KeyRound,
  MapPin,
  MemoryStick,
  Network,
  Pencil,
  Plus,
  ScrollText,
  Trash2,
} from "lucide-react";
import { api, message } from "../api/client";
import { Button, Card, Empty, ErrorAlert, PageHeader } from "../components/ui";
import { useKeyPairs, useSecurityGroups, useTemplates } from "../hooks/data";
import type { Template, ZoneCapacity } from "../types";

const INSTANCE_TYPES: Record<string, [number, number]> = {
  "t2.micro": [1, 1024],
  "t2.small": [1, 2048],
  "t2.medium": [2, 4096],
  "t4.micro": [2, 1024],
  "t4.small": [2, 2048],
  "t4.medium": [2, 4096],
  "m5.large": [2, 8192],
  "m5.xlarge": [4, 16384],
};

function TemplateEditor({
  initial,
  onClose,
}: {
  initial?: Template;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const keys = useKeyPairs();
  const securityGroups = useSecurityGroups();
  const zones = useQuery({
    queryKey: ["capacity-zones"],
    queryFn: () =>
      api.get<ZoneCapacity[]>("/instances/capacity/zones").then((response) => response.data),
  });
  const [error, setError] = useState("");
  const [keyMode, setKeyMode] = useState(
    initial?.key_pair_id ? "reusable" : initial?.create_key_pair === false ? "none" : "one_time",
  );
  const [instanceType, setInstanceType] = useState(initial?.instance_type ?? "t2.small");
  const save = useMutation({
    mutationFn: (form: FormData) => {
      const preset = INSTANCE_TYPES[String(form.get("instance_type"))];
      const payload = {
        name: form.get("name"),
        description: form.get("description"),
        image: form.get("image"),
        instance_type: form.get("instance_type"),
        cpu_count: preset?.[0] ?? Number(form.get("cpu")),
        memory_mb: preset?.[1] ?? Number(form.get("memory")),
        disk_gb: Number(form.get("disk")),
        availability_zones: form.getAll("availability_zones"),
        security_group_ids: form.getAll("security_group_ids"),
        user_data: form.get("user_data"),
        key_pair_id: keyMode === "reusable" ? form.get("key_pair") : null,
        create_key_pair: keyMode === "one_time",
      };
      return initial
        ? api.patch(`/launch-templates/${initial.id}`, payload)
        : api.post("/launch-templates", payload);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["templates"] });
      onClose();
    },
    onError: (value) => setError(message(value)),
  });
  const preset = INSTANCE_TYPES[instanceType];
  return (
    <Card className="template-editor-card">
      <div className="template-editor-heading">
        <div><span className="eyebrow">Launch configuration</span><h2>{initial ? `Edit ${initial.name}` : "Create launch template"}</h2><p>Define one immutable configuration for every instance launched from this template.</p></div>
        <Boxes />
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <form onSubmit={(event) => { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }}>
        <div className="fields">
          <label>Template name<input name="name" required defaultValue={initial?.name} /></label>
          <label>Description<input name="description" defaultValue={initial?.description} /></label>
          <label>Machine image<select name="image" defaultValue={initial?.image ?? "24.04"}><option value="24.04">Ubuntu 24.04 LTS</option><option value="22.04">Ubuntu 22.04 LTS</option><option value="debian-12">Debian 12</option></select></label>
        </div>
        <div className="template-form-section">
          <div><h3>Compute profile</h3><p>Select a standard instance type or define a custom allocation.</p></div>
          <div className="fields">
            <label>Instance type<select name="instance_type" value={instanceType} onChange={(event) => setInstanceType(event.target.value)}><optgroup label="Burstable T2"><option value="t2.micro">t2.micro · 1 vCPU · 1 GB</option><option value="t2.small">t2.small · 1 vCPU · 2 GB</option><option value="t2.medium">t2.medium · 2 vCPU · 4 GB</option></optgroup><optgroup label="Burstable T4"><option value="t4.micro">t4.micro · 2 vCPU · 1 GB</option><option value="t4.small">t4.small · 2 vCPU · 2 GB</option><option value="t4.medium">t4.medium · 2 vCPU · 4 GB</option></optgroup><optgroup label="General purpose"><option value="m5.large">m5.large · 2 vCPU · 8 GB</option><option value="m5.xlarge">m5.xlarge · 4 vCPU · 16 GB</option></optgroup><option value="custom">Custom allocation</option></select></label>
            <label>vCPUs<input name="cpu" type="number" min="1" max="8" disabled={Boolean(preset)} defaultValue={preset?.[0] ?? initial?.cpu_count ?? 2} /></label>
            <label>Memory (MB)<input name="memory" type="number" min="1024" max="16384" step="512" disabled={Boolean(preset)} defaultValue={preset?.[1] ?? initial?.memory_mb ?? 2048} /></label>
            <label>Root disk (GB)<input name="disk" type="number" min="5" max="100" defaultValue={initial?.disk_gb ?? 10} /></label>
          </div>
        </div>
        <div className="template-form-section">
          <div><h3>Placement and networking</h3><p>Choose eligible zones and firewall policies.</p></div>
          <fieldset className="security-group-picker"><legend>Availability zones</legend><div className="security-group-options">{zones.data?.map((zone) => <label className="security-group-option" key={zone.availability_zone}><input type="checkbox" name="availability_zones" value={zone.availability_zone} defaultChecked={initial?.availability_zones.includes(zone.availability_zone)} disabled={!zone.available}/><span><strong>{zone.availability_zone}</strong><small>{zone.available ? "Capacity available" : "Unavailable"}</small></span></label>)}</div></fieldset>
          <fieldset className="security-group-picker"><legend>Security groups</legend><div className="security-group-options">{securityGroups.data?.map((group) => <label className="security-group-option" key={group.id}><input type="checkbox" name="security_group_ids" value={group.id} defaultChecked={initial?.security_group_ids.includes(group.id)}/><span><strong>{group.name}</strong><small>{group.ingress_rules.length} inbound rules</small></span></label>)}</div></fieldset>
        </div>
        <div className="fields">
          <label>SSH key policy<select value={keyMode} onChange={(event) => setKeyMode(event.target.value)}><option value="one_time">Generate a one-time key per instance</option><option value="reusable">Use a reusable key pair</option><option value="none">No SSH key</option></select></label>
          {keyMode === "reusable" && <label>Reusable key pair<select name="key_pair" required defaultValue={initial?.key_pair_id ?? ""}><option value="" disabled>Select a key pair</option>{keys.data?.map((key) => <option value={key.id} key={key.id}>{key.name}</option>)}</select></label>}
        </div>
        <label>Startup commands<textarea name="user_data" rows={8} defaultValue={initial?.user_data ?? ""}/><small>Executed once as root before endpoint publishing.</small></label>
        <div className="form-actions"><Button type="button" className="secondary" onClick={onClose}>Cancel</Button><Button disabled={save.isPending}>{save.isPending ? "Saving…" : initial ? "Save changes" : "Create template"}</Button></div>
      </form>
    </Card>
  );
}

export function TemplatesPage() {
  const templates = useTemplates();
  const qc = useQueryClient();
  const [editor, setEditor] = useState<"new" | Template | null>(null);
  const [error, setError] = useState("");
  const remove = async (template: Template) => {
    if (!confirm(`Delete launch template “${template.name}”?`)) return;
    try {
      await api.delete(`/launch-templates/${template.id}`);
      void qc.invalidateQueries({ queryKey: ["templates"] });
    } catch (value) { setError(message(value)); }
  };
  return <>
    <PageHeader title="Launch templates" description="Version-ready machine configurations for consistent manual and autoscaled launches." action={<Button onClick={() => setEditor("new")}><Plus/>New template</Button>}/>
    {error && <ErrorAlert>{error}</ErrorAlert>}
    {templates.error && <ErrorAlert>Could not load launch templates: {message(templates.error)}</ErrorAlert>}
    {editor && <TemplateEditor initial={editor === "new" ? undefined : editor} onClose={() => setEditor(null)}/>} 
    {templates.isLoading ? <Card><div className="loading">Loading launch templates…</div></Card> : !templates.data?.length ? <Card><Empty title="No launch templates" detail="Create a reusable compute, placement, network, and startup configuration."/></Card> : <div className="template-card-grid">{templates.data.map((item) => <Card className="template-resource-card" key={item.id}><div className="template-card-head"><div className="template-icon"><Boxes/></div><div><h2>{item.name}</h2><p>{item.description || "Reusable instance configuration"}</p></div><div className="template-card-actions"><button title="Edit template" onClick={() => setEditor(item)}><Pencil/></button><button className="danger-icon" title="Delete template" onClick={() => void remove(item)}><Trash2/></button></div></div><div className="template-specs"><div><Cpu/><span>Instance type</span><strong>{item.instance_type}</strong><small>{item.cpu_count} vCPU</small></div><div><MemoryStick/><span>Memory</span><strong>{(item.memory_mb / 1024).toFixed(1)} GB</strong><small>Guest allocation</small></div><div><HardDrive/><span>Root disk</span><strong>{item.disk_gb} GB</strong><small>{item.image === "debian-12" ? "Debian 12" : `Ubuntu ${item.image}`}</small></div></div><div className="template-meta"><span><MapPin/>{item.availability_zones.length ? item.availability_zones.join(", ") : "Automatic placement"}</span><span><Network/>{item.security_group_ids.length} security group{item.security_group_ids.length === 1 ? "" : "s"}</span><span><KeyRound/>{item.key_pair_id ? "Reusable SSH key" : item.create_key_pair ? "One-time SSH keys" : "No SSH key"}</span><span><ScrollText/>{item.user_data ? "Startup configured" : "No startup commands"}</span></div><Button className="secondary template-edit-button" onClick={() => setEditor(item)}><Pencil/>Edit configuration</Button></Card>)}</div>}
  </>;
}
