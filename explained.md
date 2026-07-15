# LocalEC2 — Theory, Implementation, and Viva Questions

This document explains the theory behind every major LocalEC2 feature, how each feature works internally, how it was implemented in this project, and the questions a teacher may ask during a demonstration or viva.

## 1. Project summary

### What is LocalEC2?

LocalEC2 is a private, browser-based, EC2-style virtual-machine management system. It allows a user to create and manage Ubuntu virtual machines on a local computer. It imitates important ideas from Amazon EC2—instances, images, key pairs, security groups, volumes, launch templates, autoscaling, monitoring, and asynchronous operations—but implements them locally using Multipass.

It is an educational cloud control plane, not a replacement for AWS and not an implementation of the AWS API.

### Main technology stack

| Layer | Technology | Responsibility |
|---|---|---|
| Frontend | React, TypeScript, Vite | Forms, tables, resource pages, terminal, charts |
| Client data layer | Axios, TanStack Query | REST requests, caching, refresh, mutations |
| Backend | FastAPI, Python, Pydantic | Authentication, validation, REST API, WebSockets |
| Database | MongoDB Atlas | Users, desired resource state, operations, relationships |
| VM provider | Multipass | Creates and controls real Ubuntu VMs |
| Hypervisor layer | Normally QEMU/KVM on Ubuntu | CPU, memory, disk, and device virtualization |
| Guest firewall | UFW over Linux netfilter | Applies security-group rules inside VMs |
| Terminal transport | WebSocket + PTY | Interactive browser shell |
| Authentication | Argon2, JWT, refresh cookies | Password and session security |

### High-level architecture

```text
Browser
  │
  ├── HTTPS/REST ──> React client ──> FastAPI routes ──> MongoDB
  │                                      │
  │                                      ├── operation worker
  │                                      ├── autoscaling worker
  │                                      ├── reconciliation worker
  │                                      └── Multipass CLI
  │                                               │
  │                                               └── Ubuntu VMs
  │
  └── WebSocket ──> terminal/metrics endpoints ──> Multipass VM
```

The frontend does not run Multipass commands directly. It asks the backend to create an operation. A worker reads that operation from MongoDB and calls the Multipass provider.

## 2. The control-plane idea

### Theory

A cloud system is commonly divided into:

- **Control plane:** accepts requests, stores desired state, authenticates users, schedules work, and reports status.
- **Data plane:** runs the actual workload and carries its network/storage traffic.

### Implementation in LocalEC2

- FastAPI, MongoDB, and the background workers form the control plane.
- Multipass Ubuntu VMs, their processes, guest network stack, and mounted data form the data plane.
- MongoDB stores what LocalEC2 believes should exist.
- Multipass represents what actually exists on the host.
- The reconciliation worker compares the two to detect drift.

### Likely viva questions

**Q: Is LocalEC2 itself a hypervisor?**  
No. LocalEC2 is a management/control-plane application. Multipass and its underlying virtualization driver provide the VM functionality.

**Q: Why not call Multipass directly from React?**  
A browser cannot safely execute privileged host commands. The backend authenticates the user, validates inputs, records state, and serializes the work before invoking Multipass.

**Q: Why store VM information in MongoDB if Multipass already knows the VMs?**  
Multipass knows infrastructure facts such as name, runtime state, and IP. MongoDB stores application facts such as owner, display name, selected key, security groups, AMI relationship, autoscaling relationship, operation history, and soft-deletion state.

**Q: What is desired state versus actual state?**  
Desired state is the state recorded or requested by LocalEC2. Actual state is reported by Multipass. Reconciliation attempts to keep the database view consistent with the VM provider.

## 3. How Multipass works

### Theory

Multipass is a lightweight Ubuntu VM manager. It downloads Ubuntu images, asks the platform's virtualization backend to create a VM, configures CPU/RAM/disk/networking, starts the guest, and exposes commands such as `launch`, `start`, `stop`, `suspend`, `delete`, `exec`, `shell`, `mount`, and `clone`.

On Ubuntu, hardware-assisted virtualization is normally provided through KVM and QEMU. Multipass hides most hypervisor details behind a consistent CLI. A VM has its own kernel, userspace, virtual CPU, virtual memory, virtual disk, and network interface; it is more isolated than a normal process or container.

### Implementation in LocalEC2

`MultipassProvider` wraps the CLI in asynchronous Python methods:

| LocalEC2 action | Multipass command |
|---|---|
| Create standard VM | `multipass launch <image> --name ... --cpus ... --memory ... --disk ...` |
| Start | `multipass start <name>` |
| Stop | `multipass stop <name>` |
| Hibernate | `multipass suspend <name>` |
| Terminate | `multipass delete <name> --purge` |
| Execute a command | `multipass exec <name> -- <command>` |
| Interactive terminal | `multipass shell <name>` |
| Attach volume abstraction | `multipass mount <host-path> <name>:<guest-path>` |
| Detach volume | `multipass umount <name>:<guest-path>` |
| Create/launch from AMI | `multipass clone <source> --name <new-name>` |

Blocking CLI calls run in `asyncio.to_thread`, so they do not block FastAPI's event loop. Command output and exit codes are captured. A timeout or nonzero exit becomes `ProviderUnavailable`, which is recorded as an operation failure.

The internal Multipass name is generated from a sanitized display name, the beginning of the user ID, and random bytes. This avoids unsafe CLI names and reduces collisions.

### Likely viva questions

**Q: Is Multipass the same as Docker?**  
No. A Multipass VM runs a separate guest kernel. A normal Docker container shares the host kernel. VMs generally provide stronger isolation but have more overhead.

**Q: What is hardware virtualization?**  
CPU features such as Intel VT-x or AMD-V allow a hypervisor to run guest operating systems efficiently while controlling privileged operations and memory mappings.

**Q: What happens on the first launch?**  
Multipass may download and cache the requested Ubuntu image, create the virtual disk, configure compute and networking, apply cloud-init, boot Ubuntu, and then report the assigned address.

**Q: Why use a provider interface?**  
The `VmProvider` protocol separates business logic from Multipass. Tests can use `FakeProvider`, and another VM backend could be implemented later without rewriting the API and operation logic.

**Q: How are command-injection risks reduced?**  
Most Multipass arguments are passed as a list to `subprocess.run`, without a host shell. VM names are sanitized. Guest shell strings are constructed only for controlled firewall/setup operations, and the SSH public key is shell-quoted when inserted into a cloned VM.

**Q: Why is one FastAPI process recommended?**  
The background worker tasks are process-local. Multiple API processes would create multiple workers unless distributed locking or an external queue were introduced.

## 4. Instances

### Theory

An instance is a running or stopped virtual machine created from an operating-system image. Its main properties are identity, image, CPU, memory, root disk, network address, authentication key, firewall policy, owner, and lifecycle state.

Instance types are named resource presets. They standardize CPU and RAM combinations. The names in LocalEC2 resemble AWS names, but they do not provide AWS hardware, credits, pricing, or performance guarantees.

### Implementation

The launch form sends the name, description, image or AMI ID, instance type, CPU, memory, disk, key-pair choice, and security-group IDs to `POST /api/v1/instances`.

The backend:

1. Authenticates the user.
2. Validates resource limits and referenced resources.
3. Creates an `Instance` document with `PROVISIONING` state.
4. Creates a `LAUNCH` operation with `PENDING` state.
5. Returns HTTP `202 Accepted` immediately.
6. The operation worker claims the job atomically.
7. It generates or loads an SSH public key if requested.
8. It launches or clones a Multipass VM.
9. It applies security groups with UFW.
10. It records the IPv4 address, key fingerprint, and `RUNNING` state.
11. It marks the operation `SUCCEEDED`; exceptions produce `ERROR`/`FAILED`.

### Instance fields

| Field | Purpose |
|---|---|
| `id` | LocalEC2 UUID |
| `user_id` | Owner and tenant boundary |
| `multipass_name` | Safe unique provider-side name |
| `display_name` | Human-readable name |
| `description` | Purpose or notes |
| `status` | Lifecycle state |
| `ipv4_address` | Address reported by Multipass |
| `cpu_count` | Virtual CPUs |
| `memory_mb` | Allocated memory |
| `disk_gb` | Root-disk allocation |
| `instance_type` | Selected preset or custom |
| `image` | Ubuntu version |
| `ami_id` | Optional custom-image source |
| `ssh_username` | Defaults to `ubuntu` |
| `key_fingerprint` | Installed key identity |
| `key_pair_id` | Optional reusable key reference |
| `security_group_ids` | Selected firewall-policy references |
| `autoscaling_group_id` | Owning autoscaling group, if managed |
| `last_error` | Most recent provider/operation error |
| `deleted_at` | Soft-deletion timestamp |

### Lifecycle implementation

| Action | Required state | Intermediate state | Final state |
|---|---|---|---|
| Launch | New record | `PROVISIONING`/`STARTING` conceptually | `RUNNING` |
| Start | `STOPPED` or `HIBERNATED` | `STARTING` | `RUNNING` |
| Stop | `RUNNING` | `STOPPING` | `STOPPED` |
| Hibernate | `RUNNING` | `HIBERNATING` | `HIBERNATED` |
| Terminate | Existing active record | `TERMINATING` | soft-deleted |
| Retry launch | `ERROR` or `UNKNOWN` | `PROVISIONING` | `RUNNING` or `ERROR` |

Only one pending or running operation is allowed per instance through normal lifecycle endpoints.

### Likely viva questions

**Q: Why return HTTP 202 instead of 200 after launch?**  
VM creation is slow. `202 Accepted` means the valid request has been queued, not completed. The user follows its operation status asynchronously.

**Q: What is a vCPU?**  
A vCPU is a virtual processor scheduled onto host CPU resources by the hypervisor. It is not necessarily a dedicated physical core.

**Q: Can a VM allocate more resources than the host has?**  
The form enforces project maximums, but the host may still lack enough physical resources. Multipass then fails and the operation records the error.

**Q: Stop versus terminate?**  
Stop powers off the guest but keeps its virtual disk and configuration. Terminate purges the Multipass VM and soft-deletes its LocalEC2 record.

**Q: Stop versus hibernate?**  
Stop performs a normal VM shutdown. Hibernate maps to Multipass `suspend`, which preserves VM execution/memory state for later resume.

**Q: Why soft-delete the database record?**  
Setting `deleted_at` preserves history and relationships for operations while excluding the instance from active lists.

**Q: What makes retry idempotent?**  
Before retrying launch, the worker deletes any partially created Multipass VM with the same internal name. Repeating the operation therefore does not collide with a leftover VM.

**Q: Are the T2/T4/M5 types identical to AWS?**  
No. They are local CPU/RAM presets with familiar names. There are no AWS Nitro features, CPU credits, placement, pricing, or AWS network performance guarantees.

## 5. Images and AMIs

### Theory

A machine image is a reusable source for virtual-machine root disks. In AWS, an AMI contains launch information and references snapshots for root and possibly data volumes. Instances launched from an AMI receive independent volumes based on those snapshots.

### Implementation

LocalEC2 implements a local AMI as a stopped hidden Multipass backing clone:

1. The source instance must be `STOPPED` for filesystem consistency.
2. The backend runs `multipass clone` from the source to a generated AMI backing name.
3. It applies the source CPU, RAM, and disk settings.
4. The clone is started by the shared clone function and immediately stopped for storage as an image.
5. An `Ami` document records the source, backing name, Ubuntu version, resource values, and status.
6. Launching from the AMI clones that backing VM again under a new instance name.
7. If a key was selected, it is inserted into the clone's `~/.ssh/authorized_keys`.

### Likely viva questions

**Q: Why must the source be stopped?**  
Cloning a live filesystem may capture partially written or inconsistent application data. Stopping creates a more stable point-in-time state.

**Q: Is this a real AWS AMI?**  
No. It is a local Multipass clone plus MongoDB metadata that models AMI behavior.

**Q: What does the image preserve?**  
The guest OS, installed packages, configuration, and data on the VM root disk at clone time.

**Q: Does it preserve an attached LocalEC2 volume?**  
No. LocalEC2 volumes are host-directory mounts independent of the root disk. They are not part of the VM clone.

**Q: Why can the new disk not be smaller than the AMI disk?**  
A cloned disk cannot safely be shrunk below the size/layout captured in the source image. LocalEC2 validates the minimum.

**Q: Why block deletion while an active instance references the AMI?**  
It preserves referential integrity and makes resource relationships understandable. Existing clones may technically run independently, but the project intentionally prevents deleting referenced metadata/backing state.

**Q: AMI versus snapshot?**  
Conceptually, a snapshot is a point-in-time copy of storage, while an AMI is boot metadata plus storage references. LocalEC2 does not expose a separate snapshot resource; its AMI clone combines these ideas.

## 6. Key pairs and SSH

### Theory

SSH public-key authentication uses asymmetric cryptography:

1. A user has a private key and corresponding public key.
2. The server stores the public key in `authorized_keys`.
3. During login, the client proves possession of the private key by signing protocol data.
4. The server verifies the signature with the public key.
5. The private key is never sent to the VM.

This is authentication, not encryption by the key pair itself. After authentication, the SSH protocol negotiates symmetric session keys to encrypt traffic efficiently.

### Implementation

- Python `cryptography` generates a 3072-bit RSA key with public exponent 65537.
- The private key is serialized in OpenSSH PEM form.
- The public key is serialized in OpenSSH authorized-key form.
- The fingerprint is SHA-256 over the DER-encoded public-key structure.
- A reusable private key is encrypted at rest with Fernet and may be downloaded once.
- For a standard-image launch, cloud-init adds the public key to the `ubuntu` user.
- For an AMI clone, a controlled guest command appends the key to `~/.ssh/authorized_keys` if absent.

### Reusable key flow

1. Backend creates the key pair.
2. MongoDB stores public key, fingerprint, and Fernet-encrypted private key.
3. The authenticated download endpoint decrypts and streams it.
4. The same database update clears encrypted private material.
5. Later download attempts return HTTP 410.

### One-time launch key flow

1. Worker generates a key during launch.
2. It encrypts the private key in a `OneTimeSecret` document.
3. It returns only a high-entropy opaque download token in the operation result.
4. The database stores a SHA-256 hash of that token, not the raw token.
5. The token expires after the configured period, normally 10 minutes.
6. Download atomically sets `consumed_at`, decrypts the key, clears the ciphertext, and sends `Cache-Control: no-store`.

### Likely viva questions

**Q: Why can the private key be downloaded only once?**  
It reduces the time during which the server retains usable secret material. After download, responsibility moves to the user.

**Q: Can the public key decrypt data encrypted with the private key?**  
That is not the right model for SSH authentication. SSH uses signatures: the private key signs and the public key verifies.

**Q: Why hash the download token?**  
If the database leaks, an attacker cannot directly use the stored token hash as the bearer token.

**Q: Why encrypt the private key if it is one-time?**  
It still exists temporarily in the database. Fernet protects it at rest until consumed or expired.

**Q: What is the fingerprint for?**  
It is a compact identity for comparing keys without displaying the complete public key.

**Q: Does `chmod 600` secure the key cryptographically?**  
No. It is an operating-system permission that prevents other local users from reading the file; it is also required by many SSH clients.

**Q: How does cloud-init help?**  
Cloud-init applies first-boot configuration. LocalEC2 supplies YAML that creates/configures the `ubuntu` user and installs its authorized SSH key.

**Q: What production configuration is essential?**  
Use a stable, secret `PRIVATE_KEY_ENCRYPTION_KEY`. If it changes, stored encrypted keys cannot be decrypted. Production mode requires it.

## 7. Security groups and TCP blocking

### Theory

A security group is a policy describing allowed network traffic. Rules normally match:

- Direction: inbound or outbound
- Network protocol: TCP or UDP
- Port or port range
- Source or destination CIDR

Anything not allowed is denied according to the default policy.

### Which OSI layer does it work at?

LocalEC2 security groups mainly operate at:

- **Layer 3 (Network):** source/destination IPv4 CIDR matching.
- **Layer 4 (Transport):** TCP/UDP protocol and port matching.

They are not an application-layer web firewall. They do not inspect HTTP URLs, SQL, JSON, usernames, or page content at Layer 7. UFW programs Linux netfilter rules inside the guest kernel.

### How TCP is blocked

For inbound traffic, LocalEC2 sets UFW's default incoming policy to deny. It then adds allow rules for selected protocol/port/CIDR combinations.

Suppose TCP port 80 is not allowed:

1. A client sends a TCP SYN packet to the VM's IP and port 80.
2. The packet reaches the guest network stack if upstream networking permits it.
3. Netfilter checks the firewall rules.
4. No allow rule matches port 80.
5. The default deny policy drops or rejects it according to UFW/netfilter behavior.
6. The TCP three-way handshake cannot complete, so the application connection is not established.

If port 80 is allowed only from `192.168.1.0/24`, a SYN from that CIDR can match; a SYN from a different source does not.

### Exact implementation

After a VM launches, the worker executes commands equivalent to:

```bash
sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <source-cidr> to any port <port-or-range> proto <tcp-or-udp>
sudo ufw allow 22/tcp
sudo ufw --force enable
```

If every attached group has explicit egress rules, it changes the default to deny outgoing, adds each allowed outbound rule, and always permits DNS on TCP and UDP port 53.

Security groups are additive. The union of their inbound rules is allowed. For egress, any attached group with no explicit egress rules means allow-all outbound, which overrides restrictions from another group. Egress becomes restricted only when every selected group contains explicit egress rules.

### Important differences from AWS security groups

| AWS security group | LocalEC2 security group |
|---|---|
| Enforced in AWS virtual networking | Enforced inside guest with UFW |
| Stateful by design | Relies on UFW/netfilter connection tracking |
| Changes normally apply dynamically | Current UI applies selected rules during launch |
| Can reference other security groups | Current rules use IPv4 CIDR only |
| Broad protocol support | Current model supports TCP and UDP |
| Usually no forced SSH rule | LocalEC2 always adds TCP 22 for manageability |

### Likely viva questions

**Q: Is a security group a physical firewall?**  
No. It is a logical rule set translated into software firewall rules.

**Q: At which OSI layer does TCP work?**  
TCP is a Layer 4 transport protocol. IP/CIDR matching is Layer 3.

**Q: What is a port?**  
A port is a Layer 4 number used to identify a network service endpoint on a host, such as TCP 22 for SSH or TCP 80 for HTTP.

**Q: What is CIDR?**  
Classless Inter-Domain Routing notation represents an IP network and prefix length. `0.0.0.0/0` means every IPv4 address; `/32` means one IPv4 address.

**Q: Does opening port 80 start Nginx?**  
No. The firewall only permits traffic. An application must also be running and listening on that port.

**Q: If Nginx runs but port 80 is blocked, what happens?**  
Local requests inside the VM can still work, but disallowed external connections cannot complete through the guest firewall.

**Q: Why is SSH port 22 always allowed?**  
The implementation adds it as a management-safety rule so users are not easily locked out. This means the model is not a perfect AWS security-group replica.

**Q: Are rules stateful?**  
UFW/netfilter uses connection tracking, so return traffic for an allowed established connection is normally recognized. LocalEC2 itself does not implement a packet-state engine; the Linux kernel does.

**Q: DROP versus REJECT?**  
DROP silently discards a packet, often causing a timeout. REJECT sends an error/reset. The high-level UFW policy determines generated netfilter behavior; the project expresses `deny`, not a custom low-level target.

**Q: Does this firewall protect the Ubuntu host?**  
No. The generated UFW rules run inside the guest. The host needs its own firewall and secure configuration.

**Q: When are security-group rules applied?**  
During launch after the VM is running. Updating a database rule does not currently push changes to already-running VMs.

**Q: What happens if firewall application fails after VM creation?**  
The operation fails and the instance enters `ERROR`, although the underlying VM may have been created. Retry first deletes the partial VM and recreates it.

## 8. Volumes and the EBS comparison

### EBS theory

Amazon EBS is network-attached block storage for EC2. It presents a virtual block device to an instance. The guest normally partitions/formats it with a filesystem and mounts it. EBS data exists independently of instance compute, and snapshots can preserve point-in-time block data.

### What LocalEC2 implements

LocalEC2 implements an **EBS-like persistence concept**, not actual EBS:

- A volume is a private directory under the configured host `volumes_root`.
- MongoDB stores its logical capacity, owner, host path, attached instance, and guest mount path.
- `multipass mount` exposes the directory inside the guest.
- `multipass umount` detaches it.
- Data physically remains in the host directory after detach or VM termination.

This is a shared/mounted filesystem abstraction, not a raw block device. The guest does not run `mkfs` on it, it does not have EBS IOPS/throughput settings, and logical capacity is used for reporting rather than enforced filesystem quota.

### Implementation flow

**Create:**

1. Validate a unique name and size.
2. Create a UUID-based host directory with mode `0700`.
3. Store a `Volume` document.
4. Calculate used bytes by recursively summing file sizes.

**Attach:**

1. Confirm ownership and that the volume is not attached.
2. Confirm the instance exists.
3. Acquire an in-process attachment lock.
4. Recheck attachment state to reduce races.
5. Run `multipass mount host-path instance:guest-path`.
6. Record the instance ID and mount path.

**Detach:**

1. Run `multipass umount instance:guest-path` if the instance still exists.
2. Clear attachment metadata.

**Terminate an instance:**

The VM is purged and any volume records attached to it are automatically marked detached. Their host data is not deleted.

**Delete a volume:**

Deletion is blocked while attached. When allowed, the host directory is recursively removed and the MongoDB document is deleted.

### Usage calculation

```text
capacity_bytes = size_gb × 1024³
used_bytes = sum of regular file sizes under the directory
available_bytes = max(0, capacity_bytes - used_bytes)
usage_percent = min(100, used_bytes / capacity_bytes × 100)
```

This measurement does not include every filesystem overhead detail and does not prevent writing past the logical size.

### Likely viva questions

**Q: Is your volume really EBS?**  
No. It demonstrates EBS-like independent persistent storage using Multipass host-directory mounts. AWS EBS is managed network block storage.

**Q: File storage versus block storage?**  
File storage exposes files/directories and filesystem operations. Block storage exposes fixed-size blocks as a device; the guest chooses a filesystem. LocalEC2 volumes expose a mounted directory.

**Q: Why does volume data survive termination?**  
The data lives in a host directory outside the Multipass VM disk. Termination deletes the VM but only clears attachment metadata for the volume.

**Q: Can one volume attach to multiple VMs?**  
Not in this project. `attached_instance_id` allows only one current attachment.

**Q: Is the configured size enforced?**  
No. It is a logical capacity for usage display. A production version should use quotas, sparse disk images, LVM, ZFS, or a real block-storage backend.

**Q: What race condition is handled during attach?**  
Two requests might both see an available volume. An `asyncio.Lock` serializes attachment in one process and the state is rechecked inside the lock.

**Q: Is the lock sufficient for multiple backend processes?**  
No. It is process-local. A distributed deployment would require a database atomic conditional update, lease, or distributed lock.

**Q: What consistency should be considered before detach?**  
Applications should flush/close writes. A production block-volume system would also coordinate filesystem unmounting and snapshot consistency.

**Q: EBS volume versus AMI?**  
A volume stores persistent data. An AMI is a bootable machine-image template. LocalEC2 AMIs clone the root VM, while LocalEC2 volumes remain separate mounts.

## 9. Launch templates

### Theory

A launch template is a versionable, reusable definition of how to create instances. In a full cloud it may contain the image, instance type, storage, network, security groups, keys, IAM role, and user data.

### Implementation

The LocalEC2 `LaunchTemplate` stores:

- Name and description
- Ubuntu image
- CPU count
- Memory in MB
- Disk in GB
- Owner and timestamps

Autoscaling uses it to construct managed `Instance` records. The backend supports create, list, get, patch, and delete. Deletion is blocked while an autoscaling group references it. The current UI exposes create/list/delete, not editing or direct manual launch.

### Likely viva questions

**Q: Why use a launch template?**  
It makes repeated VM creation consistent and separates machine configuration from autoscaling policy.

**Q: Does changing a template resize existing instances?**  
No. It affects future launches. Existing VMs retain the configuration used when created.

**Q: Are templates versioned?**  
Not in this implementation. Updating replaces the stored fields. Production systems often keep immutable versions.

**Q: What fields are missing compared with AWS?**  
AMI selection, key pairs, security groups, network interfaces, IAM profiles, tags, user data, metadata options, and versioning are not in the current template model.

## 10. Autoscaling

### Theory

Autoscaling changes the number of instances to meet demand or desired capacity. Important terms are:

- **Minimum size:** lower capacity bound.
- **Desired size:** capacity the controller currently tries to maintain.
- **Maximum size:** upper capacity bound.
- **Metric:** measured signal, here average CPU utilization.
- **Scale-out threshold:** add capacity above this level.
- **Scale-in threshold:** remove capacity below this level.
- **Evaluation periods:** consecutive samples required before action.
- **Cooldown:** waiting time after scaling before another metric-based action.

Using different scale-out and scale-in thresholds creates hysteresis, which prevents rapid oscillation near one boundary.

### Implementation algorithm

Every configured interval, normally 30 seconds, the autoscaling worker processes enabled groups:

1. Load all nondeleted instances belonging to the group.
2. If a managed instance has a pending/running operation, skip this evaluation.
3. If current size is below desired, queue one scale-out operation.
4. If current size is above desired, queue one scale-in operation.
5. Otherwise measure CPU in every running managed VM.
6. Compute the arithmetic mean.
7. Add it to an in-memory history deque.
8. Wait until at least `evaluation_periods` samples exist.
9. Enforce cooldown since `last_scaling_at`.
10. If all recent samples are at/above the out threshold and size is below maximum, increment desired and add one VM.
11. If all are at/below the in threshold and size is above minimum, decrement desired and terminate the newest eligible VM.

CPU is measured by sampling `/proc/stat` twice, one second apart, and calculating the non-idle percentage over that interval.

### Scale-out implementation

- Load the group's launch template.
- Create a managed instance named approximately `<group>-<number>`.
- Set its `autoscaling_group_id`.
- Create a `SCALE_OUT` operation with no key generation.
- Create a scaling-event record containing previous/new size, reason, and recent CPU.
- The normal operation worker launches the VM.

### Scale-in implementation

- Sort managed instances newest first.
- Choose the newest instance in `RUNNING`, `STOPPED`, or `HIBERNATED` state.
- Queue `SCALE_IN`.
- The operation worker purges the VM and soft-deletes its record.

### Desired-capacity reconciliation

Desired capacity is handled before CPU logic. This means creating a group with desired 2 but current 0 makes the worker add instances one operation/evaluation cycle at a time until there are 2. It also reduces excess capacity similarly.

### Likely viva questions

**Q: Horizontal versus vertical scaling?**  
Horizontal scaling changes the number of instances. Vertical scaling changes CPU/RAM of an instance. LocalEC2 autoscaling is horizontal.

**Q: Scale out versus scale up?**  
Scale out adds instances. Scale up gives one instance more resources.

**Q: Why require three samples?**  
It avoids reacting to a brief spike or dip. The value is configurable in the model and defaults to 3 in the UI.

**Q: Why have cooldown?**  
New capacity needs time to boot and affect load. Cooldown prevents repeated actions before the last action's effect is visible.

**Q: Why use two thresholds?**  
The gap between 25% scale-in and 70% scale-out is hysteresis. It reduces flapping.

**Q: How is group CPU calculated?**  
The worker measures each running member and calculates their arithmetic mean. Stopped or failed members do not provide CPU values.

**Q: What if one VM cannot return CPU data?**  
Its exception is ignored. The average uses successful running measurements; if none succeed, the group does not make a metric-based decision.

**Q: What happens at maximum capacity?**  
High CPU does not add another instance because `current_size < maximum_size` is false.

**Q: What happens at minimum capacity?**  
Low CPU does not remove another instance because `current_size > minimum_size` is false.

**Q: Does autoscaling distribute work?**  
The command-balancer feature distributes independent shell commands to the lowest-CPU running member. It does not distribute ordinary HTTP application requests; a production web service would still need an HTTP reverse proxy or network load balancer.

**Q: What happens when scaling is paused?**  
The worker ignores the disabled group. Existing VMs keep running; pausing does not terminate them.

**Q: What happens when a group is deleted?**  
The backend first disables it, queues termination for managed instances, and deletes the group metadata. The UI explicitly requests managed-instance termination.

**Q: Is the sample history persistent?**  
No. It is an in-memory deque. A backend restart loses recent evaluation samples, so the group must collect them again.

**Q: Is this highly available?**  
No. Workers are local to one FastAPI process. A production design would use a durable job queue, distributed locks/leases, persistent metrics, and multiple fault-tolerant workers.

**Q: What does target CPU 50% do?**  
It is stored as group metadata, but the current decision algorithm uses the explicit scale-in and scale-out thresholds, not a proportional target-tracking calculation.

## 10.1 Command load balancer

The LocalEC2 command load balancer is a load-aware job dispatcher connected to an autoscaling group. It is intentionally different from a traditional HTTP Application Load Balancer.

When a user submits a command:

1. The browser sends it over an authenticated WebSocket.
2. The backend validates the JWT, ownership, load-balancer state, and group relationship.
3. It finds nondeleted `RUNNING` group members with IP addresses.
4. It samples CPU on every member using two `/proc/stat` readings.
5. Failed samples exclude that member from the current selection.
6. It chooses the smallest tuple of CPU percentage, active-command count, and creation time.
7. It runs `multipass exec <vm> -- sh -lc <command>` with a timeout.
8. The terminal displays the chosen target, measured CPU, output, completion, or error.
9. Target CPU values refresh dynamically in the UI.

Commands are independent. Shell state such as `cd`, aliases, variables, or background jobs is not guaranteed to persist because the next command may execute on another VM. Persistent paths must be included in one command, for example `cd /opt/app && ./task.sh`.

This feature is best described as load-aware command scheduling rather than network packet balancing. Selection runs in FastAPI userspace; it is not implemented in the Linux kernel.

**Q: Why use WebSocket?**  
It provides an authenticated bidirectional channel for command submission, target-selection events, output, completion, and errors without repeated page requests.

**Q: Why measure every target before a command?**  
It makes selection reflect current CPU conditions instead of relying only on an older periodic sample.

**Q: What happens if the lowest-load VM fails?**  
Execution returns an error and its active-command counter is released. On the next command, failed CPU sampling excludes the unavailable VM.

**Q: Is this equivalent to AWS ALB?**  
No. AWS ALB distributes HTTP/HTTPS requests. LocalEC2 currently distributes independent shell commands based on VM CPU load.

## 11. Operations and asynchronous jobs

### Theory

Long infrastructure actions should not keep an HTTP request open. An operation resource gives the action its own ID, state, progress, timestamps, result, and error.

### Implementation

An `Operation` stores:

- Owner and optional instance
- Type: launch, start, stop, hibernate, terminate, scale out/in, reconcile
- Status: pending, running, succeeded, failed
- Progress and human-readable message
- Request and result payloads
- Error code and error message
- Created, started, and finished timestamps

The operation loop atomically selects the oldest pending operation with MongoDB `find_one_and_update`, changes it to running, and then executes it. Atomic claiming prevents the same single queue document from being claimed twice by competing claim calls, although the application is intentionally deployed with one worker process.

The UI polls the operations endpoint, displays up to the latest 100 user operations, and filters by status. Instance details show recent instance-specific operations.

### Likely viva questions

**Q: Why use a database queue?**  
It makes operations durable and observable with technology already used by the project. A production system could use Celery, RabbitMQ, Kafka, SQS, or another job system.

**Q: What does atomic claim mean?**  
Finding and changing `PENDING` to `RUNNING` happens as one database operation, preventing two callers from both seeing the same unchanged record and claiming it normally.

**Q: What happens when a worker crashes during an operation?**  
The operation may remain `RUNNING`. The current system would need recovery/lease logic for robust crash handling.

**Q: Error state versus operation failure?**  
`FAILED` describes the action. `ERROR` describes the instance after that action failed. They are related but belong to different resources.

**Q: Why store request payload?**  
It supports audit/debugging and enables retry of a failed launch with the original parameters.

## 12. Reconciliation

### Theory

Reconciliation is a control loop that compares desired state with actual provider state. It handles drift caused by external changes, crashes, or partial failures.

### Implementation concept

The background reconciliation worker periodically lists Multipass instances and compares them with active LocalEC2 records. This is necessary because a user could run Multipass commands outside LocalEC2, or a VM/provider failure could change actual state without an API request.

### Likely viva questions

**Q: Polling versus events?**  
Polling checks periodically and is simple but introduces delay and repeated work. Event-driven updates can be faster but require a reliable event source. LocalEC2 uses periodic reconciliation.

**Q: Why is reconciliation important after restart?**  
The database persists, and Multipass VMs may persist, but process memory does not. Reconciliation rebuilds confidence in the actual state.

**Q: What is eventual consistency?**  
The database and provider may temporarily differ, but background work makes them converge over time rather than requiring every component to change atomically.

## 13. Browser terminal

### Theory

An interactive terminal needs bidirectional, low-latency communication. HTTP request/response is awkward for continuous keystrokes and output, so WebSocket provides a persistent full-duplex channel. A pseudo-terminal (PTY) makes a child shell behave as if connected to a real terminal, supporting prompts, colors, line editing, signals, and resizing.

### Implementation

1. React creates an xterm.js terminal.
2. It connects to `/ws/instances/{id}/terminal` with the JWT in a WebSocket subprotocol.
3. FastAPI validates the access token, ownership, instance ID, and `RUNNING` state.
4. On Linux, the backend opens a PTY and starts `multipass shell <internal-name>`.
5. One asynchronous loop reads PTY output and sends it to the browser.
6. Another receives input/resize messages and writes them to the PTY.
7. On disconnect, the process group is terminated and the PTY is closed.

The JWT is placed in a subprotocol rather than the URL so it is less likely to appear in web-server access logs. Query-token support remains only for older clients.

### Likely viva questions

**Q: WebSocket versus HTTP?**  
HTTP is normally request/response. WebSocket maintains one bidirectional connection so the server can continuously push terminal output while receiving keystrokes.

**Q: What is a PTY?**  
A pseudo-terminal emulates terminal devices for a subprocess. Interactive programs detect it and behave like they do in a real shell window.

**Q: Does the terminal execute on the host?**  
The backend starts the host-side `multipass shell` client, but the interactive shell and entered commands run inside the selected guest VM.

**Q: How is authorization enforced?**  
The token identifies the user, the instance document is loaded, and its `user_id` must match before the connection is accepted.

**Q: Is WebSocket encrypted?**  
In local development the default is `ws://`, which is not TLS-encrypted. Production should be behind HTTPS and use `wss://`.

## 14. Monitoring

### Theory

Monitoring samples resource utilization so users and controllers can understand workload health. CPU and memory are utilization metrics; disk percentage is capacity utilization; network counters are cumulative bytes.

### Implementation

Every two seconds, the metrics WebSocket executes guest commands through Multipass:

- CPU: two `/proc/stat` readings one second apart.
- Memory: used/total from `free -m`.
- Disk: root filesystem percentage from `df -P /`.
- Network: RX/TX byte counters summed from `/proc/net/dev`.

FastAPI sends JSON points to React. The frontend retains the latest 60 points and renders Recharts line charts. If disconnected, the frontend attempts to reconnect. The autoscaler separately uses the same CPU sampling command on its own interval.

### Likely viva questions

**Q: Why sample `/proc/stat` twice?**  
CPU utilization is a rate over an interval. One reading is a cumulative counter; two readings allow calculation of busy time divided by total elapsed CPU time.

**Q: Are RX/TX chart values bandwidth?**  
No. They are cumulative byte counters. Bandwidth would require calculating the difference between samples divided by elapsed time.

**Q: Why keep only 60 points?**  
It bounds browser memory and creates a rolling live view. The project does not store long-term metrics.

**Q: Agent-based or agentless monitoring?**  
It is agentless from the project's perspective: no custom monitoring daemon is installed in the VM. The backend invokes standard guest commands through Multipass.

## 15. Authentication and authorization

### Theory

- **Authentication** answers “Who are you?”
- **Authorization** answers “May you access this resource?”

### Implementation

- Passwords are hashed with Argon2, not encrypted or stored in plaintext.
- A short-lived JWT access token normally lasts 15 minutes.
- A refresh JWT normally lasts 7 days and is stored in an HTTP-only, SameSite=Lax cookie.
- Refresh-token hashes and revocation state are stored in MongoDB.
- Refresh rotates the token: the old record is revoked and a new one is issued.
- Logout revokes the refresh token and deletes the cookie.
- REST dependencies load the current user.
- Resource queries include `user_id`, enforcing ownership.
- WebSocket endpoints independently validate JWT and ownership.

### Likely viva questions

**Q: Hashing versus encryption?**  
Hashing is one-way and is used for passwords. Encryption is reversible with a key and is used for private-key material that must later be downloaded.

**Q: Why Argon2?**  
Argon2 is a memory-hard password hashing algorithm designed to make brute-force attacks expensive.

**Q: Why use both access and refresh tokens?**  
Short-lived access tokens reduce exposure. A longer refresh token restores a session without forcing frequent login and can be tracked/revoked.

**Q: Why HTTP-only for the refresh cookie?**  
Browser JavaScript cannot read an HTTP-only cookie, reducing token theft through many XSS attacks.

**Q: Why store token hashes?**  
A database leak does not directly reveal usable raw refresh/download bearer tokens.

**Q: What is JWT HS256?**  
It signs token contents with an HMAC-SHA-256 shared secret. Anyone with the secret can create valid tokens, so the secret must be strong and protected.

**Q: Is CORS authentication?**  
No. CORS is a browser policy controlling which origins may call the API. Real security still requires authentication and authorization.

**Q: Is the system production-ready?**  
It contains meaningful security controls, but the README defines it as personal/educational. Production would require TLS, hardened secrets, distributed workers, stronger operational isolation, auditing, backups, rate limits, and a broader threat review.

## 16. MongoDB data model and integrity

### Why MongoDB?

MongoDB stores JSON-like documents that map naturally to Pydantic models and API responses. Motor/PyMongo async operations work well with FastAPI. It also makes operation payloads and flexible resource metadata convenient.

### Relationship examples

```text
User
 ├── Instances
 │    ├── KeyPair (optional)
 │    ├── SecurityGroups (many)
 │    ├── AMI source (optional)
 │    └── AutoScalingGroup (optional)
 ├── Volumes ── attached to one Instance (optional)
 ├── AMIs ── created from one stopped Instance
 ├── LaunchTemplates
 │    └── AutoScalingGroups
 └── Operations
```

The routes enforce ownership and application-level referential integrity. For example, referenced key pairs/security groups must belong to the same user; in-use keys, security groups, AMIs, and templates cannot be deleted.

### Likely viva questions

**Q: Why UUIDs?**  
UUIDs avoid simple sequential identifiers, reduce collision risk across creators, and are suitable for distributed resource identities.

**Q: Does MongoDB enforce all foreign keys?**  
No. MongoDB does not provide traditional relational foreign keys here. The application checks references before creation/deletion.

**Q: What is a race condition?**  
A result depends on timing between concurrent actions—for example, two volume-attach requests both observing “available.” Locks or atomic database operations are used to control such cases.

**Q: Why timestamps in UTC?**  
UTC avoids ambiguity across server/user time zones. The frontend converts timestamps to the user's local display time.

## 17. REST API and validation

### Theory and implementation

REST endpoints model resources with HTTP methods:

| Method | Typical meaning in LocalEC2 |
|---|---|
| GET | Read/list a resource |
| POST | Create a resource or start an action |
| PATCH/PUT | Replace/update properties |
| DELETE | Delete or queue termination |

Pydantic validates types and constraints before route logic. Examples include ports 1–65535, valid CIDRs, CPU/memory/disk bounds, and autoscaling capacity relationships. Errors use a structured response with status, detail, and a request ID.

### Likely viva questions

**Q: PUT versus PATCH?**  
PUT conventionally replaces a full representation; PATCH applies a partial change. Some project patch schemas currently require the full editable object, so their practical behavior is closer to replacement despite the method name.

**Q: What does HTTP 409 mean here?**  
The request conflicts with current resource state—for example, deleting an attached volume or starting an already-running instance.

**Q: What does HTTP 422 mean?**  
Input is structurally understood but fails validation, such as an invalid capacity relationship or too-small AMI disk.

**Q: Why return a request ID?**  
It helps correlate a client-visible failure with server logs and debugging traces.

## 18. Frontend implementation

### How it works

- React Router maps navigation paths to resource pages.
- React Hook Form validates and collects launch fields.
- Axios sends authenticated API requests.
- TanStack Query caches resource lists and refetches them.
- Mutations invalidate relevant query keys after changes.
- Operation/instance queries poll while asynchronous work progresses.
- xterm.js renders the interactive terminal.
- Recharts renders live metrics.

### Likely viva questions

**Q: Why TypeScript?**  
It catches mismatched fields and invalid states during development and documents API object shapes.

**Q: Why TanStack Query?**  
Server state needs caching, loading/error states, invalidation after mutations, and polling. The library standardizes those behaviors.

**Q: Client-side versus server-side validation?**  
Client validation improves usability, but it can be bypassed. Server-side validation is authoritative and required for security and integrity.

**Q: Why show operation progress instead of freezing the page?**  
Infrastructure actions can take minutes. Asynchronous feedback keeps the UI responsive and lets users navigate while work continues.

## 19. Feature limitations and honest defense answers

A strong viva answer distinguishes what the project models from what a production cloud provides.

| Feature | Current limitation | Reasonable improvement |
|---|---|---|
| VM backend | Single local host | Scheduler across multiple hypervisor nodes |
| Workers | In-process, single recommended API process | Durable external queue and distributed workers |
| Volumes | Host directories, logical quota only | Real block devices, quotas, snapshots, replication |
| Security groups | Guest UFW, launch-time application | Host/network-fabric enforcement and dynamic updates |
| Networking | Multipass networking, no VPC/subnets | Virtual networks, routes, NAT, elastic IPs |
| Work distribution | Independent commands only | HTTP/TCP listeners, queues, draining, retries |
| Metrics | Live in memory, short window | Time-series database, alerts, retention |
| AMIs | Full local clones | Snapshot-backed/versioned images and lifecycle management |
| Templates | Limited fields, no versions | Immutable template versions and more launch options |
| Availability | One host is a failure domain | Replication and multi-node failover |
| Secrets | Secure patterns but local educational deployment | KMS/HSM or dedicated secret manager |
| Authorization | Per-user ownership | Roles, projects, policies, audit trail |

### Likely viva questions

**Q: What is the biggest difference from AWS EC2?**  
AWS is a distributed, multi-tenant public cloud with managed networking, block storage, availability zones, APIs, billing, and large-scale fault tolerance. LocalEC2 is a single-host educational control plane over Multipass.

**Q: What is the single point of failure?**  
The Ubuntu host is the main failure domain. If it fails, compute and local volume data become unavailable.

**Q: What would you improve first for production?**  
Move work to a durable distributed queue, enforce stable secret management and TLS, implement robust recovery/leases, use real storage quotas/block storage, and add host/network-level isolation.

**Q: Why is the project still valuable?**  
It demonstrates real control-plane patterns: asynchronous jobs, desired/actual state, VM provider abstraction, resource ownership, lifecycle state machines, SSH provisioning, firewall translation, persistent storage, image cloning, monitoring, and autoscaling.

## 20. Rapid-fire viva questions

**Q: What is cloud computing?**  
On-demand access to configurable compute, storage, and networking resources through managed interfaces and automation.

**Q: IaaS, PaaS, and SaaS?**  
IaaS exposes infrastructure such as VMs; PaaS exposes an application platform; SaaS exposes a complete application. LocalEC2 models IaaS concepts.

**Q: What is virtualization?**  
The abstraction of physical compute resources into isolated virtual machines.

**Q: What is a hypervisor?**  
Software that creates and manages VMs and mediates their access to hardware.

**Q: Type 1 versus Type 2 hypervisor?**  
Type 1 runs directly on hardware; Type 2 runs through a host OS. Linux KVM turns the Linux kernel into a hypervisor, with QEMU providing userspace device emulation.

**Q: What is cloud-init?**  
A guest first-boot system that applies machine initialization data, such as users and SSH keys.

**Q: What is UFW?**  
Uncomplicated Firewall, a high-level Linux firewall tool that configures netfilter.

**Q: What is netfilter?**  
The Linux kernel framework that filters, tracks, and transforms network packets.

**Q: What is TCP's three-way handshake?**  
SYN, SYN-ACK, ACK. If the initial packet is blocked, the connection cannot establish.

**Q: TCP versus UDP?**  
TCP is connection-oriented and reliable; UDP is connectionless and does not guarantee delivery/order.

**Q: What is an IP address?**  
A Layer 3 address identifying a network interface/location for packet routing.

**Q: What is NAT?**  
Network Address Translation rewrites address/port information between networks, commonly allowing private hosts to share external connectivity.

**Q: What is a root disk?**  
The boot storage containing the guest operating system and root filesystem.

**Q: Ephemeral versus persistent storage?**  
Ephemeral storage is tied to compute lifecycle; persistent storage exists independently. LocalEC2 volume directories are intended to be persistent.

**Q: What is a snapshot?**  
A point-in-time storage copy used for backup or creating new storage/images.

**Q: What is the LocalEC2 command load balancer?**  
It is an authenticated dispatcher that measures group-member CPU and sends each independent shell command to the lowest-load running VM.

**Q: What is health checking?**  
Testing whether a component can serve its purpose. LocalEC2 readiness checks MongoDB, Multipass, and the operation worker; it does not yet replace unhealthy workload instances based on application health.

**Q: What is idempotency?**  
The property that repeating an operation has the same intended effect rather than creating unwanted duplicates. Launch retry removes a partial provider VM first.

**Q: What is eventual consistency?**  
Components may differ temporarily but converge through background processing and reconciliation.

**Q: What is horizontal scalability?**  
Increasing capacity by adding instances or service replicas.

**Q: What is fault tolerance?**  
Continuing correct service despite component failures. The present single-host design has limited fault tolerance.

**Q: What is least privilege?**  
Giving a user/process only the permissions required. Narrow CIDRs and ports follow least privilege better than `0.0.0.0/0`.

**Q: Why should private keys never be committed to Git?**  
Repository history can retain the secret permanently and distribute it to everyone with access.

**Q: Why should APIs never trust frontend input?**  
Users can bypass or modify browser code and call the backend directly. The backend must authenticate and validate every request.

## 21. A strong two-minute project explanation

> “LocalEC2 is a local EC2-style control plane built with React, FastAPI, MongoDB, and Multipass. The browser sends authenticated REST requests to FastAPI. Instead of waiting for slow VM work, the backend stores an operation in MongoDB and returns 202. A worker atomically claims pending jobs and calls Multipass to launch, start, stop, suspend, clone, mount, or delete real Ubuntu VMs. MongoDB stores ownership, desired state, relationships, and history, while reconciliation compares that state with Multipass.
>
> SSH keys are generated with 3072-bit RSA. Public keys enter the guest through cloud-init or `authorized_keys`, while private keys are Fernet-encrypted and downloadable once. Security groups are translated to UFW rules inside the VM, so CIDRs work at OSI Layer 3 and TCP/UDP ports at Layer 4. Volumes model EBS-like persistent storage using independent host directories mounted through Multipass; they are not true block devices. AMIs are stopped Multipass backing clones. Launch templates define worker resources, and an autoscaling loop uses consecutive average-CPU samples, minimum/desired/maximum capacity, thresholds, and cooldown to add or terminate managed VMs. WebSockets provide an interactive PTY terminal and live monitoring. The project is educational and single-host, but it demonstrates core cloud control-plane patterns honestly.”

## 22. Final defense checklist

Before the presentation, be able to explain and demonstrate:

- [ ] Why LocalEC2 is a control plane and Multipass is the VM provider.
- [ ] VM versus container and Multipass versus hypervisor.
- [ ] The complete asynchronous launch request flow.
- [ ] Instance states and valid transitions.
- [ ] Why security groups operate mainly at OSI Layers 3 and 4.
- [ ] How a denied TCP SYN prevents the handshake.
- [ ] Why UFW-based security differs from AWS network-fabric security groups.
- [ ] Public/private SSH key roles and one-time private-key handling.
- [ ] Why LocalEC2 volumes are EBS-like but not real EBS/block devices.
- [ ] Why attached volume data is not included in an AMI.
- [ ] AMI cloning and stopped-source consistency.
- [ ] Launch-template purpose and current fields.
- [ ] Minimum, desired, maximum, thresholds, evaluation periods, cooldown, and hysteresis.
- [ ] Why command dispatching distributes shell work but does not replace an HTTP load balancer.
- [ ] WebSocket and PTY roles in the browser terminal.
- [ ] How CPU, memory, disk, and network metrics are collected.
- [ ] Authentication versus authorization; hashing versus encryption.
- [ ] MongoDB's role and application-level relationships.
- [ ] Current limitations and the next production improvements.
