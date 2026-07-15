# LocalEC2

LocalEC2 is a private browser-based cloud console for creating and managing Ubuntu virtual machines on an Ubuntu host with Multipass. It uses a React/TypeScript frontend, a host-run FastAPI backend, and MongoDB Atlas for persistent data.

## Ubuntu prerequisites

- Ubuntu with hardware virtualization enabled
- A user with `sudo` access
- A MongoDB Atlas cluster and database user

The installer supplies Python 3.11+, Node.js 20, pnpm 10.15.1, Git, Multipass, and all project packages. Docker is not required.

## Setup

```bash
cd /path/to/EC2_V3
chmod +x setup-ubuntu.sh start-ubuntu.sh
./setup-ubuntu.sh
```

To refresh only project dependencies and configuration after system prerequisites are installed:

```bash
./setup-ubuntu.sh --project-only
```

Edit `backend/.env` and set the Atlas connection:

```env
MONGODB_URI=mongodb+srv://username:URL_ENCODED_PASSWORD@cluster.example.mongodb.net/?retryWrites=true&w=majority
MONGODB_DATABASE=stratus
```

Atlas Network Access must allow the host's public IP. Percent-encode reserved URI characters in credentials (`@` becomes `%40`). Then start both applications:

```bash
./start-ubuntu.sh
```

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`
- API documentation: `http://localhost:8000/docs`

The development launcher listens on `0.0.0.0:8000` so dedicated nodes on the LAN/VPN can reach
the control plane. Restrict TCP 8000 to the node network with the host/cloud firewall; do not expose
plain HTTP on port 8000 to the public Internet. Set `LOCAL_EC2_BIND_HOST=127.0.0.1` before launching
to restore local-only access, or put the API behind authenticated VPN connectivity or an HTTPS
reverse proxy for remote nodes.

The customer console deliberately contains no physical-node inventory or maintenance controls.
Start the separately deployed platform-administration console when required:

```bash
./start-admin.sh
```

- Platform administration: `http://localhost:5174`

The administration application is a dedicated operations console with:

- platform health and aggregate capacity;
- physical-node enrollment, approval, draining, and removal;
- cross-customer workload inventory;
- control-plane operation and failure history;
- team creation and role-based access management.

Set `PLATFORM_ADMIN_EMAIL` in `backend/.env` to the infrastructure owner's login email. Node APIs
return HTTP 403 to ordinary customer accounts even if they attempt to call the endpoints directly.

For development only, the database bootstraps `admin` / `admin` and forces a password change on
first use. Set `DEVELOPMENT_DEFAULT_ADMIN=false` outside local development. Production startup
rejects configurations that leave this development account enabled.

## Manual development

```bash
cd backend
source .venv/bin/activate
python -m uvicorn app.main:app --reload
```

In another terminal:

```bash
cd frontend
pnpm run dev
```

## Checks

```bash
cd backend
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy app
.venv/bin/python -m pytest

cd ../frontend
pnpm run lint
pnpm run typecheck
pnpm run test -- --run
pnpm run build
```

## Troubleshooting

- **MongoDB timeout:** allow your public IP in Atlas and verify the cluster is running.
- **Multipass permission denied:** log out and back in after setup so the `multipass` group applies.
- **Multipass unavailable:** run `sudo snap restart multipass` and `multipass version`.
- **Virtualization unavailable:** enable Intel VT-x/AMD-V in firmware and verify `/dev/kvm` exists.
- **Backend startup error:** inspect `backend/backend.error.log`.

The app uses one FastAPI process because its background workers are process-local. It is intended as a personal/educational local cloud console, not as a public-cloud replacement.

## Distributed cloud-node foundation

The console now has a backward-compatible first milestone for the multi-node architecture. The
existing Multipass provider remains the default, while dedicated Ubuntu hosts can be enrolled from
the separate platform-administration console and report capacity through a token-authenticated
heartbeat agent.

Each enrolled node has an explicit approval state, logical availability zone, compute provider,
schedulability/drain state, last heartbeat, capabilities, CPU/memory/storage inventory, host
interfaces, and libvirt networks. The node credential is stored by the control plane only as a
SHA-256 hash.

Enrollment alone deliberately does not claim or modify a machine. After creating an enrollment,
the administration console generates a host-specific command. Run that command with `sudo` on the
dedicated Ubuntu node. The installer:

- installs QEMU/KVM, libvirt, Vagrant, and `vagrant-libvirt`;
- enables the system libvirt daemon and its default NAT network;
- installs a restricted `localec2` service account and Python agent;
- saves the node credential with restricted permissions; and
- starts an agent that reports real inventory every 15 seconds.

The agent makes outbound HTTP(S) calls to the control plane, so it does not require an inbound
management port. When the control plane is on another PC, replace `localhost` in the generated
command with an address reachable from the node (LAN, private VPN, or public HTTPS).

The agent can be run manually after installing the backend package:

```bash
export LOCAL_EC2_API_URL=https://api.example.com/api/v1
export LOCAL_EC2_NODE_ID=<node-id-from-the-console>
export LOCAL_EC2_NODE_TOKEN=<node-enrollment-token>
localec2-agent --once
```

The generated installer uses `deploy/install-node-ubuntu.sh`,
`deploy/localec2-agent.service`, and `deploy/agent.env.example`. Its service can be checked on the
node with:

```bash
systemctl status localec2-agent
journalctl -u localec2-agent -f
```

Vagrant/libvirt execution can also be enabled manually on a local control-plane node with:

```env
VM_PROVIDER=vagrant-libvirt
VAGRANT_EXECUTABLE=vagrant
VAGRANT_PROVIDER=libvirt
VAGRANT_INSTANCES_ROOT=/var/lib/localec2/instances
```

This milestone provides enrollment, approval, draining, monitoring, and the Vagrant provider. It
does not yet dispatch VM operations to remote agents; remote leased operations and scheduling are
the next control-plane milestone.

## EC2-style resources

- Instance types provide T2, T4, and M family CPU/memory presets plus custom sizing.
- Security groups are reusable ingress policies enforced inside guests with UFW.
- Volumes are host-backed, VM-independent directories that can be detached and mounted on another instance; terminating a VM does not delete their data.
- Key pairs are reusable SSH identities. For safety, each private key can be downloaded only once.
- AMIs capture a stopped instance as a reusable local machine image that can launch cloned VMs.
- Hibernation suspends a running instance with Multipass so its in-memory session can resume later.
- Command load balancers provide an authenticated browser terminal that measures running autoscaling members and dispatches each independent command to the instance with the lowest current CPU load.
- Launch templates can run startup commands in every autoscaled VM, allowing each new target to install and start the same application automatically.
