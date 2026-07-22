import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';
import { api, message } from '../api/client';
import { Button, Card, Empty, ErrorAlert, PageHeader, Status } from '../components/ui';
import { useGroups, useKeyPairs, useSecurityGroups, useTemplates } from '../hooks/data';
import type { ScalingEvent, ZoneCapacity } from '../types';
export { TemplatesPage } from './Templates';

export function LegacyTemplatesPage() {
  const templates = useTemplates();
  const data = templates.data ?? [];
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [keyMode, setKeyMode] = useState('one_time');
  const keys = useKeyPairs();
  const securityGroups = useSecurityGroups();
  const zones = useQuery({
    queryKey: ['capacity-zones'],
    queryFn: () => api.get<ZoneCapacity[]>('/instances/capacity/zones').then(response => response.data),
  });
  const create = useMutation({
    mutationFn: (body: FormData) => api.post('/launch-templates', {
      name: body.get('name'), description: body.get('description'), image: body.get('image'),
      cpu_count: Number(body.get('cpu')), memory_mb: Number(body.get('memory')),
      disk_gb: Number(body.get('disk')),
      instance_type: String(body.get('instance_type') || 'custom'),
      availability_zones: body.getAll('availability_zones'),
      security_group_ids: body.getAll('security_group_ids'),
      user_data: body.get('user_data'),
      key_pair_id: body.get('key_mode') === 'reusable' ? body.get('key_pair') : null,
      create_key_pair: body.get('key_mode') === 'one_time',
    }),
    onSuccess: () => {
      setOpen(false);
      setError('');
      void qc.invalidateQueries({ queryKey: ['templates'] });
    },
    onError: (errorValue) => setError(message(errorValue)),
  });
  return <>
    <PageHeader title="Launch templates" description="Reusable VM configurations for manual and autoscaled launches." action={<Button onClick={() => setOpen(true)}><Plus />New template</Button>} />
    {error && <ErrorAlert>{error}</ErrorAlert>}
    {templates.error && <ErrorAlert>Could not load launch templates: {message(templates.error)}</ErrorAlert>}
    {open && <Card><form onSubmit={(event) => { event.preventDefault(); create.mutate(new FormData(event.currentTarget)); }}><div className="fields"><label>Name<input name="name" required /></label><label>Description<input name="description" /></label><label>Image<select name="image"><option value="24.04">Ubuntu 24.04</option><option value="22.04">Ubuntu 22.04</option><option value="debian-12">Debian 12</option></select></label><label>Instance type<input name="instance_type" defaultValue="custom" /></label><label>vCPU<input name="cpu" type="number" min="1" max="8" defaultValue="2" /></label><label>Memory MB<input name="memory" type="number" min="1024" max="16384" defaultValue="2048" /></label><label>Disk GB<input name="disk" type="number" min="5" max="100" defaultValue="10" /></label><label>SSH key policy<select name="key_mode" value={keyMode} onChange={(event) => setKeyMode(event.target.value)}><option value="one_time">Generate one-time key per instance</option><option value="reusable">Use reusable key pair</option><option value="none">No SSH key</option></select></label>{keyMode === 'reusable' && <label>Reusable key pair<select name="key_pair" required>{keys.data?.map((key) => <option key={key.id} value={key.id}>{key.name}</option>)}</select></label>}</div><fieldset className="security-group-picker"><legend>Availability zones</legend><p>Select multiple zones for balanced autoscaling placement. Leave empty for automatic placement.</p><div className="security-group-options">{zones.data?.map(zone => <label className="security-group-option" key={zone.availability_zone}><input type="checkbox" name="availability_zones" value={zone.availability_zone} disabled={!zone.available} /><span><strong>{zone.availability_zone}</strong><small>{zone.available ? 'Capacity available' : 'Currently unavailable'}</small></span></label>)}</div></fieldset><fieldset className="security-group-picker"><legend>Security groups</legend><p>Applied to every instance created from this template.</p><div className="security-group-options">{securityGroups.data?.map(group => <label className="security-group-option" key={group.id}><input type="checkbox" name="security_group_ids" value={group.id} /><span><strong>{group.name}</strong><small>{group.ingress_rules.length} inbound rules</small></span></label>)}</div></fieldset><label>Startup commands<textarea name="user_data" rows={7} defaultValue={"sudo apt-get update\nsudo apt-get install -y nginx\necho \"healthy\" | sudo tee /var/www/html/health\nprintf '<h1>Served by %s</h1>\\n' \"$(hostname)\" | sudo tee /var/www/html/index.html\nsudo systemctl enable --now nginx"} /><small>Runs once inside every autoscaled VM after launch. A failure marks the launch operation failed.</small></label><div className="form-actions"><Button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button disabled={create.isPending || (keyMode === 'reusable' && !keys.data?.length)}>Create template</Button></div></form></Card>}
    <div className="resource-grid">{templates.isLoading ? <Card><div className="loading">Loading launch templates…</div></Card> : !templates.error && data.length === 0 ? <Card><Empty title="No templates" detail="Create a distributed launch template with placement, networking, compute, and key policies." /></Card> : data.map((item) => <Card key={item.id}><div className="resource-title"><div><h3>{item.name}</h3><p>{item.description}</p></div><button className="danger-icon" onClick={async () => { if (confirm('Delete this template?')) { await api.delete(`/launch-templates/${item.id}`); void qc.invalidateQueries({ queryKey: ['templates'] }); } }}><Trash2 /></button></div><div className="chips"><span>Ubuntu {item.image}</span><span>{item.instance_type}</span><span>{item.cpu_count} vCPU</span><span>{item.memory_mb / 1024} GB</span><span>{item.disk_gb} GB disk</span><span>{item.availability_zones.length ? item.availability_zones.join(', ') : 'Automatic zones'}</span><span>{item.security_group_ids.length} security groups</span><span>{item.key_pair_id ? 'Reusable SSH key' : item.create_key_pair ? 'One-time SSH keys' : 'No SSH key'}</span>{item.user_data && <span>Startup configured</span>}</div></Card>)}</div>
  </>;
}

export function AutoscalingPage() {
  const groups = useGroups();
  const data = groups.data ?? [];
  const templates = useTemplates();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const events = useQuery({
    queryKey: ['scaling-events', selected],
    queryFn: () => api.get<ScalingEvent[]>(`/autoscaling-groups/${selected}/events`).then(response => response.data),
    enabled: Boolean(selected),
    refetchInterval: 5000,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['groups'] });
    void qc.invalidateQueries({ queryKey: ['instances'] });
    void qc.invalidateQueries({ queryKey: ['operations'] });
  };
  const create = useMutation({
    mutationFn: (form: FormData) => api.post('/autoscaling-groups', {
      name: form.get('name'), launch_template_id: form.get('template'),
      minimum_size: Number(form.get('min')), desired_size: Number(form.get('desired')),
      maximum_size: Number(form.get('max')), target_cpu_percent: 50,
      scale_out_threshold: Number(form.get('out')), scale_in_threshold: Number(form.get('in')),
      evaluation_periods: 3, cooldown_seconds: 300, enabled: true,
    }),
    onSuccess: () => { setOpen(false); setError(''); refresh(); },
    onError: (errorValue) => setError(message(errorValue)),
  });
  const toggle = useMutation({
    mutationFn: (group: typeof data[number]) => api.patch(`/autoscaling-groups/${group.id}`, {
      name: group.name, launch_template_id: group.launch_template_id,
      minimum_size: group.minimum_size, desired_size: group.desired_size,
      maximum_size: group.maximum_size, target_cpu_percent: group.target_cpu_percent,
      scale_out_threshold: group.scale_out_threshold, scale_in_threshold: group.scale_in_threshold,
      evaluation_periods: group.evaluation_periods, cooldown_seconds: group.cooldown_seconds,
      enabled: !group.enabled,
    }),
    onSuccess: () => { setError(''); refresh(); },
    onError: (errorValue) => setError(message(errorValue)),
  });
  const desired = useMutation({
    mutationFn: ({ id, size }: { id: string; size: number }) =>
      api.post(`/autoscaling-groups/${id}/set-desired-capacity`, { desired_size: size }),
    onSuccess: () => { setError(''); refresh(); },
    onError: (errorValue) => setError(message(errorValue)),
  });
  const remove = async (id: string) => {
    if (!confirm('Delete this autoscaling group and terminate all instances it manages?')) return;
    try {
      await api.delete(`/autoscaling-groups/${id}`, {
        data: { terminate_managed_instances: true },
      });
      setError('');
      refresh();
    } catch (errorValue) {
      setError(message(errorValue));
    }
  };
  return <>
    <PageHeader title="Autoscaling" description="Maintain desired capacity across approved nodes and react to sustained guest CPU thresholds." action={<Button disabled={!templates.data?.length} onClick={() => setOpen(true)}><Plus />New group</Button>} />
    {error && <ErrorAlert>{error}</ErrorAlert>}
    {groups.error && <ErrorAlert>Could not load autoscaling groups: {message(groups.error)}</ErrorAlert>}
    {open && <Card><form onSubmit={(event) => { event.preventDefault(); create.mutate(new FormData(event.currentTarget)); }}><div className="fields"><label>Name<input name="name" required /></label><label>Launch template<select name="template">{templates.data?.map((template) => <option value={template.id} key={template.id}>{template.name}</option>)}</select></label><label>Minimum<input name="min" type="number" min="0" defaultValue="1" /></label><label>Desired<input name="desired" type="number" min="0" defaultValue="1" /></label><label>Maximum<input name="max" type="number" min="1" max="20" defaultValue="3" /></label><label>Scale out CPU %<input name="out" type="number" defaultValue="70" /></label><label>Scale in CPU %<input name="in" type="number" defaultValue="25" /></label></div><div className="form-actions"><Button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button disabled={create.isPending}>Create group</Button></div></form></Card>}
    <div className="resource-grid">{groups.isLoading ? <Card><div className="loading">Loading autoscaling groups…</div></Card> : !groups.error && data.length === 0 ? <Card><Empty title="No autoscaling groups" detail={templates.data?.length ? 'Create a group from a launch template.' : 'Create a launch template first.'} /></Card> : data.map((group) => <Card key={group.id}><div className="resource-title"><div><h3>{group.name}</h3><Status value={group.enabled ? 'RUNNING' : 'STOPPED'} /></div><button className="danger-icon" title="Delete group and terminate managed instances" onClick={() => void remove(group.id)}><Trash2 /></button></div><div className="capacity"><span>Min <strong>{group.minimum_size}</strong></span><span>Desired <strong>{group.desired_size}</strong></span><span>Max <strong>{group.maximum_size}</strong></span></div><p>Scale in below {group.scale_in_threshold}% · scale out above {group.scale_out_threshold}% · {group.evaluation_periods} evaluation periods · {group.cooldown_seconds}s cooldown</p><form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); desired.mutate({ id: group.id, size: Number(form.get('desired')) }); }}><label>Desired capacity<input name="desired" type="number" min={group.minimum_size} max={group.maximum_size} defaultValue={group.desired_size} /></label><div className="form-actions"><Button disabled={desired.isPending}>Update capacity</Button><Button type="button" className="secondary" disabled={toggle.isPending} onClick={() => toggle.mutate(group)}>{group.enabled ? 'Pause scaling' : 'Resume scaling'}</Button><Button type="button" className="secondary" onClick={() => setSelected(selected === group.id ? null : group.id)}>Activity history</Button></div></form>{selected === group.id && <div className="operation-list">{events.data?.length ? events.data.map(event => <div className="operation-row" key={event.id}><Status value={event.status} /><strong>{event.action}</strong><span>{event.reason}{event.average_cpu == null ? '' : ` · CPU ${event.average_cpu.toFixed(1)}%`}</span><span>{event.previous_size} → {event.new_size}</span></div>) : <Empty title="No scaling activity" detail="Capacity evaluations and scaling actions will appear here." />}</div>}</Card>)}</div>
  </>;
}
