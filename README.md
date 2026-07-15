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
systemd agent. It opens no inbound management port; the node polls the API over HTTP(S).

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
so direct SSH requires routing/VPN access to that guest network. The browser interactive PTY is
disabled until a reverse terminal channel is implemented.

## Current feature boundary

Real distributed compute, approval, scheduling, lifecycle, monitoring, security-group guest rules,
autoscaling, load-balancer target health/configuration, team RBAC, and one-time SSH keys are
implemented. The public load-balancer listener/gateway is still a separate networking milestone.

AMI mutation and EBS-style volumes are intentionally disabled. Correct multi-node persistence needs
a replicated storage layer (for example Ceph RBD/CephFS), image replication, fencing, backups, and
recovery testing; old host-directory records are shown read-only rather than presented as durable
cloud storage.

## Verification

```bash
make lint
make test
cd frontend && pnpm run build
cd ../admin-frontend && pnpm run build
```
