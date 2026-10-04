# Cicada

Cicada creates reusable antiX Full virtual machines on a Fedora host with KVM and libvirt. Each VM boots from the same read-only antiX ISO while keeping its own persistent root filesystem, Firefox profile, UUID, MAC addresses, hostname, timezone, and mapped public IPv6 source address.

Cicada is not a general-purpose VM launcher. Its specific purpose is to provide a reproducible environment for testing per-VM native IPv6 source routing alongside independent browser state. An ordinary libvirt VM can provide separate virtual hardware and private addresses, but its default IPv4 NAT normally sends every guest through the same public IPv4 address and does not guarantee routed public IPv6.

For an unambiguous routing test, Cicada prevents the normal desktop user from falling back to the shared public IPv4 address; IPv4 remains available for DNS and system maintenance. Cicada does not create separate public IPv4 addresses. It also does not provide a VPN, anonymity, anti-fingerprinting, or any guarantee about how an online service will correlate sessions.

## What Cicada Builds

Each managed VM receives:

- 1 virtual CPU with host CPU passthrough.
- 2,048 MiB of memory and 1 GiB of in-memory zram swap.
- A private 1,000,000,000-byte raw persistence disk with an 850 MiB persistent root filesystem.
- A shared, read-only antiX Full ISO and shared extracted boot files.
- A writable virtiofs control share mounted at `/mnt/shared` from `~/Documents/Shared` on the host.
- A per-VM host download directory selected with `VM_DOWNLOAD` and mounted at `/mnt/downloads`.
- A normal libvirt NAT adapter for system maintenance and DNS.
- A dedicated IPv6 adapter with a stable private address and a reserved public `/128` mapping.
- A Firefox launcher named `browser` whose normal desktop user cannot fall back to public IPv4.

The guest remains an antiX live system. Do not run the antiX installer inside it.

## Network Model

```text
antiX VM
├── default adapter ─────────────────▶ Fedora libvirt IPv4 NAT ───────▶ maintenance and DNS
│   browser user's public IPv4 blocked
└── dedicated IPv6 adapter ──────────▶ virbr-cicada6 ─────────────────▶ public IPv6 /128
    fd71:6e9f:db42:1::/64              per-VM nftables translation
```

The uplink can be Ethernet or Wi-Fi. The helper follows whichever host interface carries the default IPv6 route and routes VM traffic through it instead of bridging it, so the usual Wi-Fi bridging limitation does not apply.

The network helper records assignments by VM UUID in `/var/lib/cicada-network/state.json`. A VM keeps its interface identifier across restarts, renames, and ISP prefix changes. If the ISP changes the routed prefix, the complete public address changes while that identifier remains stable. When a VM is undefined, helper refreshes remove its registry entry, public aliases, and saved domain XML; status and periodic refreshes reconcile against libvirt.

The browser guard rejects public IPv4 traffic from the normal desktop user, while preserving IPv4 loopback, DNS through the default libvirt gateway, and maintenance access for root and APT. IPv4-only sites and redirects therefore fail instead of silently sharing the host's public IPv4 address.

All VMs still share one physical uplink and ISP prefix. An online service may correlate sessions through the shared prefix, account, browser fingerprint, or another server-side signal. A distinct public `/128` confirms only the source address used for a connection.

## Requirements

- A Fedora x86_64 host with hardware virtualization enabled.
- Working KVM, QEMU, libvirt, nftables, and virtiofs support.
- Sudo access from the normal Fedora account that runs the setup script.
- Native IPv6 on the default uplink, Ethernet or Wi-Fi, with a usable global `/64` prefix.
- The antiX Full x64 ISO, renamed to `cicada.iso`. Cicada expects antiX Full because it is a lightweight OS that runs on the single vCPU each VM receives. The fixed file name keeps the scripts independent of antiX version numbers such as 26 or 26.1.
- The setup script and network helper kept together.

Cicada cannot obtain an additional ISP prefix, add IPv6 to an IPv4-only connection, or override router or ISP limits on multiple IPv6 addresses on the host connection.

Follow [GUIDE.md](GUIDE.md) to install and validate the Fedora virtualization stack before creating a VM.

## Quick Start

From the repository root, copy both maintained files into `~/Documents/Shared`:

```bash
install -D -m 0755 scripts/cicada-setup.sh "$HOME/Documents/Shared/cicada-setup.sh"
install -D -m 0644 scripts/cicada-network.py "$HOME/Documents/Shared/cicada-network.py"
```

Rename the downloaded antiX Full x64 ISO, whatever its version number, to `cicada.iso` in `~/Downloads`:

```bash
mv ~/Downloads/antiX-*_x64-full.iso ~/Downloads/cicada.iso
```

Choose a lowercase VM name and an absolute download directory, then create the VM from the normal Fedora account:

```bash
VM_NAME="cicada-vm1"
VM_DOWNLOAD="$HOME/Downloads/$VM_NAME"
bash "$HOME/Documents/Shared/cicada-setup.sh" "$VM_NAME" "$VM_DOWNLOAD"
```

The VM name must contain 1 to 63 lowercase letters, digits, or hyphens and must begin and end with a letter or digit.
The setup creates `VM_DOWNLOAD` if necessary and maps it to `/mnt/downloads` in that VM. The path may be inside your home directory or elsewhere, such as on a second drive, and may contain spaces, but it must not be a symbolic link, contain commas, quotes, backslashes or control characters, or lie under `/boot`, `/dev`, `/etc`, `/proc`, `/run`, `/sys`, `/tmp`, `/usr` or `/var`. The directory, or the existing folder it will be created in, must be owned by your normal Fedora account, and no other filesystem, such as another drive or a btrfs subvolume, may be mounted inside it. Setup labels the whole directory tree for VM access and gives the guest write access to all of it, so prefer a dedicated folder for each VM rather than an existing folder that holds other files. Firefox asks where to save each download, starting in `/mnt/downloads`, so you can rename the file before saving it. Only files saved under `/mnt/downloads` reach the host directory.

When the antiX live desktop opens, run these three commands in the guest:

```bash
sudo mkdir -p /mnt/shared
mountpoint -q /mnt/shared || sudo mount -t virtiofs shared /mnt/shared
bash /mnt/shared/cicada-setup.sh --guest
```

Guest setup writes its output to `~/Documents/Shared/<guest-hostname>-setup.log` on Fedora. After setup succeeds, log out once:

```bash
desktop-session-exit --logout
```

Log back in and launch the configured browser:

```bash
browser
```

## Common Operations

Run host-side commands from the normal Fedora account unless the command explicitly uses `sudo`.

| Context | Operation | Command |
| --- | --- | --- |
| Fedora | Create a VM | `bash "$HOME/Documents/Shared/cicada-setup.sh" "$VM_NAME" "$VM_DOWNLOAD"` |
| Fedora | Deliberately rebuild a VM | `bash "$HOME/Documents/Shared/cicada-setup.sh" "$VM_NAME" "$VM_DOWNLOAD" --replace` |
| Fedora | Add the current IPv6 design to a stopped managed VM | `bash "$HOME/Documents/Shared/cicada-setup.sh" --enable-ipv6 "$VM_NAME"` |
| Fedora | Show recorded IPv6 assignments | `bash "$HOME/Documents/Shared/cicada-setup.sh" --ipv6-status` |
| Fedora | Start an existing VM | `sudo virsh -c qemu:///system start "$VM_NAME"` |
| antiX guest | Show guest identity and network details | `vm-info` |
| antiX guest | Start the configured browser | `browser` |
| antiX guest | Shut down normally | `desktop-session-exit --shutdown` |

The `--replace` option deletes the selected VM and its recognized private disk before rebuilding it. It is not an upgrade command. To update an existing managed VM without losing its persistent disk, UUID, Firefox profile, or timezone, use the upgrade procedure in the guide.

Closing Virtual Machine Manager or its console does not stop a VM.

## Host Components

The setup installs and maintains:

- `/usr/local/libexec/cicada-network.py`.
- `cicada-network.service` and `cicada-network.timer`.
- `/etc/sysctl.d/90-cicada-network.conf`.
- The `cicada-ipv6` libvirt network on `virbr-cicada6`.
- The dedicated `ip6 cicada_ipv6` nftables table.
- Registry data and original domain XML backups under `/var/lib/cicada-network`; a VM's backup is removed when the VM is undefined.

Do not copy or hand-edit the registry. The helper validates its managed aliases, detects local address collisions, waits for IPv6 duplicate-address detection, and refuses incompatible remnants of the legacy `antix-vm-ipv6` setup described in the guide.

## Verification and Limits

Use the local status report to inspect assignments, then compare each guest browser's `ip=` value at `https://www.cloudflare.com/cdn-cgi/trace` with its assigned address. With two or more VMs running, each should report its own IPv6 address. That check confirms the source address used for the Cloudflare request only; it cannot prove what another website, API, CDN, or download endpoint observes, or how it will interpret or correlate those connections.

The desktop IPv4 rule is a traffic guard, not a security boundary against a deliberately modified guest, root activity, or a separately configured proxy. Review the threat model and operational caveats in the guide before relying on it.

## Repository Layout

```text
.
├── README.md
├── GUIDE.md
└── scripts
    ├── cicada-setup.sh
    └── cicada-network.py
```

- [GUIDE.md](GUIDE.md) is the complete host preparation, VM creation, guest setup, upgrade, diagnostics, and removal procedure.
- [`scripts/cicada-setup.sh`](scripts/cicada-setup.sh) orchestrates host provisioning, VM lifecycle setup, shared storage, persistence, and guest bootstrap.
- [`scripts/cicada-network.py`](scripts/cicada-network.py) manages per-VM addressing, host firewall rules, source translation, state, and guest network guards.
