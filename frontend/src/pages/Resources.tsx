import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';
import { api, message } from '../api/client';
import { Button, Card, Empty, ErrorAlert, PageHeader, Status } from '../components/ui';
import { useGroups, useKeyPairs, useTemplates } from '../hooks/data';

export function TemplatesPage() {
  const { data = [] } = useTemplates();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [keyMode, setKeyMode] = useState('one_time');
  const keys = useKeyPairs();
  const create = useMutation({
    mutationFn: (body: FormData) => api.post('/launch-templates', {
      name: body.get('name'), description: body.get('description'), image: body.get('image'),
      cpu_count: Number(body.get('cpu')), memory_mb: Number(body.get('memory')),
      disk_gb: Number(body.get('disk')),
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
    {open && <Card><form onSubmit={(event) => { event.preventDefault(); create.mutate(new FormData(event.currentTarget)); }}><div className="fields"><label>Name<input name="name" required /></label><label>Description<input name="description" /></label><label>Image<select name="image"><option>24.04</option><option>22.04</option></select></label><label>vCPU<input name="cpu" type="number" min="1" max="8" defaultValue="2" /></label><label>Memory MB<input name="memory" type="number" min="512" max="16384" defaultValue="2048" /></label><label>Disk GB<input name="disk" type="number" min="5" max="100" defaultValue="10" /></label><label>SSH key policy<select name="key_mode" value={keyMode} onChange={(event) => setKeyMode(event.target.value)}><option value="one_time">Generate one-time key per instance</option><option value="reusable">Use reusable key pair</option><option value="none">No SSH key</option></select></label>{keyMode === 'reusable' && <label>Reusable key pair<select name="key_pair" required>{keys.data?.map((key) => <option key={key.id} value={key.id}>{key.name}</option>)}</select></label>}</div><label>Startup commands<textarea name="user_data" rows={7} defaultValue={"sudo apt-get update\nsudo apt-get install -y nginx\necho \"healthy\" | sudo tee /var/www/html/health\nprintf '<h1>Served by %s</h1>\\n' \"$(hostname)\" | sudo tee /var/www/html/index.html\nsudo systemctl enable --now nginx"} /><small>Runs once inside every autoscaled VM after launch. A failure marks the launch operation failed.</small></label><div className="form-actions"><Button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button disabled={create.isPending || (keyMode === 'reusable' && !keys.data?.length)}>Create template</Button></div></form></Card>}
    <div className="resource-grid">{data.length === 0 ? <Card><Empty title="No templates" detail="Create a template to standardize VM launches." /></Card> : data.map((item) => <Card key={item.id}><div className="resource-title"><div><h3>{item.name}</h3><p>{item.description}</p></div><button className="danger-icon" onClick={async () => { if (confirm('Delete this template?')) { await api.delete(`/launch-templates/${item.id}`); void qc.invalidateQueries({ queryKey: ['templates'] }); } }}><Trash2 /></button></div><div className="chips"><span>Ubuntu {item.image}</span><span>{item.cpu_count} vCPU</span><span>{item.memory_mb / 1024} GB</span><span>{item.disk_gb} GB disk</span><span>{item.key_pair_id ? 'Reusable SSH key' : item.create_key_pair ? 'One-time SSH keys' : 'No SSH key'}</span>{item.user_data && <span>Startup configured</span>}</div></Card>)}</div>
  </>;
}

export function AutoscalingPage() {
  const { data = [] } = useGroups();
  const templates = useTemplates();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
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
    <PageHeader title="Autoscaling" description="Keep local VM groups near a target CPU load." action={<Button disabled={!templates.data?.length} onClick={() => setOpen(true)}><Plus />New group</Button>} />
    {error && <ErrorAlert>{error}</ErrorAlert>}
    {open && <Card><form onSubmit={(event) => { event.preventDefault(); create.mutate(new FormData(event.currentTarget)); }}><div className="fields"><label>Name<input name="name" required /></label><label>Launch template<select name="template">{templates.data?.map((template) => <option value={template.id} key={template.id}>{template.name}</option>)}</select></label><label>Minimum<input name="min" type="number" min="0" defaultValue="1" /></label><label>Desired<input name="desired" type="number" min="0" defaultValue="1" /></label><label>Maximum<input name="max" type="number" min="1" max="20" defaultValue="3" /></label><label>Scale out CPU %<input name="out" type="number" defaultValue="70" /></label><label>Scale in CPU %<input name="in" type="number" defaultValue="25" /></label></div><div className="form-actions"><Button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button disabled={create.isPending}>Create group</Button></div></form></Card>}
    <div className="resource-grid">{data.length === 0 ? <Card><Empty title="No autoscaling groups" detail={templates.data?.length ? 'Create a group from a launch template.' : 'Create a launch template first.'} /></Card> : data.map((group) => <Card key={group.id}><div className="resource-title"><div><h3>{group.name}</h3><Status value={group.enabled ? 'RUNNING' : 'STOPPED'} /></div><button className="danger-icon" title="Delete group and terminate managed instances" onClick={() => void remove(group.id)}><Trash2 /></button></div><div className="capacity"><span>Min <strong>{group.minimum_size}</strong></span><span>Desired <strong>{group.desired_size}</strong></span><span>Max <strong>{group.maximum_size}</strong></span></div><p>Scale in below {group.scale_in_threshold}% · scale out above {group.scale_out_threshold}%</p><div className="form-actions"><Button className="secondary" disabled={toggle.isPending} onClick={() => toggle.mutate(group)}>{group.enabled ? 'Pause scaling' : 'Resume scaling'}</Button></div></Card>)}</div>
  </>;
}
