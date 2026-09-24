import argparse
import dataclasses
import fcntl
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as element_tree
from pathlib import Path
from typing import Any

ARGUMENT_DEFINITIONS = {
    "command": {
        "choices": (
            "enable",
            "refresh",
            "status",
            "preflight",
            "guest",
            "guest-ipv4",
            "install-guest",
        )
    },
    "vm_name": {"nargs": "?"},
}
STATE_DIRECTORY = Path("/var/lib/antix-vm-network")
STATE_PATH = STATE_DIRECTORY / "state.json"
HELPER_PATH = Path("/usr/local/libexec/antix-vm-network.py")
LOCK_PATH = Path("/run/lock/antix-vm-network.lock")
NETWORK_NAME = "antix-vm-ipv6"
BRIDGE_NAME = "virbr-antix6"
PRIVATE_PREFIX = ipaddress.IPv6Network("fd71:6e9f:db42:1::/64")
GATEWAY_ADDRESS = str(PRIVATE_PREFIX.network_address + 1)
HOST_TABLE = "antix_vm_ipv6"
GUEST_TABLE = "antix_vm_browser"
BROWSER_UID = 1000
ROUTE_PROBE = "2606:4700:4700::1111"
LEGACY_PATHS = (
    Path("/usr/local/sbin/antix-vm-ipv6"),
    Path("/etc/systemd/system/antix-vm-ipv6.service"),
    Path("/etc/NetworkManager/dispatcher.d/90-antix-vm-ipv6"),
)


@dataclasses.dataclass
class VmIdentity:
    name: str
    uuid: str
    mac: str
    private_ipv6: str
    public_identifier: int


@dataclasses.dataclass(frozen=True)
class PublicAlias:
    interface: str
    address: str


@dataclasses.dataclass
class NetworkState:
    version: int = 1
    vms: list[VmIdentity] = dataclasses.field(default_factory=list)
    aliases: list[PublicAlias] = dataclasses.field(default_factory=list)


def run_command(
    *arguments: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:

    result = subprocess.run(
        arguments,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "LC_ALL": "C"},
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{arguments[0]} failed: {detail}")
    return result


def run_json(*arguments: str) -> list[dict[str, Any]]:

    return json.loads(run_command(*arguments).stdout)


def virsh(*arguments: str) -> str:

    return run_command(
        "virsh",
        "--connect",
        "qemu:///system",
        *arguments,
    ).stdout


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:

    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError(f"Refusing a symlink at {path}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def validate_state(state: NetworkState) -> None:

    if state.version != 1:
        raise ValueError("Unsupported IPv6 registry version.")
    identifiers = set()
    private_addresses = set()
    mac_addresses = set()
    vm_uuids = set()
    vm_names = set()
    for identity in state.vms:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", identity.name):
            raise ValueError("Invalid VM name in the IPv6 registry.")
        if str(uuid.UUID(identity.uuid)) != identity.uuid:
            raise ValueError("Invalid VM UUID in the IPv6 registry.")
        if not re.fullmatch(r"52:54:00(?::[0-9a-f]{2}){3}", identity.mac):
            raise ValueError("Invalid dedicated MAC in the IPv6 registry.")
        private_address = ipaddress.IPv6Address(identity.private_ipv6)
        if (
            private_address not in PRIVATE_PREFIX
            or int(private_address) <= int(PRIVATE_PREFIX.network_address) + 1
            or str(private_address) != identity.private_ipv6
        ):
            raise ValueError("Invalid private IPv6 address in the registry.")
        if not 1 < identity.public_identifier < 2**64:
            raise ValueError("Invalid public IPv6 interface identifier.")
        for value, used_values in (
            (identity.public_identifier, identifiers),
            (identity.private_ipv6, private_addresses),
            (identity.mac, mac_addresses),
            (identity.uuid, vm_uuids),
            (identity.name, vm_names),
        ):
            if value in used_values:
                raise ValueError("Duplicate VM identity or address in the IPv6 registry.")
            used_values.add(value)
    if len(state.aliases) != len(set(state.aliases)):
        raise ValueError("Duplicate public aliases in the registry.")
    for alias in state.aliases:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", alias.interface):
            raise ValueError("Unsupported uplink interface name.")
        address = ipaddress.IPv6Address(alias.address)
        if not address.is_global or str(address) != alias.address:
            raise ValueError("A managed public alias is not a canonical global address.")


def load_state() -> NetworkState:

    if not STATE_PATH.exists():
        return NetworkState()
    if STATE_PATH.is_symlink():
        raise RuntimeError("Refusing a symlink as the IPv6 registry.")
    data = json.loads(STATE_PATH.read_text())
    state = NetworkState(
        version=data["version"],
        vms=[VmIdentity(**identity) for identity in data["vms"]],
        aliases=[PublicAlias(**alias) for alias in data["aliases"]],
    )
    validate_state(state)
    return state


def save_state(state: NetworkState) -> None:

    validate_state(state)
    atomic_write(STATE_PATH, json.dumps(dataclasses.asdict(state), indent=4) + "\n")


def public_address(identity: VmIdentity, prefix: ipaddress.IPv6Network) -> str:

    if prefix.prefixlen != 64 or not prefix.network_address.is_global:
        raise ValueError("A globally routed /64 prefix is required.")
    return str(prefix.network_address + identity.public_identifier)


def select_uplink(
    route: dict[str, Any],
    interfaces: list[dict[str, Any]],
    managed_aliases: list[PublicAlias],
) -> tuple[str, ipaddress.IPv6Network]:

    interface_name = route.get("dev", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", interface_name):
        raise RuntimeError("No supported default IPv6 uplink was found.")
    owned_addresses = {alias.address for alias in managed_aliases}
    candidates = []
    for interface in interfaces:
        if interface["ifname"] != interface_name:
            continue
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet6" or address.get("prefixlen") != 64:
                continue
            flags = address_flags(address)
            if flags & {"tentative", "dadfailed", "deprecated"}:
                continue
            if address.get("preferred_life_time") == 0:
                continue
            local_address = address["local"]
            if (
                local_address in owned_addresses
                or not ipaddress.IPv6Address(local_address).is_global
            ):
                continue
            candidates.append(local_address)
    if not candidates:
        raise RuntimeError("The default uplink has no usable native global IPv6 /64.")
    preferred_source = route.get("prefsrc", route.get("src"))
    selected_address = (
        preferred_source
        if preferred_source in candidates
        else sorted(candidates)[0]
    )
    return (
        interface_name,
        ipaddress.IPv6Network(f"{selected_address}/64", strict=False),
    )


def current_uplink(state: NetworkState) -> tuple[str, ipaddress.IPv6Network]:

    routes = run_json(
        "ip",
        "-j",
        "-6",
        "route",
        "get",
        ROUTE_PROBE,
    )
    if not routes:
        raise RuntimeError("The host has no default IPv6 route.")
    return select_uplink(
        routes[0],
        run_json(
            "ip",
            "-j",
            "-6",
            "address",
            "show",
        ),
        state.aliases,
    )


def build_host_rules(
    identities: list[VmIdentity],
    interface: str,
    active_addresses: dict[str, str],
    reserved_addresses: set[str],
) -> str:

    lines = [
        f"table ip6 {HOST_TABLE} {{",
        "    chain forward_guard {",
        "        type filter hook forward priority -10; policy accept;",
    ]
    for identity in identities:
        if identity.uuid in active_addresses:
            lines.append(
                f"        iifname \"{BRIDGE_NAME}\" oifname \"{interface}\" "
                f"ether saddr {identity.mac} ip6 saddr {identity.private_ipv6} accept"
            )
    lines.extend([
        f"        iifname \"{BRIDGE_NAME}\" drop",
        f"        oifname \"{BRIDGE_NAME}\" ct state established,related accept",
        f"        oifname \"{BRIDGE_NAME}\" drop",
        "    }",
        "    chain source_nat {",
        "        type nat hook postrouting priority 100; policy accept;",
    ])
    for identity in identities:
        if identity.uuid in active_addresses:
            lines.append(
                f"        iifname \"{BRIDGE_NAME}\" oifname \"{interface}\" "
                f"ip6 saddr {identity.private_ipv6} snat to {active_addresses[identity.uuid]}"
            )
    lines.extend([
        "    }",
        "    chain postrouting_guard {",
        "        type filter hook postrouting priority 110; policy accept;",
    ])
    for address in sorted(set(active_addresses.values())):
        lines.append(
            f"        iifname \"{BRIDGE_NAME}\" oifname \"{interface}\" "
            f"ip6 saddr {address} accept"
        )
    lines.extend([
        f"        iifname \"{BRIDGE_NAME}\" oifname != \"{BRIDGE_NAME}\" drop",
        "    }",
        "    chain host_guard {",
        "        type filter hook output priority -10; policy accept;",
    ])
    for address in sorted(reserved_addresses):
        lines.extend([
            f"        ip6 saddr {address} icmpv6 type "
            "{ nd-neighbor-solicit, nd-neighbor-advert } accept",
            f"        ip6 saddr {address} drop",
        ])
    lines.extend(["    }", "}", ""])
    return "\n".join(lines)


def build_guest_rules(dns_address: str) -> str:

    ipaddress.IPv4Address(dns_address)
    return (
        f"table inet {GUEST_TABLE} {{\n"
        "    chain browser_output {\n"
        "        type filter hook output priority -10; policy accept;\n"
        f"        meta skuid {BROWSER_UID} ip daddr 127.0.0.0/8 accept\n"
        f"        meta skuid {BROWSER_UID} ip daddr {dns_address} udp dport 53 accept\n"
        f"        meta skuid {BROWSER_UID} ip daddr {dns_address} tcp dport 53 accept\n"
        f"        meta skuid {BROWSER_UID} meta nfproto ipv4 reject\n"
        "    }\n"
        "}\n"
    )


def apply_rules(family: str, table: str, rules: str) -> None:

    exists = run_command(
        "nft",
        "list",
        "table",
        family,
        table,
        check=False,
    ).returncode == 0
    transaction = (f"delete table {family} {table}\n" if exists else "") + rules
    run_command(
        "nft",
        "--check",
        "--file",
        "-",
        input_text=transaction,
    )
    run_command(
        "nft",
        "--file",
        "-",
        input_text=transaction,
    )


def network_xml() -> str:

    network = element_tree.Element("network")
    element_tree.SubElement(network, "name").text = NETWORK_NAME
    element_tree.SubElement(network, "forward", {"mode": "route"})
    element_tree.SubElement(
        network,
        "bridge",
        {"name": BRIDGE_NAME, "stp": "off", "delay": "0"},
    )
    element_tree.SubElement(
        network,
        "ip",
        {"family": "ipv6", "address": GATEWAY_ADDRESS, "prefix": "64"},
    )
    return element_tree.tostring(network, encoding="unicode")


def validate_network(xml_text: str) -> None:

    network = element_tree.fromstring(xml_text)
    bridge = network.find("bridge")
    forward = network.find("forward")
    addresses = network.findall("ip")
    if (
        network.findtext("name") != NETWORK_NAME
        or bridge is None
        or bridge.get("name") != BRIDGE_NAME
        or forward is None
        or forward.get("mode") != "route"
        or forward.get("dev") is not None
        or len(addresses) != 1
        or addresses[0].get("family") != "ipv6"
        or addresses[0].get("address") != GATEWAY_ADDRESS
        or addresses[0].get("prefix") != "64"
    ):
        raise RuntimeError("The existing dedicated network has an unexpected definition.")


def ensure_network() -> None:

    networks = virsh("net-list", "--all", "--name").splitlines()
    if NETWORK_NAME in networks:
        validate_network(virsh("net-dumpxml", NETWORK_NAME))
    else:
        interfaces = run_json(
            "ip",
            "-j",
            "link",
            "show",
        )
        if any(interface["ifname"] == BRIDGE_NAME for interface in interfaces):
            raise RuntimeError("The dedicated bridge name is already in use.")
        definition_path = STATE_DIRECTORY / "network.xml"
        atomic_write(definition_path, network_xml())
        virsh("net-define", str(definition_path))
    virsh("net-autostart", NETWORK_NAME)
    if NETWORK_NAME not in virsh("net-list", "--name").splitlines():
        virsh("net-start", NETWORK_NAME)


def address_inventory() -> dict[PublicAlias, dict[str, Any]]:

    inventory = {}
    for interface in run_json(
        "ip",
        "-j",
        "-6",
        "address",
        "show",
    ):
        for address in interface.get("addr_info", []):
            if address.get("family") == "inet6":
                inventory[PublicAlias(interface["ifname"], address["local"])] = address
    return inventory


def remove_tracked_aliases(aliases: list[PublicAlias]) -> None:

    inventory = address_inventory()
    for alias in aliases:
        address = inventory.get(alias)
        if address is not None and (
            address.get("prefixlen") != 128
            or address.get("preferred_life_time") != 0
        ):
            raise RuntimeError(
                f"Tracked alias {alias.address} has unexpected properties; "
                "it was not changed or removed."
            )
    for alias in aliases:
        if alias in inventory:
            run_command(
                "ip",
                "-6",
                "address",
                "del",
                f"{alias.address}/128",
                "dev",
                alias.interface,
            )


def remove_vm_metadata(vm_uuid: str, state: NetworkState) -> None:

    canonical_uuid = str(uuid.UUID(vm_uuid))
    identity = next(
        (entry for entry in state.vms if entry.uuid == canonical_uuid),
        None,
    )
    if identity is not None:
        removed_aliases = [
            alias
            for alias in state.aliases
            if int(ipaddress.IPv6Address(alias.address)) & (2**64 - 1)
            == identity.public_identifier
        ]
        remove_tracked_aliases(removed_aliases)
        state.vms = [entry for entry in state.vms if entry.uuid != canonical_uuid]
        state.aliases = [
            alias for alias in state.aliases if alias not in removed_aliases
        ]
        save_state(state)

    backup_path = STATE_DIRECTORY / "backups" / f"{canonical_uuid}.xml"
    if backup_path.is_symlink():
        raise RuntimeError(f"Refusing a symlink as the domain backup: {backup_path}.")
    backup_path.unlink(missing_ok=True)

    definition_path = STATE_DIRECTORY / "domain.xml"
    if definition_path.is_symlink():
        raise RuntimeError(f"Refusing a symlink as the temporary domain XML: {definition_path}.")
    if definition_path.is_file():
        domain = element_tree.parse(definition_path).getroot()
        if domain.findtext("uuid", "").lower() == canonical_uuid:
            definition_path.unlink()


def prune_missing_vms(state: NetworkState) -> bool:

    if not state.vms:
        return False
    existing_uuids = {
        line.strip().lower()
        for line in virsh("list", "--all", "--uuid").splitlines()
        if line.strip()
    }
    missing_uuids = [
        identity.uuid
        for identity in state.vms
        if identity.uuid not in existing_uuids
    ]
    for vm_uuid in missing_uuids:
        remove_vm_metadata(vm_uuid, state)
    return bool(missing_uuids)


def clear_empty_network_state(state: NetworkState) -> None:

    remove_tracked_aliases(state.aliases)
    if run_command(
        "nft",
        "list",
        "table",
        "ip6",
        HOST_TABLE,
        check=False,
    ).returncode == 0:
        run_command("nft", "delete", "table", "ip6", HOST_TABLE)
    if state.aliases:
        state.aliases = []
        save_state(state)


def address_flags(address: dict[str, Any]) -> set[str]:

    return set(address.get("flags", [])) | {
        flag
        for flag in ("tentative", "dadfailed", "deprecated", "dynamic", "temporary")
        if address.get(flag) is True
    }


def address_ready(address: dict[str, Any]) -> bool:

    return not address_flags(address) & {"tentative", "dadfailed"}


def refresh_network(state: NetworkState) -> None:

    if not state.vms:
        clear_empty_network_state(state)
        return
    interface, prefix = current_uplink(state)
    desired_addresses = {
        identity.uuid: public_address(identity, prefix)
        for identity in state.vms
    }
    desired_aliases = [
        PublicAlias(interface, address)
        for address in desired_addresses.values()
    ]
    inventory = address_inventory()
    owned_aliases = set(state.aliases)
    for alias in owned_aliases & inventory.keys():
        if (
            inventory[alias].get("prefixlen") != 128
            or inventory[alias].get("preferred_life_time") != 0
        ):
            raise RuntimeError(
                f"Tracked alias {alias.address} has unexpected properties; "
                "it was not changed or removed."
            )
    for alias in desired_aliases:
        for existing_alias in inventory:
            if existing_alias.address == alias.address and existing_alias not in owned_aliases:
                raise RuntimeError(
                    f"Address collision at {alias.address}; no existing address was adopted."
                )
        if alias in inventory and inventory[alias].get("prefixlen") != 128:
            raise RuntimeError("A managed alias no longer has its expected /128 prefix.")

    reserved_aliases = set(state.aliases) | set(desired_aliases)
    reserved_addresses = {alias.address for alias in reserved_aliases}
    ready_addresses = {
        identity.uuid: desired_addresses[identity.uuid]
        for identity in state.vms
        if PublicAlias(interface, desired_addresses[identity.uuid]) in inventory
        and address_ready(inventory[PublicAlias(interface, desired_addresses[identity.uuid])])
    }
    apply_rules(
        "ip6",
        HOST_TABLE,
        build_host_rules(
            state.vms,
            interface,
            ready_addresses,
            reserved_addresses,
        ),
    )
    state.aliases = sorted(reserved_aliases, key=lambda alias: (alias.interface, alias.address))
    save_state(state)

    sysctl_path = Path("/etc/sysctl.d/90-antix-vm-network.conf")
    atomic_write(
        sysctl_path,
        f"net.ipv6.conf.{interface}.accept_ra=2\n"
        "net.ipv6.conf.all.forwarding=1\n",
        0o644,
    )
    run_command("sysctl", "-w", f"net.ipv6.conf.{interface}.accept_ra=2")
    run_command("sysctl", "-w", "net.ipv6.conf.all.forwarding=1")
    ensure_network()

    for alias in desired_aliases:
        if alias not in inventory:
            run_command(
                "ip",
                "-6",
                "address",
                "add",
                f"{alias.address}/128",
                "dev",
                alias.interface,
                "preferred_lft",
                "0",
                "valid_lft",
                "forever",
                "noprefixroute",
            )

    deadline = time.monotonic() + 10
    while True:
        inventory = address_inventory()
        if any(
            "dadfailed" in address_flags(inventory.get(alias, {}))
            for alias in desired_aliases
        ):
            raise RuntimeError(
                "IPv6 duplicate-address detection failed; "
                "the affected mapping stays blocked."
            )
        if all(
            alias in inventory and address_ready(inventory[alias])
            for alias in desired_aliases
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "IPv6 duplicate-address detection did not finish; "
                "mappings stay blocked."
            )
        time.sleep(0.2)

    apply_rules(
        "ip6",
        HOST_TABLE,
        build_host_rules(
            state.vms,
            interface,
            desired_addresses,
            reserved_addresses,
        ),
    )
    for alias in state.aliases:
        if alias not in desired_aliases and alias in inventory:
            run_command(
                "ip",
                "-6",
                "address",
                "del",
                f"{alias.address}/128",
                "dev",
                alias.interface,
            )
    state.aliases = desired_aliases
    save_state(state)


def validate_domain(domain: element_tree.Element, vm_name: str) -> Path:

    if domain.findtext("name") != vm_name:
        raise ValueError("The selected domain name does not match.")
    vm_uuid = str(uuid.UUID(domain.findtext("uuid", "")))
    arguments = shlex.split(domain.findtext("./os/cmdline", ""))
    if f"antix_vm_uuid={vm_uuid}" not in arguments:
        raise RuntimeError("This is not a VM created by the maintained antiX setup.")
    if domain.findtext("./os/kernel") != "/var/lib/libvirt/images/antix-26-shared-boot/vmlinuz":
        raise RuntimeError("The selected VM has an unexpected kernel.")
    interfaces = domain.findall("./devices/interface")
    if not interfaces or any(
        interface.get("type") != "network"
        or interface.find("source") is None
        or interface.find("source").get("network") not in {"default", NETWORK_NAME}
        for interface in interfaces
    ):
        raise RuntimeError("The selected VM has an unmanaged network interface.")
    if sum(interface.find("source").get("network") == "default" for interface in interfaces) != 1:
        raise RuntimeError("Exactly one default-network adapter is required.")
    if sum(
        interface.find("source").get("network") == NETWORK_NAME
        for interface in interfaces
    ) > 1:
        raise RuntimeError("More than one dedicated IPv6 adapter is attached.")
    for filesystem in domain.findall("./devices/filesystem"):
        target = filesystem.find("target")
        source = filesystem.find("source")
        if target is not None and target.get("dir") == "shared" and source is not None:
            share = Path(source.get("dir", ""))
            if share.is_absolute() and share.is_dir() and not share.is_symlink():
                return share
    raise RuntimeError("The selected VM has no usable shared folder.")


def assign_identity(
    state: NetworkState,
    vm_name: str,
    vm_uuid: str,
    used_macs: set[str],
) -> VmIdentity:

    for identity in state.vms:
        if identity.uuid == vm_uuid:
            identity.name = vm_name
            validate_state(state)
            return identity
    state.vms = [identity for identity in state.vms if identity.name != vm_name]
    used_identifiers = {identity.public_identifier for identity in state.vms} | {
        int(ipaddress.IPv6Address(alias.address)) & (2**64 - 1)
        for alias in state.aliases
    }
    used_private = {identity.private_ipv6 for identity in state.vms}
    while True:
        public_identifier = secrets.randbits(64)
        private_address = str(PRIVATE_PREFIX.network_address + public_identifier)
        if (
            public_identifier > 1
            and public_identifier not in used_identifiers
            and private_address not in used_private
        ):
            break
    used_macs |= {identity.mac for identity in state.vms}
    while True:
        mac = "52:54:00:" + ":".join(f"{byte:02x}" for byte in secrets.token_bytes(3))
        if mac not in used_macs:
            break
    identity = VmIdentity(
        vm_name,
        vm_uuid,
        mac,
        private_address,
        public_identifier,
    )
    state.vms.append(identity)
    validate_state(state)
    return identity


def update_domain(
    domain: element_tree.Element,
    identity: VmIdentity,
    dns_address: str,
) -> str:

    ipaddress.IPv4Address(dns_address)
    devices = domain.find("devices")
    operating_system = domain.find("os")
    if devices is None or operating_system is None:
        raise ValueError("The VM definition is incomplete.")
    for interface in list(devices.findall("interface")):
        source = interface.find("source")
        if source is not None and source.get("network") == NETWORK_NAME:
            devices.remove(interface)
    interface = element_tree.SubElement(devices, "interface", {"type": "network"})
    element_tree.SubElement(interface, "mac", {"address": identity.mac})
    element_tree.SubElement(interface, "source", {"network": NETWORK_NAME})
    element_tree.SubElement(interface, "model", {"type": "virtio"})
    element_tree.SubElement(interface, "port", {"isolated": "yes"})
    cmdline = operating_system.find("cmdline")
    if cmdline is None:
        cmdline = element_tree.SubElement(operating_system, "cmdline")
    arguments = [
        argument
        for argument in shlex.split(cmdline.text or "")
        if not argument.startswith((
            "ipv6.disable=",
            "antix_private_ipv6=",
            "antix_ipv6_",
            "antix_ipv4_dns=",
        ))
    ]
    arguments.extend([
        "antix_ipv6_version=1",
        f"antix_private_ipv6={identity.private_ipv6}",
        f"antix_ipv6_mac={identity.mac}",
        f"antix_ipv6_gateway={GATEWAY_ADDRESS}",
        f"antix_ipv4_dns={dns_address}",
    ])
    cmdline.text = shlex.join(arguments)
    return element_tree.tostring(domain, encoding="unicode")


def install_host_service() -> None:

    atomic_write(HELPER_PATH, Path(__file__).read_text(), 0o644)
    atomic_write(
        Path("/etc/systemd/system/antix-vm-network.service"),
        "[Unit]\n"
        "Description=Dedicated antiX VM IPv6 addresses and source guards\n"
        "After=network-online.target virtnetworkd.socket firewalld.service\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/usr/bin/python3 {HELPER_PATH} refresh\n"
        "TimeoutStartSec=120\n",
        0o644,
    )
    atomic_write(
        Path("/etc/systemd/system/antix-vm-network.timer"),
        "[Unit]\n"
        "Description=Refresh antiX VM addresses after uplink changes\n\n"
        "[Timer]\n"
        "OnBootSec=15s\n"
        "OnUnitInactiveSec=30s\n"
        "Unit=antix-vm-network.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n",
        0o644,
    )
    run_command("systemctl", "daemon-reload")
    run_command(
        "systemctl",
        "enable",
        "--now",
        "antix-vm-network.timer",
    )


def host_preflight(state: NetworkState) -> None:

    for command in ("ip", "nft", "sysctl", "virsh", "systemctl"):
        if shutil.which(command) is None:
            raise RuntimeError(f"Missing host dependency: {command}")
    for legacy_path in LEGACY_PATHS:
        if legacy_path.exists():
            raise RuntimeError(f"Remove the previous IPv6 patch first: {legacy_path}")
    run_command("nft", "list", "tables")
    if run_command(
        "nft",
        "list",
        "table",
        "ip6",
        "antix_vm_snat",
        check=False,
    ).returncode == 0:
        raise RuntimeError(
            "Remove the old ip6 antix_vm_snat table before enabling this implementation."
        )
    current_uplink(state)
    if NETWORK_NAME in virsh("net-list", "--all", "--name").splitlines():
        validate_network(virsh("net-dumpxml", NETWORK_NAME))


def enable_vm(vm_name: str, state: NetworkState) -> None:

    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", vm_name):
        raise ValueError("Use a valid VM name.")
    host_preflight(state)
    if virsh("domstate", vm_name).strip() != "shut off":
        raise RuntimeError(
            "Shut down the selected VM normally before enabling IPv6; "
            "its disk will be preserved."
        )
    original_xml = virsh("dumpxml", vm_name, "--inactive")
    domain = element_tree.fromstring(original_xml)
    share = validate_domain(domain, vm_name)
    setup_path = Path(__file__).with_name("antix-vm-setup.sh")
    if not setup_path.is_file():
        raise RuntimeError(
            "Run --enable-ipv6 from the maintained setup script and its companion helper."
        )
    setup_source = setup_path.read_text()
    default_network = element_tree.fromstring(virsh("net-dumpxml", "default"))
    dns_address = next(
        (
            address.get("address", "")
            for address in default_network.findall("ip")
            if address.get("family", "ipv4") == "ipv4"
        ),
        "",
    )
    ipaddress.IPv4Address(dns_address)
    used_macs = set()
    for name in virsh("list", "--all", "--name").splitlines():
        if name:
            other_domain = element_tree.fromstring(virsh("dumpxml", name, "--inactive"))
            used_macs.update(
                mac.get("address", "").lower()
                for mac in other_domain.findall("./devices/interface/mac")
            )
    identity = assign_identity(
        state,
        vm_name,
        domain.findtext("uuid", ""),
        used_macs,
    )
    updated_xml = update_domain(domain, identity, dns_address)
    backup_path = STATE_DIRECTORY / "backups" / f"{identity.uuid}.xml"
    if not backup_path.exists():
        atomic_write(backup_path, original_xml)
    save_state(state)
    refresh_network(state)
    definition_path = STATE_DIRECTORY / "domain.xml"
    atomic_write(definition_path, updated_xml)
    virsh("define", str(definition_path))
    atomic_write(share / "antix-vm-setup.sh", setup_source, 0o755)
    atomic_write(share / "antix-vm-network.py", Path(__file__).read_text(), 0o644)
    (share / "antix-setup.sh").unlink(missing_ok=True)
    install_host_service()
    print(f"IPv6 configured for {vm_name}; its disk and profile were preserved.")
    print("Start the VM and complete the guest step in ANTIX-VM-GUIDE.md.")
    print_status(state)


def guest_configuration() -> dict[str, str]:

    arguments = dict(
        argument.split("=", 1)
        for argument in shlex.split(Path("/proc/cmdline").read_text())
        if "=" in argument
    )
    if arguments.get("antix_ipv6_version") != "1" or arguments.get("ipv6.disable") == "1":
        raise RuntimeError("Enable IPv6 on the host for this VM, then shut down and start the VM.")
    private_address = arguments.get("antix_private_ipv6", "")
    parsed_address = ipaddress.IPv6Address(private_address)
    if (
        parsed_address not in PRIVATE_PREFIX
        or int(parsed_address) <= int(PRIVATE_PREFIX.network_address) + 1
        or str(parsed_address) != private_address
    ):
        raise ValueError("The guest private IPv6 address is invalid.")
    if arguments.get("antix_ipv6_gateway") != GATEWAY_ADDRESS:
        raise ValueError("The guest IPv6 gateway is invalid.")
    ipaddress.IPv4Address(arguments.get("antix_ipv4_dns", ""))
    if not re.fullmatch(r"52:54:00(?::[0-9a-f]{2}){3}", arguments.get("antix_ipv6_mac", "")):
        raise ValueError("The guest dedicated MAC is invalid.")
    return arguments


def guest_ipv4_interfaces(
    configuration: dict[str, str],
    interfaces: list[dict[str, Any]],
) -> tuple[str, str, str]:

    gateway = ipaddress.IPv4Address(configuration["antix_ipv4_dns"])
    dedicated_interfaces = [
        interface["ifname"]
        for interface in interfaces
        if interface.get("address", "").lower() == configuration["antix_ipv6_mac"]
    ]
    if len(dedicated_interfaces) != 1:
        raise RuntimeError("The dedicated IPv6 adapter is missing or ambiguous.")
    dedicated_interface = dedicated_interfaces[0]
    candidates = []
    for interface in interfaces:
        interface_name = interface["ifname"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", interface_name):
            raise RuntimeError("Unsupported guest interface name.")
        if interface_name in {"lo", dedicated_interface}:
            continue
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet":
                continue
            local = ipaddress.IPv4Interface(
                f"{address["local"]}/{address["prefixlen"]}"
            )
            if (
                not local.ip.is_link_local
                and not local.ip.is_loopback
                and local.ip != gateway
                and gateway in local.network
            ):
                candidates.append((interface_name, str(local.ip)))
    if len({interface for interface, address in candidates}) != 1:
        raise RuntimeError(
            "No unique IPv4 adapter has a lease on the libvirt gateway subnet."
        )
    ipv4_interface, source_address = sorted(candidates)[0]
    return dedicated_interface, ipv4_interface, source_address


def preserve_guest_configuration(path: Path, content: str) -> bool:

    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlink at {path}.")
    previous_content = path.read_text() if path.exists() else ""
    if previous_content == content:
        return False
    if path.exists():
        backup_path = path.with_name(f"{path.name}.antix-vm-backup")
        if not backup_path.exists():
            atomic_write(backup_path, previous_content, 0o600)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    atomic_write(path, content, mode)
    return True


def prevent_dedicated_ipv4(interface: str, mac: str) -> None:

    dhcpcd_path = Path("/etc/dhcpcd.conf")
    if dhcpcd_path.exists() or shutil.which("dhcpcd") is not None:
        previous_content = dhcpcd_path.read_text() if dhcpcd_path.exists() else ""
        policy = (
            f"interface {interface}\n"
            "    noipv4\n"
            "    noipv4ll\n"
            "    nogateway\n"
        )
        if policy not in previous_content:
            preserve_guest_configuration(
                dhcpcd_path,
                previous_content.rstrip() + "\n\n" + policy,
            )
            if (
                shutil.which("dhcpcd") is not None
                and run_command(
                    "pgrep",
                    "-x",
                    "dhcpcd",
                    check=False,
                ).returncode == 0
            ):
                run_command("dhcpcd", "--rebind", interface)

    connman_directory = Path("/var/lib/connman")
    if connman_directory.is_dir() or shutil.which("connmanctl") is not None:
        policy_path = connman_directory / "antix-vm-ipv6.config"
        policy = (
            "[service_antix_ipv6]\n"
            "Type=ethernet\n"
            f"MAC={mac}\n"
            "IPv4=off\n"
        )
        previous_policy = policy_path.read_text() if policy_path.exists() else ""
        if previous_policy != policy:
            if shutil.which("connmanctl") is not None:
                result = run_command("connmanctl", "services", check=False)
                pattern = rf"\b(ethernet_{mac.replace(":", "")}_[a-zA-Z0-9_]+)\b"
                for service in sorted(set(re.findall(pattern, result.stdout))):
                    result = run_command(
                        "connmanctl",
                        "config",
                        service,
                        "--ipv4",
                        "off",
                    )
                    if "Error" in result.stdout or "Error" in result.stderr:
                        raise RuntimeError(
                            f"Cannot disable ConnMan IPv4 on {interface}: "
                            f"{result.stdout.strip()} {result.stderr.strip()}"
                        )
            preserve_guest_configuration(policy_path, policy)


def restore_guest_ipv4_route(
    dedicated_interface: str,
    ipv4_interface: str,
    source_address: str,
    gateway: str,
) -> None:

    routes = run_json(
        "ip",
        "-j",
        "-4",
        "route",
        "show",
        "default",
        "dev",
        dedicated_interface,
    )
    for route in routes:
        arguments = ["ip", "-4", "route", "del", "default"]
        if route.get("gateway"):
            arguments.extend(["via", route["gateway"]])
        arguments.extend(["dev", dedicated_interface])
        if "metric" in route:
            arguments.extend(["metric", str(route["metric"])])
        run_command(*arguments, check=False)
    run_command(
        "ip",
        "-4",
        "route",
        "replace",
        "default",
        "via",
        gateway,
        "dev",
        ipv4_interface,
        "src",
        source_address,
        "metric",
        "100",
    )
    routes = run_json(
        "ip",
        "-j",
        "-4",
        "route",
        "get",
        "1.1.1.1",
    )
    if (
        not routes
        or routes[0].get("dev") != ipv4_interface
        or routes[0].get("gateway") != gateway
        or routes[0].get("prefsrc", routes[0].get("src")) != source_address
    ):
        raise RuntimeError(
            "The guest IPv4 route still selects the wrong adapter; "
            "package setup was not started."
        )


def configure_guest_ipv4() -> None:

    configuration = guest_configuration()
    interfaces = run_json(
        "ip",
        "-j",
        "address",
        "show",
    )
    dedicated_interface, ipv4_interface, source_address = guest_ipv4_interfaces(
        configuration,
        interfaces,
    )
    prevent_dedicated_ipv4(
        dedicated_interface,
        configuration["antix_ipv6_mac"],
    )
    restore_guest_ipv4_route(
        dedicated_interface,
        ipv4_interface,
        source_address,
        configuration["antix_ipv4_dns"],
    )
    print(
        f"IPv4 maintenance route: {source_address} on {ipv4_interface} "
        f"via {configuration["antix_ipv4_dns"]}; "
        f"{dedicated_interface} is reserved for IPv6."
    )


def configure_guest() -> None:

    configuration = guest_configuration()
    if shutil.which("nft") is None:
        raise RuntimeError("Run the standard --guest setup to install guest nftables.")
    apply_rules(
        "inet",
        GUEST_TABLE,
        build_guest_rules(configuration["antix_ipv4_dns"]),
    )
    configure_guest_ipv4()
    interfaces = run_json(
        "ip",
        "-j",
        "link",
        "show",
    )
    dedicated_interfaces = [
        interface["ifname"]
        for interface in interfaces
        if interface.get("address", "").lower() == configuration["antix_ipv6_mac"]
    ]
    if len(dedicated_interfaces) != 1:
        raise RuntimeError("The dedicated IPv6 adapter is missing or ambiguous.")
    dedicated_interface = dedicated_interfaces[0]
    for interface in interfaces:
        interface_name = interface["ifname"]
        if interface_name == "lo":
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", interface_name):
            raise RuntimeError("Unsupported guest interface name.")
        if interface_name != dedicated_interface:
            run_command("sysctl", "-w", f"net.ipv6.conf.{interface_name}.disable_ipv6=1")
    for setting, value in (
        ("disable_ipv6", "0"),
        ("accept_ra", "0"),
        ("autoconf", "0"),
        ("use_tempaddr", "0"),
    ):
        run_command("sysctl", "-w", f"net.ipv6.conf.{dedicated_interface}.{setting}={value}")
    run_command(
        "ip",
        "link",
        "set",
        "dev",
        dedicated_interface,
        "up",
    )
    private_address = configuration["antix_private_ipv6"]
    existing_addresses = run_json(
        "ip",
        "-j",
        "-6",
        "address",
        "show",
        "dev",
        dedicated_interface,
    )
    for interface in existing_addresses:
        for address in interface.get("addr_info", []):
            if address.get("scope") == "global" and address["local"] != private_address:
                if ipaddress.IPv6Address(address["local"]) not in PRIVATE_PREFIX:
                    raise RuntimeError(
                        "The dedicated adapter has an unmanaged global IPv6 address."
                    )
                run_command(
                    "ip",
                    "-6",
                    "address",
                    "del",
                    f"{address["local"]}/{address["prefixlen"]}",
                    "dev",
                    dedicated_interface,
                )
    run_command(
        "ip",
        "-6",
        "address",
        "replace",
        f"{private_address}/64",
        "dev",
        dedicated_interface,
        "preferred_lft",
        "forever",
        "valid_lft",
        "forever",
    )
    deadline = time.monotonic() + 10
    alias = PublicAlias(dedicated_interface, private_address)
    while True:
        address = address_inventory().get(alias)
        if address and "dadfailed" in address_flags(address):
            raise RuntimeError("The guest private IPv6 address is duplicated.")
        if address and address_ready(address):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("The guest IPv6 address is not ready.")
        time.sleep(0.2)
    run_command(
        "ip",
        "-6",
        "route",
        "replace",
        "default",
        "via",
        GATEWAY_ADDRESS,
        "dev",
        dedicated_interface,
        "src",
        private_address,
        "metric",
        "50",
    )


def install_guest() -> None:

    guest_configuration()
    runtime_path = Path("/usr/local/sbin/antix-vm-runtime")
    runtime_text = runtime_path.read_text()
    if "antix-vm-runtime.lock" not in runtime_text:
        raise RuntimeError("Complete the normal guest setup before installing the IPv6 upgrade.")
    invocation = f"/usr/bin/python3 {HELPER_PATH} guest"
    bootstrap_invocation = (
        f"if [ -f {HELPER_PATH} ]; then\n"
        f"    {invocation}\n"
        "fi\n"
    )
    updated_runtime = runtime_text.replace(bootstrap_invocation, f"{invocation}\n")
    if invocation not in updated_runtime:
        if "flock -x 9\n" not in updated_runtime:
            raise RuntimeError("The guest runtime has an unexpected structure.")
        updated_runtime = updated_runtime.replace(
            "flock -x 9\n",
            f"flock -x 9\n{invocation}\n",
            1,
        )
    atomic_write(HELPER_PATH, Path(__file__).read_text(), 0o644)
    if updated_runtime != runtime_text:
        atomic_write(runtime_path, updated_runtime, 0o755)
    configure_guest()
    print(
        "Desktop-user Internet traffic is now IPv6-only; "
        "DNS and root/APT IPv4 remain available."
    )


def print_status(state: NetworkState) -> None:

    inventory = address_inventory()
    report = []
    for identity in state.vms:
        addresses = [
            {
                "address": alias.address,
                "interface": alias.interface,
                "ready": alias in inventory and address_ready(inventory[alias]),
            }
            for alias in state.aliases
            if (
                int(ipaddress.IPv6Address(alias.address)) & (2**64 - 1)
                == identity.public_identifier
            )
        ]
        report.append({
            "name": identity.name,
            "uuid": identity.uuid,
            "private_ipv6": identity.private_ipv6,
            "public_ipv6": addresses,
        })
    print(json.dumps(report, indent=4))


def main() -> None:

    parser = argparse.ArgumentParser(
        description="Maintain explicit per-VM IPv6 source addresses."
    )
    for name, options in ARGUMENT_DEFINITIONS.items():
        parser.add_argument(name, **options)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run the network helper through sudo.")
    if arguments.command == "enable" and not arguments.vm_name:
        parser.error("The enable command requires a VM name.")
    if arguments.command != "enable" and arguments.vm_name:
        parser.error("Only enable accepts a VM name.")
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            if arguments.command == "guest":
                configure_guest()
            elif arguments.command == "guest-ipv4":
                configure_guest_ipv4()
            elif arguments.command == "install-guest":
                install_guest()
            else:
                helper_source = Path(__file__).read_text()
                if HELPER_PATH.is_symlink():
                    raise RuntimeError(f"Refusing a symlink as the installed helper: {HELPER_PATH}.")
                if not HELPER_PATH.is_file() or HELPER_PATH.read_text() != helper_source:
                    atomic_write(HELPER_PATH, helper_source, 0o644)
                state = load_state()
                pruned = prune_missing_vms(state)
                if (pruned or not state.vms) and arguments.command != "refresh":
                    refresh_network(state)
                if arguments.command == "enable":
                    enable_vm(arguments.vm_name, state)
                elif arguments.command == "refresh":
                    refresh_network(state)
                elif arguments.command == "preflight":
                    host_preflight(state)
                else:
                    print_status(state)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
        element_tree.ParseError,
    ) as error:
        print(f"STOP: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
