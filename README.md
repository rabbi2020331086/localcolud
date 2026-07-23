# LocalEC2 distributed private cloud

LocalEC2 separates a browser/API control plane from dedicated Ubuntu compute nodes. Customers see
an EC2-style console and create instances in logical availability zones. Only platform staff see
physical hosts, enrollment credentials, capacity reservations, networking inventory, draining, and
RBAC controls.

The control-plane PC does **not** run customer VMs implicitly. To donate that PC's capacity, enroll
it as a node and install/approve it by the same process as every other compute host.

## Architecture

- `frontend/`: customer console on port 5173.
- `admin-frontend/`: separate platform console on port 5174.
- `backend/`: FastAPI control plane on port 8000 with MongoDB durable state.
- `backend/app/agent.py`: outbound-only agent installed on compute nodes.
- Vagrant + libvirt/KVM: the only schedulable compute provider.

Instance placement is capacity checked and atomically reserved. Lifecycle operations become durable
node jobs; an approved agent claims a leased job, executes Vagrant on its own host, and reports the
result. Offline, unapproved, drained, obsolete, or undersized nodes are excluded from scheduling.

## Start the control plane

Requirements: Python 3.11+, Node.js 20, pnpm 10.15.1, and MongoDB. The control plane itself does not
need Vagrant, KVM, or Multipass.

```bash
git clone https://github.com/rabbi2020331086/localcolud.git
cd localcolud
chmod +x setup-ubuntu.sh start-ubuntu.sh start-admin.sh
./setup-ubuntu.sh
```

Set `MONGODB_URI` in `backend/.env`, then run:

```bash
./start-ubuntu.sh
```

In a second terminal:

```bash
./start-admin.sh
```

- Customer console: `http://localhost:5173`
- Platform console: `http://localhost:5174`
- API/OpenAPI: `http://localhost:8000/docs`

Development admin login is `admin` / `admin`; the first login forces a password change. Disable
`DEVELOPMENT_DEFAULT_ADMIN` and use strong secrets before any public deployment.

## Add each compute laptop

1. Open Platform console > Infrastructure > Enroll node.
2. Give it a unique name and availability zone.
3. In the setup panel, replace `localhost` with the control plane's LAN, VPN, or public HTTPS API
   address, including `/api/v1`.
4. Copy the generated command and run it with `sudo` on that dedicated Ubuntu compute laptop.
5. Wait for CPU, RAM, disk, interfaces, `jobs`, `vagrant`, `libvirt`, and `kvm` to appear.
6. Set the capacity reserved for Ubuntu/agent overhead, then click Approve.

The installer adds KVM, libvirt, Vagrant, `vagrant-libvirt`, a restricted `localec2` account, and a
systemd agent. It opens no inbound management port; jobs poll over HTTP(S) and interactive terminals
use an outbound authenticated reverse WebSocket.

```bash
systemctl status localec2-agent
journalctl -u localec2-agent -f
```

Re-run **Setup / reconnect** after an agent upgrade. This rotates the node token and installs the
current agent. Never expose plain HTTP port 8000 to the Internet; use a private VPN or an HTTPS
reverse proxy and restrict the API to known node/customer networks.

## Launch flow

Once at least one node is online and approved, the customer Launch page shows available zones. A
launch reserves CPU, RAM, and disk on one eligible node before it is queued. The node downloads the
Ubuntu Vagrant box on first use, creates the VM, installs the selected SSH public key and UFW rules,
and reports its guest address. Subsequent start, stop, hibernate, delete, metrics, load-balancing, and
autoscaling commands are executed by the assigned node agent.

Ubuntu 24.04, Ubuntu 22.04, and Debian 12 are available through versioned Bento Vagrant boxes. The
Vagrant guest user is `vagrant`. Guest addresses currently come from each node's libvirt network,
so direct SSH requires routing/VPN access to that guest network. Browser terminals do not require
guest-network routing or physical-node credentials because their PTY is relayed by the assigned
compute agent after the control plane verifies instance ownership.

### Public endpoints with Tailscale

Set `TAILSCALE_REUSABLE_AUTH_KEY` in `backend/.env` to a reusable, pre-approved auth key. The launch
worker installs Tailscale inside every VM, registers a unique
`instance-<id>` device, enables Funnel for port 80, and records its Tailscale IPv4, MagicDNS name,
and public HTTPS endpoint. The enrollment secret travels in a dedicated node job whose stored
payload is redacted immediately after completion.

Enable MagicDNS, HTTPS, and Funnel in the Tailscale admin console before launching. Tagging is
optional: set `TAILSCALE_INSTANCE_TAG=tag:localec2-instance` only after defining a matching
`tagOwner` and Funnel permission in the tailnet policy and generating a key allowed to apply it.
Funnel publishes the configured port but does not install a website; the image or customer startup
data must run an HTTP service on port 80. Security-group ingress must allow the target port.

Load balancers use AWS-style target groups. A group can register selected running instances or
track an Auto Scaling Group. Creating a load balancer reserves a small managed appliance VM,
using the cached Ubuntu 22.04 image, installs HAProxy, joins it to the tailnet, publishes its
listener through Funnel, and continuously synchronizes its Tailscale targets. Managed appliances
are hidden from the instance list and are terminated with their load balancer.

## Current feature boundary

Real distributed compute, approval, scheduling, lifecycle, reverse browser terminals, monitoring,
security-group guest rules, autoscaling with event history, load-balancer health/routing tests, team
RBAC, one-time SSH keys, and movable persistent block volumes are implemented. Distributed launch
templates carry instance type, sizing, images, security groups, SSH policy, startup data, and a set
of availability zones used for balanced autoscaling placement.

Volumes attach locally for QEMU performance, synchronize their qcow2 backing image to canonical
control-plane storage on detach, and can then attach to an instance on another node. They survive
instance deletion and were verified across two physical nodes. They are single-attach and do not
provide synchronous replication while attached; automatic node-failure recovery still requires a
replicated backend such as Ceph RBD, fencing, backups, and recovery testing. AMI mutation remains
disabled.

### Classroom disaster recovery

Every customer instance includes **Snapshots and recovery protection** under its Actions page. The
instance owner can take manual snapshots at any time, independently enable or disable a per-instance
schedule, choose the interval, request one or two laptop copies, set retention, inspect history, and
delete their own points. A full administrator sees every tenant policy and recovery point in the
separate **Replication** workspace and can override policy, force a snapshot, delete copies, or
recover an instance onto a selected healthy node.
During capture, agent 0.6.5 briefly pauses a running libvirt domain, uses compressed `qemu-img
convert` output with a
shared read-only source lock to make
a standalone qcow2, resumes the guest, uploads a canonical copy, and downloads checksum-verified
copies to other enrolled nodes. MongoDB records the source, replica nodes, size, checksum, status,
and completion time.
Live conversion, verification, upload, and replica-distribution stages are persisted in MongoDB and
shown to customers as a percentage and progress bar while the snapshot is being created.

Recovery is deliberately fenced: the source node must be offline, the destination must have spare
schedulable capacity, and attached persistent volumes must be detached first. Recovery launches a
new Vagrant/libvirt definition and replaces its root disk with the selected point. RAM state and
writes newer than the point are lost. This is classroom disaster recovery, not synchronous storage,
live migration, application-consistent backup, or zero-data-loss high availability. The canonical
`RECOVERY_STORAGE_ROOT` is still a control-plane failure domain and should itself be backed up.

Upgrade every participating node from Admin > Agent/Infrastructure before enabling the policy.

### Agent orphan-resource collection

Agent 0.7.0 fetches a fresh authenticated inventory for its own node every five minutes and
reconciles only LocalEC2-namespaced resources. An item must remain absent from the authoritative
MongoDB inventory for a one-hour grace period before deletion. The collector covers orphaned
`localec2-*` libvirt domains and Vagrant workspaces, node-local recovery qcow2 files, portable-volume
qcow2 files, AMI transfer packages, and deleted AMI Vagrant-box caches. It never targets foreign
libvirt domains or arbitrary host paths, and it performs no cleanup when the control plane cannot
provide a fresh inventory.

Configure node intervals with `LOCAL_EC2_GC_INTERVAL_SECONDS` and
`LOCAL_EC2_GC_GRACE_SECONDS`. The minimum grace period is five minutes; one hour is the safe default.
Each node reports the last collection time, status, category counts, reclaimed bytes, and error to
Admin > Infrastructure > node summary.

Agent 0.5.4 adds safe remote self-updates, explicitly gated administrator-only sudo host commands, and replicated AMI package transfer in addition to
reverse PTY, portable volume jobs, and secure Tailscale/Funnel provisioning. Upgrade an
existing node without rotating its token:

```bash
sudo /opt/localec2-agent/venv/bin/pip install websockets
sudo curl -fsSL http://CONTROL-PLANE:8000/api/v1/agent/agent.py \
  -o /opt/localec2-agent/agent.py
sudo systemctl restart localec2-agent
```

## Verification

```bash
make lint
make test
cd frontend && pnpm run build
cd ../admin-frontend && pnpm run build
```
