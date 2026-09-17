#!/usr/bin/env bash

set -euo pipefail

die() {

    printf "STOP: %s\n" "$*" >&2
    exit 1
}

valid_name() {

    [[ ${#1} -le 63 && "$1" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
}

guest_setup_log() {

    [[ "${ANTIX_GUEST_LOG_STARTED:-no}" == "yes" ]] && return
    [[ -w /mnt/shared ]] || die "The mounted shared folder is not writable."
    local guest_name
    guest_name=$(hostname)
    valid_name "$guest_name" || guest_name="antix-guest"
    local log_path="/mnt/shared/${guest_name}-setup.log"
    [[ ! -L "$log_path" ]] || die "Refusing a symlink as the setup log."
    [[ ! -e "$log_path" || ( -f "$log_path" && -w "$log_path" ) ]] ||
        die "The setup log is not a writable regular file."
    exec > >(tee -a -- "$log_path") 2>&1
    ANTIX_GUEST_LOG_STARTED="yes"
    printf "\nSetup output is saved to %s\n" "$log_path"
}

guest_ipv4_setup() {

    local network_helper
    network_helper="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/antix-vm-network.py"
    [[ -f "$network_helper" ]] || die "The companion antix-vm-network.py file is missing."
    sudo python3 "$network_helper" guest-ipv4
}

guest_ipv6_setup() {

    [[ $(id -u) -eq 1000 ]] || die "Run the guest IPv6 command as the normal demo user."
    ! pgrep -u "$(id -u)" -x "firefox|firefox-esr" >/dev/null ||
        die "Close Firefox before changing the guest network."
    [[ " $(cat /proc/cmdline) " == *" antix_ipv6_version=1 "* ]] ||
        die "Enable IPv6 for this VM on Fedora, then shut down and start the VM."

    local network_helper
    network_helper="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/antix-vm-network.py"
    [[ -f "$network_helper" ]] || die "The companion antix-vm-network.py file is missing."

    guest_setup_log
    guest_ipv4_setup

    if ! sudo sh -c "command -v nft >/dev/null"; then
        sudo apt-get update \
            -o "Acquire::ForceIPv4=true" \
            -o "APT::Update::Error-Mode=any"
        sudo apt-get install \
            -o "Acquire::ForceIPv4=true" \
            -y \
            --no-install-recommends \
            nftables
    fi

    sudo python3 "$network_helper" install-guest
}

host_ipv6_command() {

    [[ $(id -u) -ne 0 ]] || die "Run the host command as your normal Fedora user."
    local network_helper
    network_helper="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/antix-vm-network.py"
    [[ -f "$network_helper" ]] || die "The companion antix-vm-network.py file is missing."
    sudo python3 "$network_helper" "$@"
}

guest_setup() {

    [[ $(id -u) -ne 0 ]] || die 'Run the guest command WITHOUT sudo before bash.'
    [[ -n ${DISPLAY:-} ]] || die 'Run this from a terminal in the antiX desktop.'
    [[ $(id -u) -eq 1000 ]] || die 'This setup expects the standard antiX demo user (UID 1000).'
    ! pgrep -u "$(id -u)" -x 'firefox|firefox-esr' >/dev/null \
        || die 'Close Firefox before running the guest setup.'
    if [[ " $(cat /proc/cmdline) " != *" antix_ipv6_version=1 "* ]]; then
        die "Enable IPv6 for this VM on Fedora, then shut down and start it before guest setup."
    fi
    sudo -v
    guest_setup_log
    guest_ipv4_setup

    sudo python3 - <<'PY'
import json
import re
import shlex
import uuid
from pathlib import Path

args = dict(
    token.split("=", 1)
    for token in shlex.split(Path("/proc/cmdline").read_text())
    if "=" in token
)
name = args.get("hostname", "")
zone = args.get("tz", "")

if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name):
    raise SystemExit("STOP: valid VM hostname missing from the boot configuration.")
if not re.fullmatch(r"Europe/[A-Za-z_]+", zone):
    raise SystemExit("STOP: European timezone missing from the boot configuration.")
zonefile = Path("/usr/share/zoneinfo") / zone
if not zonefile.is_file():
    raise SystemExit(f"STOP: the guest does not have timezone {zone}.")
localtime = Path("/etc/localtime")
localtime.unlink(missing_ok=True)
localtime.symlink_to(zonefile)
Path("/etc/timezone").write_text(zone + "\n")
Path("/etc/hostname").write_text(name + "\n")
hosts = Path("/etc/hosts")
lines = hosts.read_text().splitlines()
lines = [line for line in lines if not re.match(r"^\s*127\.0\.1\.1\s", line)]
hosts.write_text("\n".join(lines) + f"\n127.0.1.1\t{name}\n")
vm_uuid = args.get("antix_vm_uuid")
identity = {
    "name": name,
    "timezone": zone,
    "uuid": (
        str(uuid.UUID(vm_uuid))
        if vm_uuid
        else "not explicitly assigned (older host setup)"
    ),
}
Path("/etc/antix-vm-identity.json").write_text(
    json.dumps(identity, indent=2) + "\n"
)
PY
    sudo hostname "$(cat /etc/hostname)"

    sudo install -d -m 0755 /usr/local/sbin /usr/local/bin
    sudo tee /usr/local/sbin/antix-vm-runtime >/dev/null <<'RUNTIME'
#!/bin/sh
set -eu
exec 9>/run/antix-vm-runtime.lock
flock -x 9
if [ -f /usr/local/libexec/antix-vm-network.py ]; then
    /usr/bin/python3 /usr/local/libexec/antix-vm-network.py guest
fi

if ! swapon --show=NAME --noheadings | grep -q '/dev/zram'; then
    modprobe zram
    device=$(zramctl --find --size 1G)
    mkswap "$device"
    swapon --priority 100 "$device"
fi

if ! mountpoint -q /mnt/shared; then
    mkdir -p /mnt/shared
    chown root:root /mnt/shared
    chmod 000 /mnt/shared
    mount -t virtiofs -o nosuid,nodev,noexec shared /mnt/shared
fi
test "$(findmnt -n -o FSTYPE --target /mnt/shared)" = virtiofs

if ! mountpoint -q /mnt/downloads; then
    if [ -L /mnt/downloads ]; then
        rm -f /mnt/downloads
    fi
    mkdir -p /mnt/downloads
    chown root:root /mnt/downloads
    chmod 000 /mnt/downloads
    if grep -qw 'antix_downloads_version=1' /proc/cmdline; then
        mount -t virtiofs -o nosuid,nodev,noexec downloads /mnt/downloads
    elif ! mount -t virtiofs -o nosuid,nodev,noexec downloads /mnt/downloads; then
        rmdir /mnt/downloads
        ln -s /mnt/shared /mnt/downloads
    fi
fi
test -w /mnt/downloads

if [ -x /etc/init.d/spice-vdagent ]; then
    modprobe uinput
    if ! pgrep -x spice-vdagentd >/dev/null; then
        /etc/init.d/spice-vdagent start
    fi
fi

RUNTIME
    sudo chmod 0755 /usr/local/sbin/antix-vm-runtime

    sudo /usr/local/sbin/antix-vm-runtime
    [[ -w /mnt/downloads ]] || die 'The selected host download folder is not writable.'

    local antix_sources_path="/etc/apt/sources.list.d/antix.list"
    local antix_repository_url
    local antix_repository_urls=(
        "https://ftp.fau.de/mxlinux-packages/antix/trixie/"
        "https://fosszone.csd.auth.gr/mxlinux-archive/antix/trixie/"
    )
    local antix_repository_refresh_succeeded="no"
    local spice_vdagent_version=""

    for antix_repository_url in "${antix_repository_urls[@]}"; do
        printf "deb %s trixie main nosystemd nonfree\n" "$antix_repository_url" |
            sudo tee "$antix_sources_path" >/dev/null

        if ! sudo apt-get update \
            -o "Acquire::ForceIPv4=true" \
            -o "APT::Update::Error-Mode=any" \
            -o "Dir::Etc::sourcelist=$antix_sources_path" \
            -o "Dir::Etc::sourceparts=-" \
            -o "APT::Get::List-Cleanup=0"; then
            continue
        fi
        antix_repository_refresh_succeeded="yes"

        spice_vdagent_version=$(
            apt-cache madison spice-vdagent |
                awk "\$3 ~ /nosystemd/ { print \$3; exit }"
        )
        [[ -z "$spice_vdagent_version" ]] || break
    done

    if [[ -z "$spice_vdagent_version" ]]; then
        if [[ "$antix_repository_refresh_succeeded" == "no" ]]; then
            die "Unable to refresh either antiX repository; package availability was not checked."
        fi

        die "The antiX nosystemd build of spice-vdagent is absent from the refreshed repositories."
    fi

    sudo apt-get update \
        -o "Acquire::ForceIPv4=true" \
        -o "APT::Update::Error-Mode=any"

    sudo apt-get install \
        -o "Acquire::ForceIPv4=true" \
        -y \
        --no-install-recommends \
        "spice-vdagent=$spice_vdagent_version" \
        gnome-themes-extra \
        nftables
    sudo apt-get clean

    printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/antix-vm-runtime\n' "$(id -un)" |
        sudo tee /etc/sudoers.d/antix-vm-runtime >/dev/null
    sudo chmod 0440 /etc/sudoers.d/antix-vm-runtime
    sudo visudo -cf /etc/sudoers.d/antix-vm-runtime

    sudo modprobe uinput
    sudo /etc/init.d/spice-vdagent restart

    sudo tee /usr/local/bin/antix-vm-session >/dev/null <<'SESSION'
#!/bin/sh
set -eu
sudo -n /usr/local/sbin/antix-vm-runtime
if ! pgrep -u "$(id -u)" -x spice-vdagent >/dev/null; then
    spice-vdagent
fi
SESSION
    sudo chmod 0755 /usr/local/bin/antix-vm-session

    mkdir -p "$HOME/.desktop-session" "$HOME/.config/autostart"
    local conf="$HOME/.desktop-session/desktop-session.conf"
    if [[ ! -f "$conf" ]]; then
        if [[ -f /etc/desktop-session/desktop-session.conf ]]; then
            cp /etc/desktop-session/desktop-session.conf "$conf"
        else
            touch "$conf"
        fi
    fi
    sed -i '/^[[:space:]]*LOAD_XDG_AUTOSTART=/d' "$conf"
    printf '\nLOAD_XDG_AUTOSTART="true"\n' >> "$conf"
    cat > "$HOME/.config/autostart/antix-vm-session.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=VM clipboard, downloads and compressed swap
Exec=/usr/local/bin/antix-vm-session
Terminal=false
DESKTOP

    if [[ -d "$HOME/Downloads" && ! -L "$HOME/Downloads" ]]; then
        rmdir "$HOME/Downloads" || die 'Downloads is nonempty; move its contents to /mnt/downloads first.'
    fi
    ln -sfnT /mnt/downloads "$HOME/Downloads"
    xdg-user-dirs-update --set DOWNLOAD /mnt/downloads

    sudo python3 - <<'PY'
import json
from pathlib import Path
p = Path('/etc/firefox/policies/policies.json')
p.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(p.read_text()) if p.exists() else {}
pol = data.setdefault('policies', {})
pol.update({
    'DownloadDirectory': '/mnt/downloads',
    'PromptForDownloadLocation': False,
    'StartDownloadsInTempDirectory': False,
    'HardwareAcceleration': False,
    'OverrideFirstRunPage': 'about:blank',
    'OverridePostUpdatePage': 'about:blank',
})
pol.setdefault('ExtensionSettings', {})['uBlock0@raymondhill.net'] = {
    'installation_mode': 'force_installed',
    'install_url': 'https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi',
}
prefs = pol.setdefault('Preferences', {})
for key, value in {
    'browser.cache.disk.enable': False,
    'ui.systemUsesDarkTheme': 1,
    'layout.css.prefers-color-scheme.content-override': 0,
}.items():
    prefs[key] = {'Value': value, 'Status': 'locked'}
p.write_text(json.dumps(data, indent=2) + '\n')
PY

    python3 - <<'PY'
import configparser
from pathlib import Path
home = Path.home()
for version in ('gtk-3.0', 'gtk-4.0'):
    p = home / '.config' / version / 'settings.ini'
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    if p.exists():
        cfg.read(p)
    if not cfg.has_section('Settings'):
        cfg.add_section('Settings')
    cfg.set('Settings', 'gtk-theme-name', 'Adwaita-dark')
    cfg.set('Settings', 'gtk-application-prefer-dark-theme', 'true')
    with p.open('w') as f:
        cfg.write(f)
p = home / '.gtkrc-2.0'
old = p.read_text() if p.exists() else ''
lines = [line for line in old.splitlines() if not line.lstrip().startswith('gtk-theme-name')]
p.write_text('\n'.join(lines) + '\ngtk-theme-name="Adwaita-dark"\n')
PY

    local ice_dir="${ICEWM_PRIVCFG:-${XDG_CONFIG_HOME:-$HOME/.config}/icewm}"
    mkdir -p "$ice_dir/themes/VM-Dark"
    cat > "$ice_dir/themes/VM-Dark/default.theme" <<'THEME'
ThemeDescription="VM Dark"
Look="nice"
ColorDialog="#242424"
ColorNormalBorder="#303030"
ColorActiveBorder="#505050"
ColorNormalButton="#303030"
ColorNormalButtonText="#eeeeee"
ColorActiveButton="#454545"
ColorActiveButtonText="#ffffff"
ColorNormalTitleButton="#303030"
ColorNormalTitleButtonText="#eeeeee"
ColorNormalTitleBar="#252525"
ColorNormalTitleBarText="#dddddd"
ColorActiveTitleBar="#383838"
ColorActiveTitleBarText="#ffffff"
ColorNormalMenu="#242424"
ColorNormalMenuItemText="#eeeeee"
ColorActiveMenuItem="#454545"
ColorActiveMenuItemText="#ffffff"
ColorDisabledMenuItemText="#999999"
ColorDefaultTaskBar="#242424"
ColorNormalTaskBarApp="#303030"
ColorNormalTaskBarAppText="#eeeeee"
ColorActiveTaskBarApp="#454545"
ColorActiveTaskBarAppText="#ffffff"
ColorMinimizedTaskBarApp="#242424"
ColorMinimizedTaskBarAppText="#bbbbbb"
ColorInvisibleTaskBarApp="#242424"
ColorInvisibleTaskBarAppText="#bbbbbb"
ColorInput="#242424"
ColorInputText="#eeeeee"
ColorLabel="#242424"
ColorLabelText="#eeeeee"
ColorListBox="#242424"
ColorListBoxText="#eeeeee"
ColorQuickSwitch="#242424"
ColorQuickSwitchText="#eeeeee"
ColorToolTip="#303030"
ColorToolTipText="#eeeeee"
THEME
    printf 'Theme="VM-Dark/default.theme"\n' > "$ice_dir/theme"

    sudo tee /usr/local/bin/browser >/dev/null <<'BROWSER'
#!/bin/sh
set -eu
test "$(id -u)" -eq 1000 || {
    echo "Run browser as the normal demo user." >&2
    exit 1
}
/usr/local/bin/antix-vm-session
test -w /mnt/downloads || {
    echo 'Host download folder unavailable; Firefox was not started.' >&2
    exit 1
}
export GTK_THEME=Adwaita:dark
export TZ="$(cat /etc/timezone)"
exec firefox-esr "$@"
BROWSER
    sudo chmod 0755 /usr/local/bin/browser
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/browser.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Firefox
Exec=/usr/local/bin/browser %u
Icon=firefox-esr
Terminal=false
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
DESKTOP
    xdg-mime default browser.desktop x-scheme-handler/http
    xdg-mime default browser.desktop x-scheme-handler/https

    sudo tee /usr/local/bin/vm-info >/dev/null <<'INFO'
#!/bin/sh
set -eu
cat /etc/antix-vm-identity.json
printf '\nCurrent local time: '
date '+%Y-%m-%d %H:%M:%S %Z %z'
printf '\nGuest interfaces (MAC addresses stay on the local network):\n'
ip -brief link
printf "\nGuest IPv6 addresses and routes:\n"
ip -brief -6 address
ip -6 route show default
printf '\nCompressed swap:\n'
swapon --show
INFO
    sudo chmod 0755 /usr/local/bin/vm-info
    sudo install -d -m 0755 /usr/local/share
    sudo tee /usr/local/share/antix-vm-info.html >/dev/null <<'HTML'
<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>VM browser details</title>
<style>html{color-scheme:dark;font:16px system-ui;background:#202020;color:#eee}body{max-width:900px;margin:40px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td{border-bottom:1px solid #555;padding:12px;overflow-wrap:anywhere}td:first-child{width:30%;color:#bbb}p{line-height:1.5}</style>
<h1>Browser-visible details</h1>
<p>This local page makes no network requests. Compare its values between VMs. Different timezone names can have the same UTC offset. Browser privacy settings can mask these values.</p>
<table id="details"></table>
<p>Your public IP, site cookies, server-side account limits and the site's decision are not measured here. A normal website cannot directly read your VM UUID or network-card MAC.</p>
<script>
function show() {
  const rows = [
    ['Timezone', Intl.DateTimeFormat().resolvedOptions().timeZone],
    ['Current UTC offset (minutes east)', -new Date().getTimezoneOffset()],
    ['Local date/time', new Date().toString()],
    ['Browser language', navigator.language],
    ['Language preferences', navigator.languages.join(', ')],
    ['User agent', navigator.userAgent],
    ['Reported logical CPUs', navigator.hardwareConcurrency],
    ['Screen', screen.width + ' × ' + screen.height],
    ['Viewport', innerWidth + ' × ' + innerHeight],
    ['Device pixel ratio', devicePixelRatio],
    ['Dark preference', matchMedia('(prefers-color-scheme: dark)').matches]
  ];
  const table = document.getElementById('details');
  table.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (const value of row) { const td = document.createElement('td'); td.textContent = value; tr.append(td); }
    table.append(tr);
  }
}
show(); addEventListener('resize', show);
</script></html>
HTML
    guest_ipv6_setup
    antix-vm-session
    touch /mnt/shared/VM-share-test.txt
    sync
    printf '\nGuest setup complete. Check these results:\n'
    swapon --show
    findmnt /mnt/shared
    findmnt /mnt/downloads
    free -h
    vm-info
    printf '\nLog out once: desktop-session-exit --logout\n'
    printf 'After logging back in, run: browser\n'
    printf 'Wait for uBlock to install; check about:policies and about:addons.\n'
    printf 'Compare browsers: browser file:///usr/local/share/antix-vm-info.html\n'
    printf 'Do NOT launch the antiX installer.\n'
}

cleanup_host_setup() {

    local work="${ANTIX_SETUP_WORK:-}"

    if [[ -z "$work" || "$work" != /var/tmp/antix-vm-setup.* || ! -d "$work" ]]; then
        return
    fi

    if mountpoint -q "$work/disk"; then
        umount "$work/disk"
    fi
    if mountpoint -q "$work/iso"; then
        umount "$work/iso"
    fi

    rm -f -- "$work/domain.xml"
    rmdir "$work/disk" "$work/iso" "$work" 2>/dev/null || true
}

host_setup() {

    [[ $(id -u) -eq 0 ]] || die 'Internal host setup needs sudo.'
    [[ $# -eq 6 ]] || die 'Invalid internal host arguments.'
    local vm="$1" host_home="$2" owner_uid="$3" owner_gid="$4" replace="$5" download="$6"
    valid_name "$vm" || die 'Use a VM name of 1-63 lowercase letters, digits and hyphens.'
    [[ "$host_home" == /home/* && -d "$host_home" ]] || die 'Expected the normal Fedora /home/... directory.'
    [[ "$owner_uid" =~ ^[0-9]+$ && "$owner_gid" =~ ^[0-9]+$ ]] || die 'Invalid host account.'
    [[ "$replace" == yes || "$replace" == no ]] || die 'Invalid replacement option.'
    [[ "$download" == "$host_home/"* ]] || die 'VM_DOWNLOAD must resolve inside the normal user home directory.'
    [[ ! -L "$download" ]] || die 'VM_DOWNLOAD must not be a symbolic link.'
    if [[ -e /usr/local/sbin/antix-vm-ipv6 ||
          -e /etc/systemd/system/antix-vm-ipv6.service ||
          -e /etc/NetworkManager/dispatcher.d/90-antix-vm-ipv6 ]]; then
        die "Remove the installed IPv6 patch first; see the network cleanup section in ANTIX-VM-GUIDE.md."
    fi
    export LC_ALL=C LIBVIRT_DEFAULT_URI=qemu:///system

    exec 8>/run/lock/antix-vm-setup.lock
    flock -x 8

    local iso=/var/lib/libvirt/images/antiX-26_x64-full.iso
    local base=/var/lib/libvirt/images/antix-26-shared-boot
    local disk="/var/lib/libvirt/images/$vm-persistence.raw"
    local legacy="/var/lib/libvirt/images/$vm.qcow2"
    local share="$host_home/Documents/Shared"
    local self
    self=$(readlink -f "${BASH_SOURCE[0]}")
    local network_helper="$(dirname "$self")/antix-vm-network.py"
    [[ -f "$network_helper" ]] || die "The companion antix-vm-network.py file is missing."

    local required_commands=(
        virsh
        virt-install
        systemctl
        mkfs.ext4
        setfacl
        semanage
        restorecon
        python3
        nft
        ip
        sysctl
    )
    local required_command
    for required_command in "${required_commands[@]}"; do
        command -v "$required_command" >/dev/null ||
            die "Missing Fedora dependency: $required_command"
    done
    systemctl enable --now virtqemud.socket virtnetworkd.socket
    virsh list --all >/dev/null
    python3 "$network_helper" preflight
    if [[ ! -f "$iso" ]]; then
        [[ -f "$host_home/Downloads/antiX-26_x64-full.iso" ]] \
            || die 'The antiX-26_x64-full.iso file is missing from both expected locations.'
        install -m 0644 "$host_home/Downloads/antiX-26_x64-full.iso" "$iso"
    fi
    if ! virsh net-info default >/dev/null 2>&1; then
        [[ -f /usr/share/libvirt/networks/default.xml ]] \
            || die 'Install Fedora package libvirt-daemon-config-network, then rerun.'
        virsh net-define /usr/share/libvirt/networks/default.xml
    fi
    virsh net-autostart default
    if ! virsh net-list --name | grep -qx default; then
        virsh net-start default
    fi

    for file in "$disk" "$legacy"; do
        [[ ! -L "$file" ]] || die "Refusing a symlink as a private disk: $file"
        [[ ! -e "$file" || -f "$file" ]] || die "Not a regular private disk: $file"
    done
    local other
    while IFS= read -r other; do
        [[ -n "$other" && "$other" != "$vm" ]] || continue
        if virsh domblklist "$other" --details |
            awk -v a="$disk" -v b="$legacy" '$4 == a || $4 == b {found=1} END {exit !found}'; then
            die "A private disk is also attached to $other; no VM was removed."
        fi
    done < <(virsh list --all --name)

    local exists=no
    if virsh dominfo "$vm" >/dev/null 2>&1; then exists=yes; fi
    if [[ "$replace" != yes && ( "$exists" == yes || -e "$disk" || -e "$legacy" ) ]]; then
        die 'The VM or private disk exists. Use --replace only when you intend to purge that VM.'
    fi

    local identity
    local selected_tz
    local vm_uuid
    local vm_mac
    identity=$(python3 - <<'PY'
import datetime
import secrets
import shlex
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

def virsh(*arguments: str) -> str:

    return subprocess.check_output(["virsh", *arguments], text=True)

now = datetime.datetime.now(datetime.timezone.utc)
pool = [
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Dublin",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Brussels",
    "Europe/Vienna",
    "Europe/Prague",
    "Europe/Warsaw",
    "Europe/Zurich",
    "Europe/Stockholm",
    "Europe/Oslo",
    "Europe/Copenhagen",
    "Europe/Budapest",
    "Europe/Belgrade",
    "Europe/Athens",
    "Europe/Helsinki",
    "Europe/Bucharest",
    "Europe/Sofia",
    "Europe/Riga",
    "Europe/Tallinn",
    "Europe/Vilnius",
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Minsk",
    "Europe/Samara",
    "Europe/Saratov",
    "Europe/Astrakhan",
    "Europe/Ulyanovsk",
]
pool = [zone for zone in pool if (Path("/usr/share/zoneinfo") / zone).is_file()]
used_zones = set()
used_offsets = set()
used_macs = set()
used_uuids = set()

for name in virsh("list", "--all", "--name").splitlines():
    if not name.strip():
        continue

    root = ET.fromstring(virsh("dumpxml", name, "--inactive"))
    used_uuids.add(root.findtext("uuid", "").lower())
    for mac_element in root.findall("./devices/interface/mac"):
        used_macs.add(mac_element.get("address", "").lower())

    for token in shlex.split(root.findtext("./os/cmdline", "")):
        if token.startswith("tz="):
            zone = token[3:]
            used_zones.add(zone)
            try:
                used_offsets.add(now.astimezone(ZoneInfo(zone)).utcoffset())
            except (KeyError, ValueError):
                pass

available = [zone for zone in pool if zone not in used_zones]
if not available:
    raise SystemExit(
        "STOP: every city in the European timezone pool is already assigned; "
        "no VM was removed."
    )
different_offset = [
    zone
    for zone in available
    if now.astimezone(ZoneInfo(zone)).utcoffset() not in used_offsets
]
zone = secrets.choice(different_offset or available)

while True:
    vm_uuid = str(uuid.uuid4())
    if vm_uuid not in used_uuids:
        break

while True:
    vm_mac = "52:54:00:" + ":".join(
        f"{byte:02x}" for byte in secrets.token_bytes(3)
    )
    if vm_mac not in used_macs:
        break

print(
    zone,
    vm_uuid,
    vm_mac,
)
PY
    )
    read -r \
        selected_tz \
        vm_uuid \
        vm_mac <<< "$identity"
    printf "\nAssigned timezone: %s\n" "$selected_tz"
    printf "VM UUID: %s\n" "$vm_uuid"
    printf "NIC MAC: %s\n" "$vm_mac"

    local work
    work=$(mktemp -d /var/tmp/antix-vm-setup.XXXXXX)
    export ANTIX_SETUP_WORK="$work"
    trap cleanup_host_setup EXIT
    mkdir "$work/iso" "$work/disk"

    mkdir -p "$base"
    if [[ ! -f "$base/vmlinuz" || ! -f "$base/initrd.gz" ]]; then
        mount -o loop,ro "$iso" "$work/iso"
        install -m 0644 "$work/iso/antiX/vmlinuz" "$base/vmlinuz"
        install -m 0644 "$work/iso/antiX/initrd.gz" "$base/initrd.gz"
        umount "$work/iso"
    fi

    if [[ "$exists" == yes ]]; then
        virsh dumpxml "$vm" --inactive > "$work/domain.xml"
        python3 - "$work/domain.xml" "$disk" "$legacy" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
allowed = set(sys.argv[2:])
for disk in root.findall('./devices/disk'):
    if disk.get('device') != 'disk':
        continue
    source = disk.find('source')
    if source is None or source.get('file') not in allowed:
        raise SystemExit('STOP: selected VM has an unexpected attached disk; nothing was deleted.')
PY
        printf '\nReplacing %s: private settings/profiles will be erased.\n' "$vm"
        if virsh list --name | grep -Fxq "$vm"; then
            virsh shutdown "$vm" || true
            for attempt in {1..15}; do
                if ! virsh list --name | grep -Fxq "$vm"; then break; fi
                sleep 2
            done
            if virsh list --name | grep -Fxq "$vm"; then
                printf 'Guest did not shut down; forcing power off for the requested purge.\n'
                virsh destroy "$vm"
            fi
        fi
        local flags=(--managed-save --snapshots-metadata --checkpoints-metadata)
        if python3 - "$work/domain.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
sys.exit(0 if ET.parse(sys.argv[1]).find('./os/nvram') is not None else 1)
PY
        then flags+=(--nvram); fi
        virsh undefine "$vm" "${flags[@]}"
    fi
    if [[ "$replace" == yes ]]; then
        rm -f -- "$disk" "$legacy"
    fi

    if [[ ! -d "$host_home/Documents" ]]; then
        install -d -m 0755 -o "$owner_uid" -g "$owner_gid" "$host_home/Documents"
    fi
    if [[ ! -d "$share" ]]; then
        install -d -m 0755 -o "$owner_uid" -g "$owner_gid" "$share"
    fi
    if [[ ! -d "$download" ]]; then
        install -d -m 0755 -o "$owner_uid" -g "$owner_gid" "$download"
    fi
    [[ -d "$download" && ! -L "$download" ]] || die 'VM_DOWNLOAD is not a usable directory.'
    setfacl -m "u:1000:rwx,d:u:1000:rwx,u:$owner_uid:rwx,d:u:$owner_uid:rwx" "$share"
    setfacl -m "u:1000:rwx,d:u:1000:rwx,u:$owner_uid:rwx,d:u:$owner_uid:rwx,u:qemu:--x" "$download"
    setfacl -m u:qemu:--x "$host_home" "$host_home/Documents"
    local download_parent="$download"
    while [[ "$download_parent" != "$host_home" ]]; do
        download_parent=$(dirname "$download_parent")
        [[ "$download_parent" == "$host_home" || "$download_parent" == "$host_home/"* ]] ||
            die 'VM_DOWNLOAD escaped the normal user home directory.'
        setfacl -m u:qemu:--x "$download_parent"
    done
    semanage fcontext -m -t svirt_image_t "$share(/.*)?" 2>/dev/null \
        || semanage fcontext -a -t svirt_image_t "$share(/.*)?"
    local download_context
    download_context=$(python3 -c 'import re, sys; print(re.escape(sys.argv[1]) + r"(/.*)?")' "$download")
    semanage fcontext -m -t svirt_image_t "$download_context" 2>/dev/null \
        || semanage fcontext -a -t svirt_image_t "$download_context"
    local guest_copy
    guest_copy=$(mktemp "$share/.antix-setup.XXXXXX")
    install -m 0644 -o "$owner_uid" -g "$owner_gid" "$self" "$guest_copy"
    mv -fT "$guest_copy" "$share/antix-setup.sh"
    restorecon -R "$share"
    restorecon -R "$download"

    truncate -s 1000000000 "$disk"
    mkfs.ext4 -q -F -m 0 -L antiX-Persist "$disk"
    mount -o loop "$disk" "$work/disk"
    mkdir "$work/disk/antiX"
    truncate -s 850M "$work/disk/antiX/rootfs"
    mkfs.ext4 -q -F -m 0 "$work/disk/antiX/rootfs"
    umount "$work/disk"
    chown qemu:qemu "$disk"
    chmod 0600 "$disk"
    restorecon "$iso" "$disk"
    restorecon -R "$base"

    local kernel_arguments=(
        "from=cd"
        "pdev=vda"
        "pdir=antiX"
        "p_static_root"
        "tz=$selected_tz"
        "hostname=$vm"
        "antix_vm_uuid=$vm_uuid"
        "antix_downloads_version=1"
    )
    local kernel_argument_text="${kernel_arguments[*]}"

    virt-install \
        --connect qemu:///system \
        --name "$vm" --uuid "$vm_uuid" \
        --virt-type kvm --arch x86_64 --machine q35 \
        --cpu host-passthrough --vcpus 2 --memory 2048 \
        --memorybacking source.type=memfd,access.mode=shared \
        --disk "path=$disk,format=raw,bus=virtio" \
        --disk "path=$iso,device=cdrom,readonly=on" \
        --filesystem "source=$share,target=shared,driver.type=virtiofs,accessmode=passthrough" \
        --filesystem "source=$download,target=downloads,driver.type=virtiofs,accessmode=passthrough" \
        --network "network=default,model=virtio,mac=$vm_mac" \
        --graphics spice,listen=127.0.0.1 --video virtio \
        --input tablet,bus=usb \
        --channel spicevmc,target.name=com.redhat.spice.0 \
        --osinfo detect=on,require=off \
        --boot "kernel=$base/vmlinuz,initrd=$base/initrd.gz,kernel_args=$kernel_argument_text" \
        --import --noautoconsole --print-xml > "$work/domain.xml"

    virsh define "$work/domain.xml"
    python3 "$network_helper" enable "$vm"
    virsh start "$vm"

    printf '\nCreated %s. Private disk bytes: ' "$vm"
    stat -c %s "$disk"
    printf \
        "Timezone: %s\nUUID: %s\nMAC: %s\nDownload directory: %s\n" \
        "$selected_tz" \
        "$vm_uuid" \
        "$vm_mac" \
        "$download"
    printf '\nInside the guest (not the installer), type:\n'
    printf 'sudo mkdir -p /mnt/shared\n'
    printf 'mountpoint -q /mnt/shared || sudo mount -t virtiofs shared /mnt/shared\n'
    printf 'bash /mnt/shared/antix-setup.sh --guest\n'
}

case "${1:-}" in
    --enable-ipv6)
        [[ $# -eq 2 ]] || die "Usage: bash antix-vm-setup.sh --enable-ipv6 VM_NAME"
        valid_name "$2" || die "Use a valid VM name."
        host_ipv6_command enable "$2"
        printf "\nAfter starting the VM: bash /mnt/shared/antix-setup.sh --guest\n"
        ;;
    --ipv6-status)
        [[ $# -eq 1 ]] || die "Usage: bash antix-vm-setup.sh --ipv6-status"
        host_ipv6_command status
        ;;
    --guest-ipv6)
        [[ $# -eq 1 ]] || die "Usage: bash antix-setup.sh --guest-ipv6"
        guest_ipv6_setup
        ;;
    --guest)
        [[ $# -eq 1 ]] || die 'Usage: bash antix-setup.sh --guest'
        guest_setup
        ;;
    --host)
        shift
        host_setup "$@"
        ;;
    -h|--help|'')
        printf 'Usage: bash antix-vm-setup.sh VM_NAME VM_DOWNLOAD [--replace]\n'
        printf 'Example: VM_NAME="antix-vm1"\n'
        printf '         VM_DOWNLOAD="$HOME/Videos/Captures"\n'
        printf '         bash ~/Downloads/antix-vm-setup.sh "$VM_NAME" "$VM_DOWNLOAD"\n'
        printf 'Run as your normal Fedora user. --replace purges the selected VM/private disk.\n'
        printf "Network: dedicated public IPv6 per VM; desktop Internet traffic cannot fall back to IPv4.\n"
        printf "Root/APT and local DNS retain IPv4. IPv4-only sites will not load in browser.\n"
        printf "Existing VM upgrade: bash antix-vm-setup.sh --enable-ipv6 VM_NAME\n"
        printf "Address report: bash antix-vm-setup.sh --ipv6-status\n"
        printf 'Guest bootstrap: bash /mnt/shared/antix-setup.sh --guest\n'
        ;;
    *)
        [[ $(id -u) -ne 0 ]] || die 'Run from your normal Fedora account; the script invokes sudo.'
        [[ $# -ge 2 && $# -le 3 ]] || die 'Usage: bash antix-vm-setup.sh VM_NAME VM_DOWNLOAD [--replace]'
        valid_name "$1" || die 'Use 1-63 lowercase letters/digits/hyphens, beginning and ending with a letter/digit.'
        [[ "$2" == /* ]] || die 'VM_DOWNLOAD must be an absolute path; use "$HOME/...".'
        download=$(realpath -m -- "$2")
        [[ "$download" == "$HOME/"* ]] || die 'VM_DOWNLOAD must resolve inside your home directory.'
        [[ ! -L "$download" ]] || die 'VM_DOWNLOAD must not be a symbolic link.'
        replace=no
        if [[ $# -eq 3 ]]; then
            [[ "$3" == --replace ]] || die 'The only optional argument is --replace.'
            replace=yes
        fi
        self=$(readlink -f "${BASH_SOURCE[0]}")
        sudo bash "$self" --host "$1" "$HOME" "$(id -u)" "$(id -g)" "$replace" "$download"
        virt-manager --connect qemu:///system >/dev/null 2>&1 &
        ;;
esac
