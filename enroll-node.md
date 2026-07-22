# Enroll This Ubuntu PC as a LocalEC2 Node

Run every command in this file on the new compute-node PC.

LocalEC2 server:

```text
http://100.74.239.28:8000
```

## 1. Install and connect Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the authentication URL printed by `tailscale up` and sign in to the provided Tailscale network.

Verify the connection:

```bash
tailscale status
tailscale ping 100.74.239.28
curl -fsS http://100.74.239.28:8000/health
curl -fsS -o /dev/null -w 'Installer HTTP status: %{http_code}\n' \
  http://100.74.239.28:8000/api/v1/agent/install.sh
```

Continue only if the health request succeeds and the installer status is `200`.

## 2. Check Ubuntu and virtualization

```bash
uname -m
cat /etc/os-release | grep -E '^(NAME|VERSION|ID)='
lscpu | grep -E 'Model name|Virtualization|Hypervisor'
grep -Eoc '(vmx|svm)' /proc/cpuinfo
```

Required: Ubuntu, `x86_64`, and a virtualization flag count greater than `0`.

If the count is `0`, enable Intel VT-x/VMX or AMD-V/SVM in BIOS/UEFI, reboot, and check again.

## 3. Update Ubuntu and check resources

```bash
sudo apt-get update
sudo apt-get upgrade -y
free -h
df -h /
```

## 4. Install the LocalEC2 node

```bash
curl -fsSL 'http://100.74.239.28:8000/api/v1/agent/install.sh' |
  sudo bash -s -- \
  --api-url 'http://100.74.239.28:8000/api/v1' \
  --node-id '809c7ea8-9136-4235-8ceb-5d29553cd397' \
  --token 'P_-hcePXHlEZkUMLQAavcMiu-2kBaLtgk-z-yChHZSuTjef5a6vUL75KEvKh0gnq'
```

The installer adds KVM, libvirt, Vagrant, `vagrant-libvirt`, and the LocalEC2 agent.

## 5. Verify the installation

```bash
test -c /dev/kvm && echo "KVM available"
sudo systemctl is-active libvirtd
sudo systemctl status localec2-agent --no-pager
sudo virsh --connect qemu:///system net-list --all
sudo -u localec2 env HOME=/var/lib/localec2 \
  VAGRANT_HOME=/var/lib/localec2/.vagrant.d vagrant plugin list
```

Required: KVM is available, both services and the `default` network are active, and `vagrant-libvirt` appears.

Pre-download Ubuntu 22.04 on every node before launching instances:

```bash
sudo -u localec2 env HOME=/var/lib/localec2 VAGRANT_HOME=/var/lib/localec2/.vagrant.d \
  VAGRANT_CWD=/var/lib/localec2 LIBVIRT_DEFAULT_URI=qemu:///system \
  bash -c 'cd /var/lib/localec2 && exec vagrant box add bento/ubuntu-22.04 --provider=libvirt'
```

If a launch remains at 35%, inspect the running VM and capture its boot screen:

```bash
sudo virsh --connect qemu:///system list --all
sudo virsh --connect qemu:///system domifaddr VM_DOMAIN_NAME --source lease
sudo virsh --connect qemu:///system screenshot VM_DOMAIN_NAME /tmp/localec2-boot.ppm
sudo journalctl -u localec2-agent --no-pager -n 100
```

Replace `VM_DOMAIN_NAME` with the name shown by `virsh list`. Share `/tmp/localec2-boot.ppm` for diagnosis.
