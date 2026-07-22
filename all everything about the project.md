# LocalEC2: Complete Project Context and Handoff

> **Purpose of this document:** Give a new developer or AI assistant enough accurate context to understand, run, debug, extend, and discuss this project without first reverse-engineering the whole repository.
>
> **Snapshot basis:** The repository as it exists on 2026-07-21. The working tree contains substantial uncommitted changes, so the current files—not Git history and not older explanatory documents—are the source of truth.

## 1. Executive summary

LocalEC2 is a browser-managed, distributed private cloud inspired by Amazon EC2. It lets customers launch and manage real Linux virtual machines on approved Ubuntu compute laptops/hosts while platform staff manage the physical fleet, capacity, access, and operations through a separate admin console.

It is **not** AWS, an AWS API implementation, or a hypervisor. It is a cloud control plane built around:

- a React customer console;
- a separate React platform-administration console;
- a FastAPI control plane;
- MongoDB durable state;
- outbound-polling agents on Ubuntu compute nodes;
- Vagrant with the libvirt/KVM provider for actual VMs;
- Tailscale/MagicDNS/Funnel for optional public HTTPS endpoints;
- durable operations and leased node jobs for asynchronous work.

The central design rule is separation of the control plane from compute. The machine running FastAPI and the web apps does not automatically run customer VMs. A machine contributes compute only after it is explicitly enrolled as a node, its agent reports the required capabilities, and a platform administrator approves it.

## 2. Product intent

The project demonstrates and implements important cloud-computing ideas on privately owned hardware:

- tenant-scoped VM lifecycle management;
- logical availability zones over multiple physical nodes;
- resource-aware scheduling and atomic capacity reservation;
- durable asynchronous jobs with leases and retries;
- desired-state versus actual-state reconciliation;
- SSH access, browser terminals, and live monitoring;
- guest firewall policy through security groups;
- cross-node replicated images, launch templates, and persistent volumes;
- autoscaling based on CPU samples;
- target groups and managed HAProxy load-balancer appliances;
- optional public endpoints through Tailscale Funnel;
- physical-node enrollment, approval, draining, diagnostics, and RBAC.

It is best viewed as an educational/small private-cloud platform. It intentionally uses familiar EC2 terminology, but its instance types, networking, storage guarantees, availability, API, and operational model are LocalEC2-specific.

## 3. Current architecture

```text
Customer browser (5173)                 Platform browser (5174)
          |                                      |
          +------------ REST/JWT ----------------+
                         |
                  FastAPI control plane (8000)
                         |
        +----------------+-------------------+
        |                |                   |
     MongoDB       background workers    WebSocket relays
   durable state    operations, ASG,      metrics/terminal
                    LB health, reconcile        |
        |                |                      |
        +---------- durable NodeJob queue ------+
                         |
                 outbound HTTP polling
                         |
           approved Ubuntu node agent(s)
                         |
      Vagrant -> vagrant-libvirt -> libvirt/KVM
                         |
                customer and appliance VMs
                         |
             optional Tailscale/Funnel HTTPS
```

### Control plane

FastAPI authenticates users, validates requests, stores resources and desired state in MongoDB, reserves compute capacity, and creates durable `Operation` records. Background workers translate operations into `NodeJob` records and wait for compute agents to execute them.

### Compute plane

Each node runs `backend/app/agent.py` as a restricted `localec2` system user. The agent opens no inbound administration port. It sends heartbeats, claims leased jobs over HTTP(S), runs Vagrant/libvirt locally, posts results, and maintains an outbound WebSocket used to relay interactive PTYs.

### Data plane

Customer workloads run inside Vagrant-managed VMs. VM guest addresses normally live on each node's libvirt network. Direct SSH therefore needs network routing/VPN access to that network. The browser terminal instead crosses the authenticated reverse WebSocket relay and needs neither direct guest routing nor physical-node credentials.

## 4. Technology stack

| Area | Technology | Role |
|---|---|---|
| Customer UI | React 19, TypeScript, Vite 6 | Customer console |
| Admin UI | React 19, TypeScript, Vite 6 | Platform/fleet console |
| Client state/API | TanStack Query, Axios, Zustand | Caching, mutations, auth session |
| Forms/UI | React Hook Form, Zod, Lucide, Recharts, xterm.js | Validation and interface widgets |
| API | Python 3.11+, FastAPI, Pydantic | REST, WebSockets, validation |
| Database | MongoDB via async PyMongo | Durable state and queues |
| Authentication | Argon2, JWT, HTTP-only refresh cookie | Password/session security |
| Secret encryption | Fernet | Downloadable private keys/one-time values |
| Compute | Vagrant, vagrant-libvirt, libvirt, QEMU/KVM | VM provisioning and lifecycle |
| Guest policy | UFW | Security-group ingress/egress |
| Public network | Tailscale, MagicDNS, Funnel | Optional HTTPS exposure |
| Managed balancing | HAProxy appliance VM | Target routing and health behavior |
| Tests/tools | pytest, Vitest, Ruff, mypy, ESLint, TypeScript | Verification |

## 5. Repository structure

```text
EC2_V3/
├── README.md                     Current user/operator overview (authoritative)
├── all everything about the project.md  This handoff
├── Makefile                      Common install/dev/test/lint commands
├── setup-ubuntu.sh               Ubuntu control-plane setup
├── setup-windows.ps1             Windows control-plane setup
├── start-ubuntu.sh               Starts API + customer UI
├── start-windows.ps1             Windows start equivalent
├── start-admin.sh                Starts platform UI
├── fix-windows-time.ps1          Windows clock/time helper
├── enroll-node.md                Site-specific enrollment notes; contains sensitive data
├── explained.md                  Historical theory document; partly obsolete
├── test.md                       Historical/testing notes; verify against current code
├── deploy/
│   ├── install-node-ubuntu.sh    Node installer served/downloaded by the API
│   ├── localec2-agent.service    Hardened systemd unit
│   └── agent.env.example         Compute-agent environment template
├── backend/
│   ├── pyproject.toml            Python package, dependencies, tool config
│   ├── .env.example              Safe control-plane configuration template
│   ├── README.md                 Short backend startup notes
│   ├── tests/test_core.py        Current backend unit tests
│   └── app/
│       ├── main.py               FastAPI app, lifespan workers, health and WS relays
│       ├── agent.py              Standalone compute-node agent and Vagrant executor
│       ├── schemas.py            REST request/response validation models
│       ├── check_database.py     Startup MongoDB connectivity check
│       ├── core/
│       │   ├── config.py         Environment-backed settings
│       │   └── security.py       Password, JWT, token, encryption helpers
│       ├── api/
│       │   ├── router.py         Combines all `/api/v1` routers
│       │   ├── dependencies.py   Current user/admin/operator authorization
│       │   └── routes/
│       │       ├── auth.py       Register/login/refresh/logout/me/password
│       │       ├── instances.py  VM creation/lifecycle/key/metrics endpoints
│       │       ├── nodes.py      Enrollment, heartbeat, jobs, agent files, volume transfer
│       │       ├── resources.py  Operations, one-time keys, templates, ASGs/events
│       │       ├── ec2_resources.py  Key pairs, security groups, volumes
│       │       ├── amis.py       AMI create/list/delete
│       │       ├── target_groups.py  Target registration and group CRUD
│       │       ├── load_balancers.py Managed LB lifecycle/checks
│       │       └── admin.py      Cross-tenant platform operations and team RBAC
│       ├── infrastructure/
│       │   ├── models.py         MongoDB domain models and enums
│       │   ├── database.py       Mongo client, serialization, migrations, indexes
│       │   ├── vm.py             Safe provider-side names/provider abstractions
│       │   ├── metrics.py        Guest CPU sampling command
│       │   └── terminal.py       Terminal-related infrastructure helper
│       ├── services/
│       │   ├── scheduler.py      Eligibility, placement, atomic reservation/release
│       │   ├── node_jobs.py      Dispatch, waiting, lease recovery, remote exec
│       │   ├── operations.py     Durable lifecycle-operation worker
│       │   ├── autoscaling.py    Desired capacity and CPU policy loop
│       │   ├── load_balancing.py HAProxy config/sync and target health loop
│       │   └── reconciliation.py Actual-state checks and expired lease cleanup
│       └── workers/              Package placeholder/legacy organization
├── frontend/
│   ├── package.json              Customer UI dependencies/scripts
│   ├── .env.example              API and WebSocket URLs
│   ├── tests/status.test.tsx     Current small UI test
│   └── src/
│       ├── main.tsx              React bootstrap/query client
│       ├── App.tsx               Route table and auth protection
│       ├── types.ts              API-facing TypeScript types
│       ├── api/client.ts         Axios client, auth/refresh behavior
│       ├── store/auth.ts         In-memory Zustand auth state
│       ├── hooks/data.ts         Shared resource queries/mutations
│       ├── components/           Shell/layout and common UI controls
│       ├── pages/Auth.tsx        Login and registration
│       ├── pages/Dashboard.tsx   Fleet/customer summary
│       ├── pages/Instances.tsx   List, launch, detail, lifecycle
│       ├── pages/Live.tsx        Metrics and xterm browser terminal
│       ├── pages/Resources.tsx   Templates and autoscaling
│       ├── pages/CloudResources.tsx  AMIs, keys, groups, volumes
│       ├── pages/LoadBalancers.tsx  Target groups and load balancers
│       ├── pages/Operations.tsx  Durable operation history
│       └── styles/app.css        Customer UI styling
└── admin-frontend/
    ├── package.json              Admin UI dependencies/scripts
    └── src/
        ├── App.tsx               Login plus overview/nodes/workloads/storage/access pages
        ├── api.ts                Admin Axios/session client
        ├── types.ts              Admin API types
        └── styles.css            Admin UI styling
```

Generated directories (`dist`, caches, `.venv`, `node_modules`) and runtime logs are not source. Lockfiles pin frontend dependency graphs and should normally remain committed.

## 6. Main features and how they work

### 6.1 Authentication and tenant isolation

- Users register and log in through `/api/v1/auth`.
- Passwords are hashed with Argon2.
- Short-lived access JWTs default to 15 minutes.
- Refresh JWTs default to seven days, are tracked by hashed token/JTI records in MongoDB, and are delivered with credentials/cookies by the web clients.
- Logout revokes the refresh record.
- Most customer database queries include `user_id`; referenced resources are checked for ownership.
- Customer access and platform roles are separate concerns.
- Platform roles are `ADMINISTRATOR`, `OPERATIONS`, `MARKETING`, `ANALYST_FINANCE`, and `DEVELOPER`.
- Admin-only endpoints use explicit administrator authorization; operational endpoints allow administrator or operations roles as appropriate.
- In development, a default `admin@localec2.local` / `admin` account may be created and is forced to change its password.

Important caveat: if `PRIVATE_KEY_ENCRYPTION_KEY` is absent outside production, `fernet()` generates a process-local key. Encrypted values may become unreadable after restart. Production validation requires a persistent key and a strong JWT secret.

### 6.2 Node enrollment and fleet management

1. A platform administrator creates a node with a unique name and availability zone.
2. The API returns a raw enrollment token only in the enrollment/reconnect response; MongoDB stores its SHA-256 hash.
3. The generated installer command installs KVM/libvirt, Vagrant, `vagrant-libvirt`, a restricted `localec2` account, the Python agent, environment file, and systemd service.
4. The agent heartbeats with hostname, agent version, CPU/RAM/storage, interfaces, virtual networks, and capabilities.
5. Required schedulable capabilities are `jobs`, `vagrant`, `libvirt`, and `kvm`.
6. An administrator chooses host-reserved CPU/RAM/storage and approves the node.
7. `schedulable=false` drains a node: existing workloads remain assigned, but new placement excludes it.
8. A node becomes offline when its last heartbeat is older than `NODE_OFFLINE_SECONDS` (default 45).
9. Token rotation/reconnect invalidates the old agent token and produces new setup data.

The systemd unit runs the agent as `localec2`, restarts on failure, enables `NoNewPrivileges`, protects the filesystem/home, and grants write access only to `/var/lib/localec2`.

### 6.3 Scheduling and capacity reservation

A node is eligible only when it is approved, schedulable, recently online, uses `vagrant-libvirt`, reports all required capabilities, matches the requested availability zone if one was requested, and has enough capacity after both host reservations and existing allocations.

Scheduling atomically increments `cpu_allocated`, `memory_allocated_mb`, and `storage_allocated_gb`. The instance records its `node_id`, zone, provider, and `capacity_reserved=true`. A `placement_pending` guard prevents two concurrent reservations for the same instance. Failed placement rolls counters back. Termination or failed launch releases capacity idempotently.

At API startup, allocation counters are rebuilt from active instance reservations. This repairs counters after interrupted writes.

### 6.4 Instance launch and lifecycle

Supported base images in the product are versioned Bento Vagrant boxes for Ubuntu 24.04, Ubuntu 22.04, and Debian 12. The guest SSH user is `vagrant`.

Launch flow:

1. Customer submits name, description, image/AMI, sizing/instance type, availability zone, key policy, security groups, and optional startup data.
2. API validates limits/references and the per-user active-instance limit.
3. API inserts an `Instance`, atomically reserves an eligible node, inserts a `LAUNCH` operation, and returns HTTP 202.
4. The operation worker claims the operation atomically.
5. It prepares SSH material, user data, UFW policy, image/AMI information, and optional Tailscale provisioning.
6. It creates a `LAUNCH` node job for the assigned node and waits for the agent result.
7. The agent writes an isolated Vagrant workspace/Vagrantfile, runs `vagrant up --provider=libvirt`, inspects the guest, and reports its state/address.
8. The operation and instance become successful/running, or the instance becomes `ERROR` with a recorded failure and released capacity.

Lifecycle mapping:

| User action | Operation | Node job / Vagrant behavior | Result |
|---|---|---|---|
| Launch/retry | `LAUNCH` | recreate workspace, `vagrant up` | `RUNNING` or `ERROR` |
| Start | `START` | `vagrant up`, reapply firewall | `RUNNING` |
| Stop | `STOP` | `vagrant halt` | `STOPPED` |
| Hibernate | `HIBERNATE` | `vagrant suspend` | `HIBERNATED` |
| Terminate | `TERMINATE` | Tailscale cleanup, `vagrant destroy -f`, workspace removal | soft-deleted, capacity released |

Autoscaling uses `SCALE_OUT` and `SCALE_IN`, which share launch/terminate mechanics. `PUBLISH_ENDPOINT` handles endpoint setup work. The API rejects conflicting active operations for one instance.

### 6.5 Durable operations and node jobs

There are two asynchronous layers:

- `Operation`: customer/platform-visible intent, progress, status, result/error, and timestamps.
- `NodeJob`: a concrete command assigned to one compute node.

Operations transition `PENDING -> RUNNING -> SUCCEEDED/FAILED`. The worker uses atomic claiming and configured concurrency. Node jobs transition `QUEUED -> LEASED -> SUCCEEDED/FAILED`. The agent renews long job leases while work continues. Expired leases are requeued within the retry policy or failed, preventing a dead agent from owning work forever. Job payload secrets such as the Tailscale auth key are redacted after completion.

Supported agent jobs include `LAUNCH`, `START`, `STOP`, `SUSPEND`, `DELETE`, `EXEC`, `INSPECT`, `TAILSCALE_SETUP`, `VOLUME_ATTACH`, `VOLUME_DETACH`, and `VOLUME_DELETE`.

### 6.6 Monitoring and reconciliation

- The metrics WebSocket runs commands inside a running guest every two seconds and returns CPU, memory, root-disk, and network counters.
- A snapshot REST endpoint provides point-in-time metrics.
- The reconciliation worker periodically inspects non-transitional distributed instances.
- Provider states map to LocalEC2 states (`running`, `shutoff/poweroff`, `suspended`, etc.).
- If an assigned node goes offline while an instance was believed running, the instance becomes `UNKNOWN` with an explanation.
- Legacy database instances without distributed placement become `UNKNOWN` and cannot use distributed lifecycle actions.
- Reconciliation also wipes expired one-time secrets and reaps expired node-job leases.

### 6.7 Browser terminal

The customer opens `/ws/instances/{id}/terminal` with the access token encoded in a WebSocket subprotocol. The API verifies ownership and that the instance is running. Each node agent maintains an authenticated outbound `/ws/agent/nodes/{id}/terminal` connection. The control plane pairs a customer session with the assigned node; input, resize events, output, exit, and error messages are relayed. The node uses a PTY around the VM's Vagrant SSH session.

No node SSH password or inbound agent port is exposed to the browser.

### 6.8 SSH keys

- Customers can create reusable key pairs.
- The public key/fingerprint are stored normally; the generated private key is Fernet-encrypted.
- A private key download is deliberately one-time/limited and referenced through a temporary opaque token.
- A launch may use an existing key, generate one, or omit managed key creation depending on request/template policy.
- An instance can request a short-lived SSH-key delivery route.
- Never log or place private keys in this handoff.

### 6.9 Security groups

Security groups contain validated TCP/UDP ingress and egress rules, port ranges, CIDR sources, and descriptions. They are translated into UFW configuration inside the guest during launch and reapplied on start. They are application-managed guest firewalls, not libvirt network ACLs or AWS security groups. Deletion is blocked when active resources reference a group.

### 6.10 AMIs

A LocalEC2 AMI is a packaged Vagrant/libvirt image plus MongoDB metadata, not an AWS AMI. A stopped source VM is packaged on its assigned node and uploaded to canonical control-plane AMI storage. Any destination node can download and cache that package as a node-local Vagrant box before launch. Deletion checks active references, removes the canonical package, and requests cache cleanup from connected nodes.

### 6.11 Persistent block volumes

- A volume is a single-attach qcow2 block image.
- Canonical detached storage is under the control-plane `VOLUME_STORAGE_ROOT` (default `./data/volumes`).
- Attach downloads or creates the qcow2 on the target compute node, attaches it to the libvirt domain as a virtio disk, formats ext4 when blank, mounts it (default `/mnt/data`), and writes an `/etc/fstab` entry.
- Detach unmounts it, removes the fstab entry, detaches it through `virsh`, uploads the full qcow2 to canonical control-plane storage, then deletes the node-local copy.
- It can subsequently attach on a different node, enabling manual mobility across physical hosts.
- Instance deletion does not delete an independent volume.
- It is **not synchronously replicated while attached**. Automatic failover after a node loss is not safe because the newest disk exists only on that node. Production-grade recovery would require a replicated backend (for example Ceph RBD), fencing, backups, and tested recovery procedures.

The agent authenticates volume GET/PUT operations with its node token, and the server checks node/volume placement authorization.

### 6.12 Launch templates and autoscaling

Launch templates capture image, CPU/RAM/disk, instance type, security groups, startup data, SSH key policy, and one or more availability zones.

An autoscaling group references a template and defines minimum, desired, maximum, CPU target/thresholds, evaluation periods, cooldown, and enabled state. The loop:

- first reconciles actual managed count toward desired count;
- samples CPU only from running distributed instances;
- scales out after sustained readings at/above the configured threshold;
- scales in after sustained readings at/below the configured threshold;
- respects min/max and cooldown;
- records every attempt in `scaling_events`;
- balances new instances across allowed template zones by choosing the least-used zone.

Instances in `ERROR` or `TERMINATING` do not satisfy desired capacity. A pending/running operation pauses another group decision to prevent overlap.

### 6.13 Target groups and load balancers

Target groups are AWS-style collections of HTTP instance targets. Registration can be:

- manual, using explicit instance IDs; or
- autoscaling, dynamically following an autoscaling group.

They define port and health path. A load balancer references a target group and can choose `round_robin`, `least_connections`, `least_cpu`, `random`, or `source_ip_hash`, with its own listener/health settings.

Creating a load balancer reserves and launches a small managed Ubuntu 22.04 appliance VM hidden from the customer instance list. The system generates HAProxy configuration using healthy targets' Tailscale addresses, continually synchronizes it, and may publish the listener with Tailscale Funnel. Deleting the load balancer also terminates its managed appliance. Current tests cover target registration, round-robin selection, least-connections behavior, and HAProxy configuration.

### 6.14 Tailscale public endpoints

When `TAILSCALE_REUSABLE_AUTH_KEY` is configured, instance launch can dispatch a secret-bearing setup job that installs Tailscale in the VM, joins with a unique hostname, allows the target port on `tailscale0`, optionally installs a simple nginx page if nothing listens, enables Funnel, and records Tailscale IPv4, MagicDNS name, and public HTTPS URL.

Before using it, the tailnet must have MagicDNS, HTTPS, and Funnel enabled. `TAILSCALE_INSTANCE_TAG` is optional and only valid after matching `tagOwner`/permissions and an auth key allowed to apply the tag. Funnel exposes a port; it does not by itself deploy the customer's application. Security-group rules must allow the service port.

## 7. Domain model and MongoDB collections

| Model/collection | Meaning and important relationships |
|---|---|
| `users` | Customer identity plus platform flags/roles |
| `refresh_tokens` | Hashed/revocable refresh sessions with TTL |
| `instances` | Tenant VM desired/observed state, placement, endpoint, ownership |
| `cloud_nodes` | Physical node inventory, heartbeat, capacity, approval/drain state |
| `operations` | Durable user-visible lifecycle intent/history |
| `node_jobs` | Leased physical-node command queue |
| `one_time_secrets` | Encrypted, expiring, consumable downloads |
| `launch_templates` | Reusable launch configuration |
| `autoscaling_groups` | Desired size and CPU scaling policy |
| `scaling_events` | ASG audit/history |
| `target_groups` | Manual or ASG-derived HTTP targets |
| `load_balancers` | Managed HAProxy appliance metadata |
| `key_pairs` | Public key, encrypted private key, fingerprint |
| `security_groups` | Guest firewall rules |
| `volumes` | Canonical/local node placement and attachment metadata |
| `amis` | Reusable backing image and source/node metadata |

MongoDB UUIDs/enums are serialized as strings; model `id` maps to Mongo `_id`. Initialization performs compatibility migrations, creates unique/lookup indexes, converts legacy load balancers to target groups, gives old nodes reservation defaults, and rebuilds capacity counters.

The database stores desired state and relationships. The node/provider holds actual VM state. Do not treat one as a complete substitute for the other.

## 8. API map

All REST endpoints below are under `/api/v1` unless noted.

### Public/system

- `GET /health` and `GET /health/readiness` (outside `/api/v1`)
- `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/change-password`
- `GET /auth/me`

### Customer resources

- Instances: list/create/get, start/retry/stop/hibernate/delete, SSH key, zone capacity, metric snapshot
- Operations: list/get
- One-time secrets: token download
- Launch templates: CRUD
- Autoscaling groups: CRUD, desired capacity, event list
- Target groups: CRUD and target registration
- Load balancers: list/create/update/delete, targets, immediate check
- Key pairs: list/create/download/delete
- Security groups: list/create/replace/delete
- Volumes: list/create/attach/detach/delete
- AMIs: list/create/delete

### Node agent

- Nodes: list/enroll/update/delete/rotate token (platform protected)
- Agent heartbeat, job claim, lease renewal, completion
- Agent installer, current `agent.py`, service unit
- Authenticated volume image download/upload

### Platform admin

- Overview and cross-tenant operation history
- Cross-tenant instance/workload list, lifecycle intervention, diagnostics
- Cross-tenant volume list, force detach/delete
- Node contribution/capacity reporting
- Team list/create/update and role management

### WebSockets

- `/ws/instances/{instance_id}/metrics`
- `/ws/instances/{instance_id}/terminal`
- `/ws/agent/nodes/{node_id}/terminal`

Use generated OpenAPI at `http://localhost:8000/docs` for exact payload schemas and status codes.

## 9. Frontend behavior

### Customer console (`frontend`, port 5173)

Routes:

- `/` dashboard
- `/login`, `/register`
- `/instances`, `/instances/new`, `/instances/:id`
- `/instances/:id/connect`, `/instances/:id/monitor`
- `/launch-templates`
- `/autoscaling`, `/autoscaling/new`, `/autoscaling/:id`
- `/load-balancers`
- `/operations`
- `/volumes`, `/security-groups`, `/key-pairs`, `/amis`

TanStack Query supplies polling/caching and mutation invalidation. Axios targets `VITE_API_URL`; WebSockets use `VITE_WS_URL`. Zustand holds the current access token/user in memory. The Axios layer attaches authentication and participates in refresh/session recovery. xterm.js renders terminal sessions, and Recharts supports monitoring displays.

### Platform console (`admin-frontend`, port 5174)

This is deliberately separate from the customer UI. Its single-page shell exposes overview, infrastructure/nodes, workloads/diagnostics, storage, operation audit, and access/team administration depending on role. It polls platform data at roughly 8–15 second intervals.

## 10. Configuration

Copy templates; never copy real `.env` values into chats or documentation.

Important backend variables:

| Variable | Purpose/default |
|---|---|
| `APP_ENV` | `development`; activates production safety validation when `production` |
| `MONGODB_URI`, `MONGODB_DATABASE` | Mongo connection/database (`stratus`) |
| `JWT_SECRET`, `JWT_ALGORITHM` | JWT signing |
| `ACCESS_TOKEN_MINUTES`, `REFRESH_TOKEN_DAYS` | Session lifetimes |
| `PRIVATE_KEY_ENCRYPTION_KEY` | Stable Fernet key for encrypted secrets |
| `FRONTEND_ORIGIN`, `ADMIN_FRONTEND_ORIGIN` | Exact credentialed CORS origins |
| `PLATFORM_ADMIN_EMAIL` | Optional explicit admin identity |
| `DEVELOPMENT_DEFAULT_ADMIN` | Enables the development bootstrap admin |
| `MAX_INSTANCE_*`, `MAX_TOTAL_INSTANCES` | Customer/request limits |
| `OPERATION_*` | Operation polling/concurrency |
| `STATE_RECONCILE_SECONDS` | Provider reconciliation interval |
| `AUTOSCALING_INTERVAL_SECONDS` | ASG evaluation interval |
| `LOAD_BALANCER_*` | LB health/request timing |
| `TAILSCALE_REUSABLE_AUTH_KEY` | Optional secret for guest enrollment |
| `TAILSCALE_INSTANCE_TAG` | Optional permitted device tag |
| `TAILSCALE_FUNNEL_TARGET_PORT` | Default public service port, 80 |
| `ONE_TIME_SECRET_MINUTES` | Secret download lifetime |
| `NODE_OFFLINE_SECONDS` | Heartbeat freshness window, 45 |
| `NODE_JOB_*` | Poll, lease, and timeout behavior |
| `VOLUME_STORAGE_ROOT` | Canonical detached qcow2 storage |
| `AMI_STORAGE_ROOT` | Canonical cross-node Vagrant package storage |

Frontend variables are `VITE_API_URL` and `VITE_WS_URL`. The agent uses `LOCAL_EC2_API_URL`, `LOCAL_EC2_NODE_ID`, `LOCAL_EC2_NODE_TOKEN`, `LOCAL_EC2_STORAGE_ROOT`, launch timeout, Vagrant executable/home, and libvirt URI; optional OVMF path overrides support nonstandard distributions.

## 11. Setup and operation

### Control plane on Ubuntu

Requirements: Python 3.11+, Node.js 20, pnpm 10.15.1, and reachable MongoDB.

```bash
chmod +x setup-ubuntu.sh start-ubuntu.sh start-admin.sh
./setup-ubuntu.sh
# Edit backend/.env, especially MONGODB_URI and production secrets.
./start-ubuntu.sh
```

In another terminal:

```bash
./start-admin.sh
```

URLs:

- customer: `http://localhost:5173`
- admin: `http://localhost:5174`
- API docs: `http://localhost:8000/docs`
- health: `http://localhost:8000/health`

`start-ubuntu.sh` checks MongoDB, runs reload-enabled Uvicorn on `0.0.0.0:8000`, and starts the customer Vite server. Restrict port 8000 to LAN/VPN networks or place it behind HTTPS; never expose plain HTTP publicly.

### Compute nodes

Use Admin > Infrastructure > Enroll node and use the command generated for that specific node. The installer expects Ubuntu x86_64 with hardware virtualization enabled. Confirm:

```bash
systemctl status localec2-agent
journalctl -u localec2-agent -f
virsh --connect qemu:///system list --all
```

The first launch may be slow while Vagrant downloads the Bento box. Pre-caching the configured boxes on every node reduces this delay.

## 12. Development and verification

```bash
make backend-install
make frontend-install
make admin-install
make lint
make test
cd frontend && pnpm run build
cd ../admin-frontend && pnpm run build
```

`make test` runs backend pytest and frontend Vitest. `make lint` runs Ruff, strict mypy, both ESLint configurations, and both TypeScript checks.

Current test coverage is useful but small. Backend tests cover password/JWT helpers, safe names, request schemas, session separation, agent Vagrant rendering/retry behavior, node status, provider-state mapping, ASG error counting, explicit admin roles, target selection, and HAProxy config. The frontend test only checks status rendering. High-risk flows still need integration tests with MongoDB and real/fake node agents.

## 13. Security model and important warnings

- Use HTTPS or a private VPN for control-plane and agent traffic.
- Rotate any node token exposed in a document, terminal transcript, or chat. `enroll-node.md` currently appears to contain a concrete node ID and raw enrollment token; treat it as compromised, rotate it from the admin console, and replace the guide with placeholders.
- Do not share `backend/.env`, `backend/env`, logs, private keys, Tailscale keys, JWT secrets, MongoDB credentials, or node tokens.
- Credentialed CORS accepts only the configured customer/admin origins.
- Node tokens and one-time tokens are hashed in MongoDB; encrypted private material uses Fernet.
- Access JWTs in WebSocket subprotocols are validated before metrics/terminal access.
- The node agent is outbound-only, but it still controls libvirt and therefore is security-sensitive. Keep its host dedicated, patched, and network-restricted.
- Security groups protect guest traffic through UFW; they do not protect the API or physical node.
- `TAILSCALE_REUSABLE_AUTH_KEY` is sent only in a dedicated job and should be redacted from stored job payloads after completion.
- Production startup rejects the development admin, weak/default JWT secrets, and missing Fernet key.

## 14. Known limitations and design boundaries

- This is not AWS-compatible and does not implement EC2 APIs, IAM, billing, Nitro, VPCs, EBS semantics, or AWS availability guarantees.
- Compute currently requires Ubuntu, x86_64 hardware virtualization, KVM/libvirt, Vagrant, and `vagrant-libvirt`.
- Guest IPs belong to node-local libvirt networks; direct cross-network access needs routing/VPN.
- The control plane and its MongoDB/storage remain important centralized dependencies.
- Background workers are process-local. Running multiple API worker processes can duplicate worker behavior without additional distributed locking/queue design; use one API process unless this architecture is changed.
- Volume mobility copies entire qcow2 files on detach and is not live replication.
- Node failure does not automatically recover the newest attached volume or restart workloads elsewhere.
- AMI capture transfers a complete packaged box through the control plane and may require substantial time and disk/network capacity.
- Tailscale Funnel depends on external tailnet configuration and service availability.
- Load balancers are VMs and consume fleet capacity.
- Reconciliation observes and updates state but is not full self-healing orchestration.
- The current automated test suite does not provide end-to-end coverage of enrollment, scheduling concurrency, VM lifecycle, volume transfer, WebSocket relays, or Tailscale.

## 15. Historical-document warning

`explained.md` describes an earlier architecture in which FastAPI directly invoked Multipass on the control-plane computer. That is no longer the current architecture. Some general cloud theory in it is still useful, but claims about Multipass, local execution, the `ubuntu` guest user, direct host workers, and old volume/AMI behavior must be checked against current code.

When documents conflict, use this priority:

1. current executable code and schemas;
2. current root `README.md`;
3. this handoff snapshot;
4. `backend/README.md` and deployment templates;
5. `explained.md` / `test.md` only as historical context.

## 16. Practical debugging guide

### API will not start

- Run `backend/.venv/bin/python -m app.check_database` from `backend`.
- Verify `MONGODB_URI`, DNS/network access, and database credentials.
- Read `backend/backend.error.log`, but redact secrets before sharing.
- Confirm production safety variables meet validation requirements.

### No zone appears on the launch page

- Node must be approved, schedulable, and heartbeat within 45 seconds.
- It must report `jobs`, `vagrant`, `libvirt`, and `kvm`.
- Check reserved plus allocated CPU/RAM/storage against totals.
- Check agent service logs and API reachability from the node.

### Launch stalls/fails

- Inspect the user-visible operation and underlying node job.
- Check node agent journal and Vagrant/libvirt state.
- First box download may take a long time.
- Confirm `/dev/kvm`, libvirt service/network, Vagrant plugin, storage space, and OVMF paths.
- A lease expiry can indicate agent death, network loss, or an operation longer than configured lease renewal/timeout.

### Instance is `UNKNOWN`

- Assigned node may be offline/unapproved.
- The instance may be a legacy local/MultiPass row with no distributed placement.
- Compare MongoDB assignment with `virsh`/Vagrant state on the assigned node.

### Terminal fails

- Instance must be `RUNNING` with a node assignment.
- Node's reverse terminal WebSocket must be connected.
- Check frontend `VITE_WS_URL`, proxy/TLS WebSocket upgrade settings, token expiry, and agent logs.

### Volume failure

- Validate volume/instance ownership and single-attach state.
- Confirm target node can download canonical data and has `qemu-img`/`virsh` access.
- On detach, ensure the guest unmounted and the control-plane upload completed before assuming the canonical copy is current.

### Public endpoint or load balancer fails

- Verify reusable Tailscale key, MagicDNS, HTTPS, Funnel permission, optional tag policy, and guest egress.
- Confirm a service listens on the target port and UFW/security groups allow it.
- For load balancers, inspect target health, Tailscale target addresses, generated HAProxy config, and appliance operation/job state.

## 17. Guidance for the next developer or AI assistant

Before changing code:

1. Read `README.md`, this document, `backend/app/infrastructure/models.py`, and the specific route/service involved.
2. Run `git status --short`. The repository is currently dirty; never overwrite unrelated user changes.
3. Treat route schemas and ownership checks as part of the security boundary.
4. Preserve the two-layer operation/node-job model; do not run hypervisor commands in API request handlers.
5. Preserve atomic capacity reservation/release and idempotency on retries.
6. Consider node offline, lease expiry, API restart, agent restart, and partial job completion for every distributed change.
7. Keep customer and admin permissions/UI separate.
8. Never include raw secrets in fixtures, logs, documentation, commits, or chat output.
9. Update current documentation when behavior changes and either update or clearly mark historical docs.
10. Run focused tests first, then `make lint`, `make test`, and both production frontend builds for a full handoff.

## 18. Short glossary

- **Control plane:** API, database, scheduling, and orchestration logic.
- **Compute node:** Approved Ubuntu host donating KVM/libvirt capacity.
- **Agent:** Outbound service on a node that executes leased jobs.
- **Operation:** Durable customer/platform intent and visible progress.
- **Node job:** Concrete command executed on exactly one assigned node under a lease.
- **Desired state:** What MongoDB says should exist or happen.
- **Actual state:** What Vagrant/libvirt/guest inspection reports.
- **Availability zone:** Logical placement label assigned to compute nodes.
- **Capacity reservation:** Atomic accounting of VM CPU/RAM/root storage against a node.
- **Reconciliation:** Periodic comparison of desired and actual state.
- **AMI:** Local reusable VM backing clone/metadata, not an AWS AMI.
- **Security group:** UFW policy rendered inside a guest.
- **Target group:** Manual or ASG-derived set of HTTP instance targets.
- **Funnel:** Tailscale feature publishing a tailnet service through public HTTPS.

## 19. One-paragraph context prompt

If context space is extremely limited, give the next assistant this:

> LocalEC2 is a distributed private EC2-style cloud: React customer UI on 5173, separate React admin UI on 5174, FastAPI/MongoDB control plane on 8000, and outbound Python agents on approved Ubuntu compute nodes. The API never launches customer VMs locally; it atomically reserves node capacity, creates durable Operations, and dispatches leased NodeJobs. Agents run Vagrant with vagrant-libvirt/KVM, return state, and relay browser PTYs over reverse WebSockets. Features include instance lifecycle, logical zones, monitoring, UFW security groups, one-time SSH keys, AMI clones, movable single-attach qcow2 volumes, launch templates, CPU autoscaling, target groups, managed HAProxy appliance load balancers, platform RBAC, and optional Tailscale/MagicDNS/Funnel endpoints. Read current code/README over the obsolete Multipass-based `explained.md`; preserve tenant authorization, atomic reservations, job idempotency/leases, and existing uncommitted changes; never expose `.env`, node tokens, Mongo credentials, private keys, or Tailscale auth keys.
