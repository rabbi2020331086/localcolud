# LocalEC2 — Complete Feature Demonstration

This guide demonstrates every EC2-style feature currently available in LocalEC2: instances and their properties, the browser terminal, security groups, key pairs, launch templates, autoscaling, volumes, AMIs, and operation history.

> LocalEC2 is a local educational cloud console. Its instances are real Ubuntu virtual machines managed by Multipass on the host PC; they are not AWS EC2 instances.

## 1. Prerequisites and startup

Before starting, confirm that:

- The host is Ubuntu with hardware virtualization enabled.
- Multipass, MongoDB connectivity, and the project dependencies have been configured.
- `backend/.env` contains a working `MONGODB_URI`, `MONGODB_DATABASE`, and the other values created by the setup script.
- The host has enough free CPU, RAM, and disk for the VMs used in this demonstration.

For a first-time setup, run:

```bash
cd /path/to/EC2_V3
chmod +x setup-ubuntu.sh start-ubuntu.sh
./setup-ubuntu.sh
```

Start LocalEC2:

```bash
cd /path/to/EC2_V3
./start-ubuntu.sh
```

Open `http://localhost:5173`. Register an account if necessary, then sign in. The backend API documentation is available at `http://localhost:8000/docs`.

Use the following names throughout the demonstration so that the relationships between resources are easy to recognize:

| Resource | Demonstration name |
|---|---|
| Key pair | `demo-admin-key` |
| Security group | `demo-web-sg` |
| First instance | `demo-web-01` |
| Volume | `demo-data` |
| AMI | `demo-web-ami` |
| Instance launched from AMI | `demo-web-clone` |
| Launch template | `demo-worker-template` |
| Autoscaling group | `demo-workers` |

## 2. Key pairs

A key pair is a reusable SSH identity. LocalEC2 stores its public key and temporarily stores the encrypted private key. The private key can be downloaded only once.

### Create and download a reusable key pair

1. In the left navigation, select **Key pairs**.
2. Enter `demo-admin-key` in **Key pair name**.
3. Select **Create key pair**.
4. Confirm that the new row shows its name and fingerprint.
5. Select the download icon once and save `demo-admin-key.pem` securely.
6. On the computer where the key was downloaded, restrict its permissions:

   ```bash
   chmod 600 demo-admin-key.pem
   ```

7. Do not lose the file. A second download attempt must fail because LocalEC2 removes the stored private-key material after the first successful download.

### Properties demonstrated

| Property | Meaning |
|---|---|
| Name | User-visible unique name for the reusable identity |
| Fingerprint | Identifier used to verify which key is associated with an instance |
| Public key | Installed in the VM for authentication |
| Private key | Secret PEM file downloaded once and kept by the user |

### Important behavior

- A reusable key pair can be selected during multiple instance launches.
- An instance launch can instead generate a unique one-time key. When that launch succeeds, the browser downloads the generated PEM file from the instance details page.
- Selecting a reusable key and leaving **Create a unique SSH key pair** checked does not create an additional key; the reusable key takes precedence.
- A key pair cannot be deleted while an active instance references it. Terminate those instances first.
- Deleting a key pair from LocalEC2 does not erase a PEM file already downloaded to a user's computer.

## 3. Security groups

Security groups are reusable firewall policies. LocalEC2 applies the selected rules inside a new guest using UFW during launch.

### Create a web and SSH security group

The current form creates one inbound rule per security group. For this demonstration, create one group for SSH and a second group for HTTP, then select both when launching the instance.

Create the SSH group:

1. Select **Security groups**.
2. Set **Name** to `demo-ssh-sg`.
3. Set **Description** to `Allow SSH for the demonstration`.
4. Set **Protocol** to `tcp`.
5. Set **From port** and **To port** to `22`.
6. Set **Source CIDR** to the narrowest network that needs access. For a simple local demonstration, use `0.0.0.0/0`, understanding that it means any IPv4 source.
7. Leave **Restrict outbound traffic** cleared.
8. Select **Create security group**.

Create the HTTP group:

1. Set **Name** to `demo-web-sg`.
2. Set **Description** to `Allow HTTP for the demonstration`.
3. Choose `tcp`, ports `80` to `80`, source `0.0.0.0/0`.
4. Leave outbound traffic unrestricted and create the group.

### Demonstrate restricted outbound traffic

Optionally create `demo-https-egress-sg`:

1. Add an inbound rule suitable for the workload.
2. Enable **Restrict outbound traffic**.
3. Set the outbound protocol to `tcp`, ports `443` to `443`, and destination `0.0.0.0/0`.
4. Create the group.

When outbound restriction is cleared, the group's empty egress-rule list means **all outbound traffic is allowed**. When it is enabled, only the configured outbound rule is allowed.

### Rule properties demonstrated

| Property | Meaning |
|---|---|
| Protocol | `tcp` or `udp` |
| From port / To port | Inclusive port or port range, from 1 through 65535 |
| Source | Inbound IPv4 CIDR allowed to reach the port |
| Destination | Outbound IPv4 CIDR allowed for a restricted egress rule |

Security groups cannot be deleted while an active instance references them. The UI currently supports creation and deletion; although the API supports replacement updates, the UI has no rule-editing control. Rules selected at launch are applied to the guest at launch time.

## 4. Launch an instance

1. Select **Instances**, then **Launch instance**.
2. Enter these general properties:

   - **Name:** `demo-web-01`
   - **Description:** `Primary VM for the complete LocalEC2 demonstration`
   - **Machine image:** `Standard Ubuntu image`
   - **Ubuntu image:** `24.04`

3. Select **Instance type** `t2.small`. This allocates 1 vCPU and 2 GB memory.
4. Leave **Disk** at 10 GB.
5. Under **Reusable key pair**, select `demo-admin-key`.
6. Select both `demo-ssh-sg` and `demo-web-sg` under **Security groups**.
7. Select **Launch instance**.
8. The request is queued and the instance details page opens. Watch the progress banner and recent operations until the instance reaches `RUNNING`.
9. The first use of an Ubuntu image may take several minutes because Multipass must download it.

### Instance types and allocation properties

| Type | vCPU | Memory | Typical category |
|---|---:|---:|---|
| `t2.micro` | 1 | 1 GB | Burstable |
| `t2.small` | 1 | 2 GB | Burstable |
| `t2.medium` | 2 | 4 GB | Burstable |
| `t4.micro` | 2 | 1 GB | Burstable |
| `t4.small` | 2 | 2 GB | Burstable |
| `t4.medium` | 2 | 4 GB | Burstable |
| `m5.large` | 2 | 8 GB | General purpose |
| `m5.xlarge` | 4 | 16 GB | General purpose |
| `custom` | 1–8 | 512–16384 MB | User-defined allocation |

The preset determines the effective CPU and memory allocation shown in **Local allocation**. Custom mode uses the entered vCPU and memory values. Disk can be 5–100 GB for a standard image.

### Instance properties

Open the instance details page and verify:

| Property | Where shown / meaning |
|---|---|
| Name | Display name used throughout the console |
| Description | User-supplied purpose of the VM |
| Status | Current lifecycle state |
| IPv4 address | Address assigned by Multipass |
| SSH command | `ssh ubuntu@<IPv4-address>` |
| Created | Time the LocalEC2 instance record was created |
| Image | Ubuntu 22.04, Ubuntu 24.04, or an AMI-derived image |
| Compute | Allocated vCPU and RAM |
| Storage | Root-disk allocation in GB |
| Key fingerprint | Fingerprint of the SSH identity installed during launch |
| Instance type | Preset name such as `t2.small`, or `custom` |
| Security groups | Firewall policies selected at launch |
| AMI | Source AMI ID when launched from a custom image |
| Autoscaling group | Owning group for an autoscaling-managed instance |

The list page additionally shows the IP address, resource summary, whether an SSH key exists, creation date, and available lifecycle actions.

### Lifecycle statuses and actions

| Status | Meaning / available next action |
|---|---|
| `PROVISIONING` | Launch record has been created and is waiting to run |
| `STARTING` | VM is being created or started |
| `RUNNING` | Terminal, monitoring, stop, and hibernate are available |
| `STOPPING` | A stop operation is in progress |
| `STOPPED` | VM is powered off; it can be started or used as an AMI source |
| `HIBERNATING` | Suspension is in progress |
| `HIBERNATED` | VM is suspended and can be started to resume |
| `TERMINATING` | Permanent deletion is in progress |
| `ERROR` / `UNKNOWN` | Launch or reconciliation failed; retry may be available |

Only one operation can run against an instance at a time.

## 5. Browser terminal

The browser terminal runs commands inside the selected VM, never on the Ubuntu host.

1. Wait until `demo-web-01` is `RUNNING`.
2. From its row, select the terminal icon, or open the details page and select **Connect**.
3. Wait for the terminal status to show `RUNNING`/connected.
4. Run commands that identify the guest and demonstrate persistence:

   ```bash
   whoami
   hostname
   cat /etc/os-release
   nproc
   free -h
   df -h /
   ip -4 address
   ```

5. Install and start a small web service for the later AMI test:

   ```bash
   sudo apt-get update
   sudo apt-get install -y nginx
   echo '<h1>LocalEC2 AMI demonstration</h1>' | sudo tee /var/www/html/index.html
   sudo systemctl enable --now nginx
   curl http://localhost
   ```

6. Create a file on the root disk:

   ```bash
   echo 'created on demo-web-01' | sudo tee /opt/instance-proof.txt
   cat /opt/instance-proof.txt
   ```

The terminal supports interactive input and resizes with the browser window. If the VM is not running, the page asks the user to start it first.

### Optional external SSH test

From a host that can route to the VM address, use the downloaded reusable key:

```bash
ssh -i demo-admin-key.pem ubuntu@<instance-ip>
```

The browser terminal itself does not require the downloaded PEM file. External SSH depends on host/VM networking and the SSH security-group rule.

## 6. Monitoring

1. Open `demo-web-01` and select **Monitor**.
2. Confirm the connection status becomes running.
3. Observe the rolling charts for CPU, memory, root-disk use, received bytes, and transmitted bytes.
4. In a terminal, briefly create CPU load:

   ```bash
   timeout 30s sh -c 'while :; do :; done'
   ```

5. Return to monitoring and confirm CPU activity increases.

The page retains the latest 60 samples and describes them as an approximately two-minute rolling view. Metrics come from inside the VM. Monitoring waits or reconnects while a VM is unavailable.

## 7. Volumes

LocalEC2 volumes are host-backed directories independent of a VM. Their data survives detach, instance termination, and attachment to another instance. The configured GB value is a logical capacity used for reporting; it is not a separately formatted block device.

### Create, attach, and write data

1. Select **Volumes**.
2. Set **Volume name** to `demo-data`.
3. Set **Capacity GB** to `10` and select **Create volume**.
4. In the volume card, select running instance `demo-web-01`.
5. Set **Mount path** to `/mnt/data`.
6. Select **Attach**.
7. Open the terminal for `demo-web-01` and run:

   ```bash
   echo 'persistent volume data' | sudo tee /mnt/data/volume-proof.txt
   cat /mnt/data/volume-proof.txt
   ls -la /mnt/data
   ```

8. Return to **Volumes** and observe used bytes, remaining bytes, usage percentage, attached state, and mount path.

### Detach and move the data

1. Select **Detach** on `demo-data`.
2. Confirm the volume becomes **Available**.
3. Attach it to another running instance at `/mnt/data`.
4. In that instance's terminal, run:

   ```bash
   cat /mnt/data/volume-proof.txt
   ```

5. Confirm the original text is present.

A volume can be attached to only one instance at a time and can attach only to a running instance through the UI. Detach it before deletion. Deleting a volume permanently removes its host directory and all contained data.

## 8. Stop, hibernate, start, retry, and terminate

### Stop and start

1. On **Instances**, select **Stop** for `demo-web-01`.
2. Watch `STOPPING` become `STOPPED`.
3. Select **Start** and confirm the VM returns to `RUNNING` with its root-disk files intact.

### Hibernate and resume

1. With the instance running, start a persistent shell process or prepare some session state.
2. Select **Hibernate**.
3. Watch `HIBERNATING` become `HIBERNATED`.
4. Select **Start** to resume it.

Hibernate uses Multipass suspension and is intended to preserve the in-memory session, while stop performs a normal VM power-off.

### Retry a failed launch

If a launch enters `ERROR` or `UNKNOWN`, correct the host issue—for example, insufficient resources or unavailable Multipass—then select **Retry launch**. Retry is available only for a failed launch whose original request is still recorded.

### Terminate

Termination permanently deletes the VM after confirmation and removes it from the active instance list. Independent LocalEC2 volumes are not deleted with the VM, but it is safest to detach them first. Termination is different from stop and hibernate and cannot be undone.

## 9. AMIs

An AMI is a local clone of a stopped instance. It captures the source VM's operating system, installed software, and root-disk data so that another VM can be launched from that state.

### Create an AMI

1. Confirm `/opt/instance-proof.txt` and the Nginx page were created on `demo-web-01`.
2. Stop `demo-web-01` and wait for `STOPPED`. A running or hibernated instance cannot be used as an AMI source.
3. Select **AMIs**.
4. Enter:

   - **Name:** `demo-web-ami`
   - **Source instance:** `demo-web-01`
   - **Description:** `Ubuntu with Nginx and demonstration content`

5. Select **Create AMI** and wait for it to appear as `AVAILABLE`.
6. Verify its base Ubuntu version, vCPU, memory, disk size, creation date, and description.

### Launch and verify an AMI clone

1. Select **Instances** → **Launch instance**.
2. Name it `demo-web-clone`.
3. Under **Machine image**, select `demo-web-ami`.
4. Select an instance type, reusable key, and security groups.
5. Keep disk at the AMI size or increase it. An AMI-backed disk cannot be smaller than the source AMI disk.
6. Launch and wait for `RUNNING`.
7. Open its terminal and verify the captured contents:

   ```bash
   cat /opt/instance-proof.txt
   systemctl is-enabled nginx
   systemctl is-active nginx
   curl http://localhost
   ```

The clone is independent of the source after launch. An AMI cannot be deleted while an active instance references it; terminate those instances first. Deleting the AMI deletes its hidden Multipass backing clone but does not delete already launched instances.

## 10. Launch templates

A launch template stores a reusable VM configuration for autoscaling-managed launches.

1. Select **Launch templates**.
2. Select **New template**.
3. Enter:

   - **Name:** `demo-worker-template`
   - **Description:** `Small Ubuntu workers for autoscaling tests`
   - **Image:** `24.04`
   - **vCPU:** `1`
   - **Memory MB:** `1024`
   - **Disk GB:** `5`

4. Select **Create template**.
5. Verify the card displays the image, CPU, memory, and disk properties.

Current templates contain only name, description, Ubuntu image, CPU count, memory, and disk. The current UI does not include AMIs, security groups, key pairs, or user data in a template, and does not provide a direct “launch from template” button. Autoscaling groups consume templates automatically.

A template should not be deleted while an autoscaling group depends on it. Delete the dependent group first.

## 11. Autoscaling

Autoscaling keeps a group of local VMs near a desired capacity and changes that capacity after sustained CPU readings.

### Create a group and demonstrate desired capacity

1. Create `demo-worker-template` first.
2. Select **Autoscaling** → **New group**.
3. Enter:

   - **Name:** `demo-workers`
   - **Launch template:** `demo-worker-template`
   - **Minimum:** `1`
   - **Desired:** `1`
   - **Maximum:** `3`
   - **Scale out CPU %:** `70`
   - **Scale in CPU %:** `25`

4. Select **Create group**.
5. Open **Operations** and watch a `SCALE_OUT` launch operation create `demo-workers-1` to satisfy desired capacity.
6. Open **Instances** and confirm the managed instance uses the template's image, CPU, memory, and disk values.

Capacity values must satisfy `minimum ≤ desired ≤ maximum`. The UI creates groups with 3 evaluation periods, a 300-second cooldown, a 50% informational target, and scaling enabled.

### Demonstrate CPU scale out

1. Wait for `demo-workers-1` to reach `RUNNING`.
2. Open its browser terminal and generate sustained CPU load. For a one-vCPU template, run:

   ```bash
   timeout 180s sh -c 'while :; do :; done'
   ```

3. Keep the load above the 70% scale-out threshold across at least three autoscaling evaluations.
4. Watch **Operations** for a `SCALE_OUT` action and **Instances** for `demo-workers-2`.
5. Scaling will not exceed maximum size 3 and will not start another group operation while a managed-instance operation is pending or running.

### Demonstrate scale in

1. Stop the CPU load and let average CPU remain at or below 25% for at least three evaluations.
2. Wait until the 300-second cooldown from the last scaling action expires.
3. Watch **Operations** for `SCALE_IN` and confirm the newest eligible managed instance is terminated.
4. Scaling will not reduce the group below minimum size 1.

The worker checks enabled groups periodically; the exact wait also depends on `AUTOSCALING_INTERVAL_SECONDS` in the backend configuration. CPU decisions use the average collected from currently running instances in the group.

### Pause, resume, and delete

1. Select **Pause scaling** to disable evaluation for the group.
2. Select **Resume scaling** to enable it again.
3. To clean up, select the delete icon and confirm. This permanently deletes the group and queues termination of all instances it manages.

Pausing scaling does not itself terminate existing instances. Deleting a group explicitly does.

## 12. Operations

Operations provide persistent progress and history for VM actions.

1. Select **Operations**.
2. Use the **Status** filter to display `ALL`, `PENDING`, `RUNNING`, `SUCCEEDED`, or `FAILED` operations.
3. For each row, inspect:

   | Column | Meaning |
   |---|---|
   | Type | Action such as launch, start, stop, hibernate, terminate, scale out, or scale in |
   | Status | Current operation state |
   | Progress | Completion percentage and progress bar |
   | Message | Current activity or failure detail |
   | Started | Start time, or creation time before work starts |
   | Duration | Elapsed seconds between start and finish |

4. Launch or stop an instance in another browser tab and watch the row move through `PENDING` → `RUNNING` → `SUCCEEDED`.
5. If an action fails, select `FAILED` to isolate it and read the error message.

Instance details show the six most recent operations for that instance and display an active-operation banner. The global Operations page shows history across instances. Operations are asynchronous: accepting an action means it has been queued, not that it has already completed.

## 13. End-to-end verification checklist

The demonstration is complete when all applicable checks pass:

- [ ] Reusable key pair created, downloaded once, and protected with mode `600`.
- [ ] Second private-key download correctly rejected.
- [ ] SSH and HTTP security groups created and selected at launch.
- [ ] `demo-web-01` reached `RUNNING` and displayed IP, compute, storage, and key properties.
- [ ] Browser terminal executed commands inside the VM.
- [ ] Monitoring displayed CPU, memory, disk, RX, and TX metrics.
- [ ] Volume attached, stored a file, detached, and exposed the same file after reattachment.
- [ ] Stop/start preserved root-disk content.
- [ ] Hibernate/resume completed successfully.
- [ ] AMI captured a stopped instance.
- [ ] `demo-web-clone` contained the AMI's file and installed Nginx service.
- [ ] Launch template stored the expected worker allocation.
- [ ] Autoscaling reconciled desired capacity.
- [ ] Sustained CPU caused scale out and sustained low CPU caused scale in.
- [ ] Pause/resume changed autoscaling status.
- [ ] Operations showed action type, progress, result, timestamps, and failures where applicable.

## 14. Safe cleanup order

Clean up in dependency order to avoid “resource is in use” errors:

1. Delete `demo-workers`; confirm termination of its managed instances.
2. Terminate `demo-web-clone` and any other AMI-backed instances.
3. Start `demo-web-01` temporarily if needed to detach `demo-data`, then detach the volume.
4. Terminate `demo-web-01` and wait for its termination operation to succeed.
5. Delete `demo-data` only if its test data is no longer needed.
6. Delete `demo-web-ami`.
7. Delete `demo-worker-template`.
8. Delete `demo-ssh-sg`, `demo-web-sg`, and any optional security group.
9. Delete `demo-admin-key`.

Always inspect **Operations** after destructive actions and wait for success before deleting a resource on which another resource depends.

## 15. Troubleshooting

| Symptom | Check |
|---|---|
| Instance remains pending or fails | Read the failed operation, inspect `backend/backend.error.log`, verify Multipass and available host resources |
| First launch is slow | Multipass may be downloading the selected Ubuntu image |
| Terminal does not connect | Instance must be `RUNNING`; verify backend WebSocket connectivity |
| External SSH fails | Check the VM IP, key file permissions, selected SSH security group, and host routing |
| Cannot create an AMI | Source must be exactly `STOPPED`, not running or hibernated |
| Cannot delete a key or security group | An active instance still references it |
| Cannot delete an AMI | An active instance was launched from it |
| Cannot delete a volume | Detach it first |
| Volume attach fails | Target must be running, volume must be available, and mount path must be valid |
| Autoscaling does not react | Group must be enabled; wait for three samples and cooldown; confirm managed VMs are running |
| Another action returns a conflict | Wait for the instance's current pending/running operation to finish |
| MongoDB timeout | Allow the host public IP in Atlas and verify the connection URI |
> Historical note: these manual scenarios target the original single-host prototype. Use the
> automated checks and distributed-node workflow documented in `README.md` for the current system.

