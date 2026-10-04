# antiX Full Virtual Machine Setup

This guide prepares a Fedora host and creates reusable antiX Full virtual machines with KVM, libvirt, and `scripts/cicada-setup.sh`.

Each guest runs the antiX live system with its own persistent root filesystem. Do not run the antiX installer inside the guest.

## Requirements

Place the antiX Full ISO in `~/Downloads` and keep the setup script and helper in `~/Documents/Shared`:

- The antiX Full x64 ISO, renamed to `cicada.iso` whatever its version number (for example 26 or 26.1). antiX Full is expected because it is a lightweight OS that runs on a single vCPU.
- The setup script named `cicada-setup.sh` in `~/Documents/Shared`.
- Its companion helper named `cicada-network.py` in `~/Documents/Shared`.

The host must be a Fedora x86_64 system with hardware virtualization enabled and working native IPv6 on its default uplink, Ethernet or Wi-Fi, using a global `/64` prefix. This setup does not obtain another ISP prefix or make an IPv4-only connection support IPv6.

### Apply the Maintained Setup Files

From the root of your clone of this repository, copy the maintained script and helper together into the shared folder. Host setup and existing-VM upgrades keep these copies current.

```bash
install -D -m 0755 scripts/cicada-setup.sh "$HOME/Documents/Shared/cicada-setup.sh"
install -D -m 0644 scripts/cicada-network.py "$HOME/Documents/Shared/cicada-network.py"
```

Repeat this step after pulling repository updates.

## VM Specifications

| Component | Configuration |
| --- | --- |
| Guest OS | antiX x64 Full, booted as a live system from the shared read-only ISO |
| Processor | 1 vCPU with host CPU passthrough |
| Memory | 2,048 MiB |
| Persistent storage | One private 1,000,000,000-byte raw disk per VM, including an 850 MiB persistent root filesystem |
| Swap | 1 GiB zram inside the VM's allocated memory |
| Shared OS data | All VMs use the same read-only ISO and shared extracted boot files; each VM keeps its own persistent disk and browser profile |
| Control folder | `~/Documents/Shared` is writable through virtiofs and mounted at `/mnt/shared` for setup files and logs |
| Download folder | A per-VM host directory selected with `VM_DOWNLOAD` is mounted at `/mnt/downloads` and used as the guest's Downloads directory |
| Network | Two virtio adapters: default IPv4 NAT for system maintenance/DNS, and a dedicated IPv6 network with an explicit per-VM public source address |
| Display | SPICE bound to `127.0.0.1` with virtio video |
| VM configuration | A unique UUID, MAC address, hostname, and automatically selected geographic timezone |

The selected timezone is chosen from the canonical geographic IANA timezones worldwide, as listed in the host's `/usr/share/zoneinfo/tzdata.zi`. Backward-compatible aliases such as `America/Louisville` are excluded because the antiX guest does not ship them. Each managed VM receives a different timezone name until the available pool is exhausted. The timezone changes the guest's local time only; it does not change the public IP address or configure a VPN.

## Network Separation

Each VM keeps its own disk, Firefox profile, UUID and virtual MAC addresses. Its dedicated adapter receives one fixed private IPv6 address from `fd71:6e9f:db42:1::/64`. Fedora maps that address to one reserved public IPv6 `/128` on the current default uplink, whether Ethernet or Wi-Fi.

The registry at `/var/lib/cicada-network/state.json` keys assignments by VM UUID. An assignment is reused after restarts and name changes. If the ISP changes the public prefix, the helper preserves each VM's unique interface identifier but necessarily changes its full public address. When a VM is undefined, helper refreshes remove its registry entry, aliases, and original domain XML backup. Status and periodic refreshes reconcile against libvirt, including VMs removed through another tool. Do not copy or hand-edit this registry.

The host checks for existing-address collisions and waits for IPv6 duplicate-address detection before enabling a new mapping. Its firewall checks the VM's private address and dedicated MAC, applies that VM's explicit source translation, and blocks unmapped egress. Reserved public aliases are marked nonpreferred and blocked as sources for host-originated application traffic; required IPv6 neighbor-discovery messages remain allowed.

Inside the guest, the helper selects the dedicated adapter by MAC and installs an explicit IPv6 source/default route. The normal desktop user (UID 1000), including `browser`, cannot connect to Internet destinations over IPv4. IPv4 loopback and DNS on the default libvirt gateway remain allowed. Root/APT and other system users are excluded from this desktop-user guard, but their connectivity still depends on working guest IPv4 routing, DNS and host NAT/firewall rules. This is a desktop-traffic guard, not an all-process isolation boundary against a deliberately modified guest or a separately configured proxy.

If IPv6 is unavailable, or a redirect/download endpoint supports only IPv4, the browser connection fails instead of silently using the shared public IPv4 address. Finish guest setup before using Firefox, and launch the configured browser with `browser`, which refreshes the guest rules before starting it.

Different exact IPv6 addresses do not determine how a website will treat or correlate connections. All VMs still share the ISP prefix and connection, and an online service can use many signals beyond the source address. Cicada enforces address ownership and removes browser IPv4 fallback so that the selected IPv6 route is testable; the verification steps are in the README.

This routed/source-translated design works over Ethernet or Wi-Fi. It routes through whichever interface carries the host's default IPv6 route instead of bridging it, so it needs no bridge or router changes. It still depends on the router and ISP accepting multiple IPv6 addresses on the host's existing connection. See [libvirt virtual networking](https://libvirt.org/formatnetwork.html) and [the Wi-Fi bridging limitation](https://wiki.libvirt.org/Networking.html).

### Installed Host Components

The host step installs:

- `/usr/local/libexec/cicada-network.py`.
- `cicada-network.service` and `cicada-network.timer`, refreshing about every 30 seconds after the previous run.
- `/etc/sysctl.d/90-cicada-network.conf`, retaining router advertisements on the selected uplink while enabling IPv6 forwarding.
- The `cicada-ipv6` libvirt network, bridge `virbr-cicada6`, and dedicated `ip6 cicada_ipv6` nftables table.
- Registry and original domain-XML backups under `/var/lib/cicada-network`.

Only recorded aliases and the helper's own firewall table are updated. Unexpected alias properties stop the update instead of changing or removing that address. There is no NetworkManager dispatcher or alias-triggered restart loop. Uplink changes can briefly interrupt VM connectivity, and existing downloads do not survive an ISP prefix change automatically.

The forwarding/advertisement setting follows the [Linux kernel's `accept_ra=2` behavior](https://docs.kernel.org/networking/ip-sysctl.html).

### Legacy Network Cleanup

Hosts that never ran a pre-Cicada `antix-vm-ipv6` setup can skip this section.

The helper refuses to run alongside the legacy `antix-vm-ipv6.service`, its NetworkManager dispatcher/helper, or the legacy `ip6 antix_vm_snat` table. If it reports one, inspect and remove only that legacy installation and its tracked aliases before proceeding. It does not delete unknown networking automatically.

A remaining `cicada-ipv6` network can be reused only if its bridge, IPv6 gateway and routed configuration match. Copying new setup files alone never changes a running VM's adapters or boot options. Use the non-destructive upgrade below for existing VMs.

## Part 1: Prepare the Fedora Host

### Install the Virtualization Stack

Update Fedora, install the virtualization tools, start libvirt, and add the current user to the `libvirt` and `kvm` groups.

```bash
sudo dnf upgrade --refresh -y
sudo dnf install -y @virtualization virt-install virt-manager virt-viewer qemu-img
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm "$(id -un)"
```

Log out of Fedora and log back in after changing group membership.

### Validate the Virtualization Host

Confirm that QEMU/KVM passes its host checks and that the system libvirt connection is available.

```bash
sudo virt-host-validate qemu
sudo virsh -c qemu:///system list --all
```

### Install Default Network Support

Install the packages used by libvirt's default NAT network.

```bash
sudo dnf install -y \
  libvirt-daemon-config-network \
  libvirt-daemon-driver-network \
  dnsmasq
```

Restart libvirt after installing the network components.

```bash
sudo systemctl restart libvirtd
```

### Confirm the Default Network Definition

Verify that Fedora provides the default network definition.

```bash
ls -l /usr/share/libvirt/networks/default.xml
```

Define the network, enable it at boot, and start it.

```bash
sudo virsh -c qemu:///system net-define \
  /usr/share/libvirt/networks/default.xml

sudo virsh -c qemu:///system net-autostart default
sudo virsh -c qemu:///system net-start default
```

### Allow VM Traffic on Docker Hosts

Docker sets the host's IPv4 `FORWARD` policy to `drop`. libvirt accepts VM traffic in its own nftables table, but Docker's drop policy still discards it, so VMs reach DNS on the libvirt gateway but nothing beyond it. Guest setup then stops with `Unable to refresh either antiX repository`, and the guest log shows `connection timed out` for both mirrors. Podman does not change this policy.

Check the policy on hosts with Docker installed:

```bash
sudo nft list chain ip filter FORWARD
```

If it shows `policy drop`, stop Docker from setting it (Docker 28 or later), restart Docker, and reset the policy without rebooting:

```bash
sudo mkdir -p /etc/docker
echo '{ "ip-forward-no-drop": true }' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
sudo nft add chain ip filter FORWARD '{ policy accept; }'
```

If `/etc/docker/daemon.json` already exists, add the `"ip-forward-no-drop": true` key to it instead of overwriting the file. Containers keep working, and firewalld still filters forwarded traffic between its zones. Rerun `bash /mnt/shared/cicada-setup.sh --guest` in any VM whose guest setup stopped.

### Install File-Sharing and SELinux Tools

Install the dependencies the setup script uses to configure the virtiofs share, access control lists, and SELinux labels.

```bash
sudo dnf install -y virtiofsd acl policycoreutils-python-utils nftables iproute procps-ng python3
```

The setup script creates `~/Documents/Shared`, configures its permissions and SELinux context, keeps both maintained scripts there, and copies the ISO into libvirt storage when needed. No separate manual share setup is needed.

### Remove Remote Viewer

The setup uses Virtual Machine Manager. Remove the separate Remote Viewer application without removing its dependencies if it is not wanted.

```bash
sudo dnf remove --setopt=clean_requirements_on_remove=False virt-viewer
```

## Part 2: Create and Use a VM

### Choose the VM Name and Download Directory

Use 1 to 63 lowercase letters, digits, or hyphens. The name must begin and end with a letter or digit.

```bash
VM_NAME="cicada-vm1"
VM_DOWNLOAD="$HOME/Downloads/$VM_NAME"
```

`VM_DOWNLOAD` must be an absolute path. The path may be inside your home directory or elsewhere, such as on a second drive, and may contain spaces, but it must not be a symbolic link, contain commas, quotes, backslashes or control characters, or lie under `/boot`, `/dev`, `/etc`, `/proc`, `/run`, `/sys`, `/tmp`, `/usr` or `/var`. The directory, or the existing folder it will be created in, must be owned by your normal Fedora account, and no other filesystem, such as another drive or a btrfs subvolume, may be mounted inside it. Setup labels the whole directory tree for VM access and gives the guest write access to all of it, so prefer a dedicated folder for each VM rather than an existing folder that holds other files. The setup creates the final directory if it does not exist. Each VM may use a different directory; the guest sees its selected directory at `/mnt/downloads`. Firefox asks where to save each download, starting in `/mnt/downloads`, so you can rename the file before saving it. Only files saved under `/mnt/downloads` reach the host directory.

### Create or Replace the VM

Run the host script from a normal Fedora account. To create a new VM without overwriting an existing VM or private disk, use the selected `VM_NAME` and `VM_DOWNLOAD`:

```bash
bash "$HOME/Documents/Shared/cicada-setup.sh" "$VM_NAME" "$VM_DOWNLOAD"
```

To deliberately rebuild that same VM, add `--replace`. This deletes only the existing VM with the selected name and its recognized private disk before rebuilding it.

```bash
bash "$HOME/Documents/Shared/cicada-setup.sh" "$VM_NAME" "$VM_DOWNLOAD" --replace
```

The script opens Virtual Machine Manager after creating the VM.

### Complete the First-Boot Guest Setup

Open a terminal in the antiX live desktop. Do not run the antiX installer.

Create the mount point:

```bash
sudo mkdir -p /mnt/shared
```

Mount the host share:

```bash
mountpoint -q /mnt/shared || sudo mount -t virtiofs shared /mnt/shared
```

Run the guest setup copied into the shared folder:

```bash
bash /mnt/shared/cicada-setup.sh --guest
```

These are the only three commands required inside the guest before logout. The `--guest` command repairs the IPv4 maintenance route before APT, installs the required packages, and configures IPv6 automatically.
It also mounts the selected host download directory at `/mnt/downloads`; the persistent guest's `~/Downloads` path and Firefox download policy point there.

### Apply the Desktop Changes

Log out once after the guest setup completes.

```bash
desktop-session-exit --logout
```

Log back in, then open the configured Firefox profile.

```bash
browser
```

### Inspect VM Information

Display the guest identity, local time, network interfaces, and compressed swap.

```bash
vm-info
```

Open the local browser-visible details page.

```bash
browser file:///usr/local/share/cicada-info.html
```

### Start an Existing VM

Set the same VM name in a Fedora terminal.

```bash
VM_NAME="cicada-vm1"
```

Start the VM without rebuilding it.

```bash
sudo virsh -c qemu:///system start "$VM_NAME"
```

After opening its console and logging in, start the configured browser inside the guest.

```bash
browser
```

### Shut Down the VM

Shut down normally from inside the guest.

```bash
desktop-session-exit --shutdown
```

Closing the VM console does not stop the VM.

## Part 3: Purge a VM Without Replacement

Set the exact VM name to remove.

```bash
VM_NAME="cicada-vm1"
```

The following command destroys the selected VM if it is running, undefines it, and deletes only its recognized private disk files. It does not create a replacement. After undefining a VM, the network helper removes its IPv6 registry entry, public aliases, and original domain XML backup. Status and periodic refreshes also reconcile the registry with libvirt, so VMs removed through another tool are purged. Shared host networking and other VMs' assignments remain.

```bash
sudo bash -c '
set -euo pipefail
vm="$1"
[[ "$vm" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || exit 1
export LIBVIRT_DEFAULT_URI=qemu:///system
xml=$(virsh dumpxml "$vm" --inactive)
flags=(--managed-save --snapshots-metadata --checkpoints-metadata)
[[ "$xml" != *"<nvram"* ]] || flags+=(--nvram)
virsh destroy "$vm" 2>/dev/null || true
virsh undefine "$vm" "${flags[@]}"
rm -f -- "/var/lib/libvirt/images/${vm}-persistence.raw" "/var/lib/libvirt/images/${vm}.qcow2"
' _ "${VM_NAME:?Set VM_NAME first}"
```

Refresh the network helper now so the removed VM's network metadata is purged immediately:

```bash
sudo python3 "$HOME/Documents/Shared/cicada-network.py" refresh
```

### Purge All Cicada VMs

To remove every Cicada VM at once instead of repeating the steps above for each name, run the block below. It recognizes a Cicada VM by the `cicada_vm_uuid=` boot option that setup gives every VM, and leaves other libvirt VMs untouched. Each Cicada VM is destroyed if running, undefined, and its recognized private disk files deleted; the final refresh purges their network metadata. Download directories, `~/Documents/Shared`, the shared ISO and boot files, and the host networking components remain.

```bash
sudo bash -c '
set -euo pipefail
export LIBVIRT_DEFAULT_URI=qemu:///system
mapfile -t vms < <(virsh list --all --name)
for vm in "${vms[@]}"; do
    [[ "$vm" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || continue
    xml=$(virsh dumpxml "$vm" --inactive)
    [[ "$xml" == *"cicada_vm_uuid="* ]] || continue
    flags=(--managed-save --snapshots-metadata --checkpoints-metadata)
    [[ "$xml" != *"<nvram"* ]] || flags+=(--nvram)
    virsh destroy "$vm" 2>/dev/null || true
    virsh undefine "$vm" "${flags[@]}"
    rm -f -- "/var/lib/libvirt/images/${vm}-persistence.raw" "/var/lib/libvirt/images/${vm}.qcow2"
    echo "Purged $vm"
done
'
sudo python3 "$HOME/Documents/Shared/cicada-network.py" refresh
```
