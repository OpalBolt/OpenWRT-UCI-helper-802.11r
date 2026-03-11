#!/usr/bin/env python3
"""OpenWRT 802.11r/k/v Roaming Configuration Helper

Reads router and network definitions from a YAML config file,
connects via SSH to discover WiFi interfaces and BSSIDs, then
applies UCI commands directly on each router via SSH.

The script does NOT commit changes — you review with
``uci changes wireless`` then run ``uci commit wireless``
and ``wifi reload`` on each router yourself.

Usage:
    python3 helper.py                       # uses config.yaml
    python3 helper.py --config my.yaml      # custom config path
    python3 helper.py --dry-run             # show commands without executing
    python3 helper.py --test                # dry-run with fabricated data
"""

import argparse
import json
import os
import binascii
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required.  Install with:  pip install pyyaml")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def random_hex(length):
    """Generate a random hex string of the given byte-length."""
    return binascii.b2a_hex(os.urandom(length)).decode()


# ---------------------------------------------------------------------------
# SSH helper
# ---------------------------------------------------------------------------

class SSHConnection:
    """Execute commands on a remote OpenWRT router via SSH."""

    def __init__(self, host, user='root', port=22):
        self.host = host
        self.user = user
        self.port = port

    def run(self, command, check=True):
        """Run *command* over SSH and return stdout (stripped).

        Raises RuntimeError when *check* is True and the remote command
        exits with a non-zero status.
        """
        result = subprocess.run(
            [
                'ssh',
                '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ConnectTimeout=10',
                '-p', str(self.port),
                f'{self.user}@{self.host}',
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed on {self.host}:\n"
                f"  cmd:    {command}\n"
                f"  stderr: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def test_connection(self):
        """Return True if the router is reachable over SSH."""
        try:
            self.run('echo ok')
            return True
        except Exception as e:
            print(f"  ✗ Failed to connect to {self.host}: {e}")
            return False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WifiInterface:
    """A single WiFi interface (one per SSID-per-radio) on a router."""
    index: int
    ssid: str
    device: str
    band: str           # '2g' or '5g'
    encryption: str = ''
    mode: str = 'ap'
    network: str = 'lan'
    bssid: str = ''
    nasid: str = ''
    ifname: str = ''


@dataclass
class Router:
    """An OpenWRT router."""
    host: str
    ssh: SSHConnection
    interfaces: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _get_band(ssh, device):
    """Determine the band ('2g' / '5g') for a wireless *device*."""
    # Modern OpenWRT (21.02+) uses the 'band' option.
    band = ssh.run(
        f"uci get wireless.{device}.band 2>/dev/null || echo ''", check=False
    )
    if band in ('2g', '5g', '6g'):
        return band

    # Older firmware falls back to hwmode.
    hwmode = ssh.run(
        f"uci get wireless.{device}.hwmode 2>/dev/null || echo ''", check=False
    )
    if '11a' in hwmode or '11ac' in hwmode or '11ax' in hwmode:
        return '5g'
    return '2g'


def _fill_bssids_ubus(ssh, interfaces):
    """Try to fill BSSID/ifname from ``ubus call network.wireless status``."""
    try:
        raw = ssh.run('ubus call network.wireless status')
        status = json.loads(raw)
    except Exception:
        return False

    for _radio_name, radio_info in status.items():
        for iface_info in radio_info.get('interfaces', []):
            config = iface_info.get('config', {})
            ssid = config.get('ssid', '')
            device = config.get('device', '')
            bssid = iface_info.get('iwinfo', {}).get('bssid', '')
            ifname = iface_info.get('ifname', '')

            for iface in interfaces:
                if iface.ssid == ssid and iface.device == device:
                    iface.bssid = bssid
                    iface.ifname = ifname
                    if bssid:
                        iface.nasid = bssid.replace(':', '')
    return True


def _fill_bssids_iwinfo(ssh, interfaces):
    """Fallback: use ``iwinfo`` to discover BSSIDs."""
    for iface in interfaces:
        if iface.bssid:
            continue
        if not iface.ifname:
            continue
        try:
            output = ssh.run(f"iwinfo {iface.ifname} info 2>/dev/null || true", check=False)
            match = re.search(r'Access Point:\s*([0-9A-Fa-f:]{17})', output)
            if match:
                iface.bssid = match.group(1)
                iface.nasid = iface.bssid.replace(':', '')
        except Exception:
            pass


def discover_interfaces(router):
    """Populate *router.interfaces* by querying UCI + ubus."""
    ssh = router.ssh

    # Count wifi-iface sections
    count_raw = ssh.run("uci show wireless | grep -c '=wifi-iface' || echo 0", check=False)
    try:
        num_ifaces = int(count_raw.strip())
    except ValueError:
        num_ifaces = 0

    interfaces = []
    for i in range(num_ifaces):
        try:
            ssid = ssh.run(
                f"uci get wireless.@wifi-iface[{i}].ssid 2>/dev/null || echo ''",
                check=False,
            )
            if not ssid:
                continue

            device = ssh.run(
                f"uci get wireless.@wifi-iface[{i}].device 2>/dev/null || echo ''",
                check=False,
            )
            encryption = ssh.run(
                f"uci get wireless.@wifi-iface[{i}].encryption 2>/dev/null || echo ''",
                check=False,
            )
            mode = ssh.run(
                f"uci get wireless.@wifi-iface[{i}].mode 2>/dev/null || echo 'ap'",
                check=False,
            ) or 'ap'
            network_name = ssh.run(
                f"uci get wireless.@wifi-iface[{i}].network 2>/dev/null || echo 'lan'",
                check=False,
            ) or 'lan'
            band = _get_band(ssh, device) if device else '2g'

            interfaces.append(WifiInterface(
                index=i,
                ssid=ssid,
                device=device,
                band=band,
                encryption=encryption,
                mode=mode,
                network=network_name,
            ))
        except Exception as e:
            print(f"    Warning: could not read wifi-iface[{i}] on {router.host}: {e}")

    # Fill in BSSIDs
    if not _fill_bssids_ubus(ssh, interfaces):
        _fill_bssids_iwinfo(ssh, interfaces)
    else:
        # ubus may not have had every ifname; fall back for the rest
        _fill_bssids_iwinfo(ssh, interfaces)

    router.interfaces = interfaces
    return interfaces


# ---------------------------------------------------------------------------
# Config data class
# ---------------------------------------------------------------------------

@dataclass
class NetworkConfig:
    """Per-network feature flags from config.yaml."""
    ssid: str
    key: str = ''
    encryption: str = 'sae'   # WPA3-SAE by default
    network: str = ''         # UCI network name (e.g. 'lan', 'guest'); empty = use discovered value
    ieee80211r: bool = True
    ieee80211k: bool = True
    ieee80211v: bool = True
    hidden: bool = False


# ---------------------------------------------------------------------------
# YAML configuration loader
# ---------------------------------------------------------------------------

def load_config(path):
    """Load and validate config.yaml.  Returns (routers, networks)."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        print(f"Config file not found: {cfg_path}")
        sys.exit(1)

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    # -- Routers ---------------------------------------------------------------
    routers_cfg = cfg.get('routers', [])
    if not routers_cfg:
        print("No routers defined in config.")
        sys.exit(1)

    routers = []
    for entry in routers_cfg:
        host = entry['host']
        user = entry.get('user', 'root')
        port = entry.get('port', 22)
        ssh = SSHConnection(host, user, port)
        routers.append(Router(host=host, ssh=ssh))

    # -- Networks --------------------------------------------------------------
    networks_cfg = cfg.get('networks', [])
    if not networks_cfg:
        print("No networks defined in config.")
        sys.exit(1)

    networks = []
    for entry in networks_cfg:
        ssid = entry['ssid']
        key = entry.get('key', '')
        encryption = entry.get('encryption', 'sae')
        network = entry.get('network', '')
        r_flag = entry.get('802.11r', True)
        k_flag = entry.get('802.11k', True)
        v_flag = entry.get('802.11v', True)
        hidden_flag = entry.get('hidden', False)

        if not key:
            print(f"Warning: no 'key' defined for network '{ssid}'")

        # If 802.11r is disabled, force everything off
        if not r_flag:
            k_flag = False
            v_flag = False

        networks.append(NetworkConfig(
            ssid=ssid,
            key=key,
            encryption=encryption,
            network=network,
            ieee80211r=r_flag,
            ieee80211k=k_flag,
            ieee80211v=v_flag,
            hidden=hidden_flag,
        ))

    return routers, networks


# ---------------------------------------------------------------------------
# Applying configuration via SSH (or dry-run)
# ---------------------------------------------------------------------------


def _build_group_data(routers, networks):
    """Pre-compute shared secrets & r0kh/r1kh lists per (SSID, band).

    Returns a dict keyed by (ssid, band) with values:
        {mobility_domain, ft_key, r0kh_list, r1kh_list}
    Only populated for networks where 802.11r is enabled.
    """
    group_data = {}

    for net in networks:
        if not net.ieee80211r:
            continue

        # Collect all interfaces across routers for this SSID
        by_band = {}  # band -> [(router, iface), ...]
        for router in routers:
            for iface in router.interfaces:
                if iface.ssid == net.ssid and iface.bssid:
                    by_band.setdefault(iface.band, []).append((router, iface))

        for band, members in by_band.items():
            if len(members) < 2:
                continue
            mobility_domain = random_hex(2)
            ft_key = random_hex(16)
            r0kh_list = [f"{i.bssid},{i.nasid},{ft_key}" for _, i in members]
            r1kh_list = [f"{i.bssid},{i.bssid},{ft_key}" for _, i in members]
            group_data[(net.ssid, band)] = {
                'mobility_domain': mobility_domain,
                'ft_key': ft_key,
                'r0kh_list': r0kh_list,
                'r1kh_list': r1kh_list,
                'ap_count': len(members),
            }

    return group_data


def _run_or_print(ssh, command, dry_run):
    """Execute *command* via SSH, or just print it when *dry_run* is True."""
    if dry_run:
        print(f"    {command}")
    else:
        ssh.run(command, check=False)


def apply_config(routers, networks, dry_run=False):
    """Delete + recreate wifi-iface sections on each router via SSH.

    When *dry_run* is True the UCI commands are printed instead of
    executed, which is also used for ``--test`` mode.
    """
    group_data = _build_group_data(routers, networks)

    # Build set of SSIDs we manage, including suffixed variants from
    # previous runs (non-roaming networks get a -rN suffix).
    configured_ssids = set()
    for net in networks:
        configured_ssids.add(net.ssid)
        if not net.ieee80211r:
            for n in range(1, len(routers) + 1):
                configured_ssids.add(f"{net.ssid}-r{n}")

    for router_num, router in enumerate(routers, 1):
        print(f"\n{'─' * 60}")
        print(f"  Router: {router.host}")
        print(f"{'─' * 60}")

        # ── 1. Collect indices of wifi-iface sections we manage ───────────
        indices_to_delete = sorted(
            [i.index for i in router.interfaces if i.ssid in configured_ssids],
            reverse=True,  # delete highest index first to avoid shifting
        )

        # Snapshot the interfaces we'll recreate (before deletion)
        ifaces_to_recreate = [
            i for i in router.interfaces if i.ssid in configured_ssids
        ]

        # ── 2. Delete existing matching sections (reverse order) ──────────
        if indices_to_delete:
            print("  Deleting existing wifi-iface sections …")
            for idx in indices_to_delete:
                _run_or_print(
                    router.ssh,
                    f"uci delete wireless.@wifi-iface[{idx}]",
                    dry_run,
                )
        else:
            print("  (no existing wifi-iface sections to delete)")

        # ── 3. Recreate each interface with uci add + set ─────────────────
        # Build a lookup: ssid -> NetworkConfig (including suffixed variants)
        net_by_ssid = {}
        for net in networks:
            net_by_ssid[net.ssid] = net
            if not net.ieee80211r:
                for n in range(1, len(routers) + 1):
                    net_by_ssid[f"{net.ssid}-r{n}"] = net

        # Recreate in a deterministic order: config-file network order,
        # then 2g before 5g within each network.
        ordered_ifaces = sorted(
            ifaces_to_recreate,
            key=lambda i: (
                [n.ssid for n in networks].index(net_by_ssid[i.ssid].ssid),
                0 if i.band == '2g' else 1,
            ),
        )

        for iface in ordered_ifaces:
            net = net_by_ssid[iface.ssid]
            band_label = "2.4 GHz" if iface.band == '2g' else "5 GHz"

            # Resolve the UCI network name: config override > discovered value
            uci_network = net.network if net.network else iface.network

            print(f"\n  Creating: {net.ssid} ({band_label}) on {iface.device} "
                  f"[network={uci_network}]")

            # Non-roaming networks get a unique SSID per router
            effective_ssid = net.ssid if net.ieee80211r else f"{net.ssid}-r{router_num}"

            pf = "wireless.@wifi-iface[-1]"

            _run_or_print(router.ssh, "uci add wireless wifi-iface", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.device='{iface.device}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.mode='{iface.mode}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.network='{uci_network}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.ssid='{effective_ssid}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.encryption='{net.encryption}'", dry_run)
            if net.key:
                _run_or_print(router.ssh, f"uci set {pf}.key='{net.key}'", dry_run)
            _run_or_print(
                router.ssh,
                f"uci set {pf}.hidden='{'1' if net.hidden else '0'}'",
                dry_run,
            )

            if not net.ieee80211r:
                if dry_run:
                    print("    # 802.11r disabled")
                continue

            # 802.11r group data
            gd = group_data.get((net.ssid, iface.band))
            if not gd:
                print(f"    ⚠  Not enough APs with known BSSIDs for "
                      f"'{net.ssid}' ({band_label}) – skipping 802.11r")
                continue

            # ── 802.11r Fast Transition ────────────────────────────────
            if dry_run:
                print("    # 802.11r Fast Transition")
            _run_or_print(router.ssh, f"uci set {pf}.ieee80211r='1'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.mobility_domain='{gd['mobility_domain']}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.ft_over_ds='0'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.ft_psk_generate_local='0'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.pmk_r1_push='1'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.nasid='{iface.nasid}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.r1_key_holder='{iface.nasid}'", dry_run)

            # r0kh / r1kh peer lists
            if dry_run:
                print("    # r0kh / r1kh peer lists")
            for entry in gd['r0kh_list']:
                _run_or_print(router.ssh, f"uci add_list {pf}.r0kh='{entry}'", dry_run)
            for entry in gd['r1kh_list']:
                _run_or_print(router.ssh, f"uci add_list {pf}.r1kh='{entry}'", dry_run)

            # ── 802.11k RRM ───────────────────────────────────────────
            if net.ieee80211k:
                if dry_run:
                    print("    # 802.11k RRM")
                _run_or_print(router.ssh, f"uci set {pf}.ieee80211k='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.rrm_neighbor_report='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.rrm_beacon_report='1'", dry_run)

            # ── 802.11v BSS Transition Management ─────────────────────
            if net.ieee80211v:
                if dry_run:
                    print("    # 802.11v BSS Transition Management")
                _run_or_print(router.ssh, f"uci set {pf}.ieee80211v='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.bss_transition='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.wnm_sleep_mode='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.wnm_sleep_mode_no_keys='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.proxy_arp='1'", dry_run)

        print(f"\n  ✓ {router.host} – UCI changes staged (not committed)")
    print("")


# ---------------------------------------------------------------------------
# Test mode – fabricated data for a dry-run preview
# ---------------------------------------------------------------------------

def _generate_test_data():
    """Return (routers, networks) with made-up but realistic data.

    Three routers, two SSIDs per router (2.4 GHz + 5 GHz), so you can
    see the full r0kh/r1kh mesh output without any real SSH connection.
    """
    test_routers_raw = [
        {'host': '192.168.1.1', 'bssids_2g': 'A4:CF:12:D0:00:01', 'bssids_5g': 'A4:CF:12:D0:10:01'},
        {'host': '192.168.1.2', 'bssids_2g': 'A4:CF:12:D0:00:02', 'bssids_5g': 'A4:CF:12:D0:10:02'},
        {'host': '192.168.1.3', 'bssids_2g': 'A4:CF:12:D0:00:03', 'bssids_5g': 'A4:CF:12:D0:10:03'},
    ]

    test_networks = [
        NetworkConfig(ssid='MyHome-WiFi',   key='test-home-pass',  encryption='sae',       network='',     ieee80211r=True,  ieee80211k=True,  ieee80211v=True,  hidden=False),
        NetworkConfig(ssid='IoT-Devices',   key='test-iot-pass',   encryption='sae-mixed', network='iot',  ieee80211r=True,  ieee80211k=True,  ieee80211v=False, hidden=False),
        NetworkConfig(ssid='Guest-Network', key='test-guest-pass', encryption='psk2',      network='guest', ieee80211r=False, ieee80211k=False, ieee80211v=False, hidden=True),
    ]

    routers = []
    for idx, raw in enumerate(test_routers_raw):
        # We don't need a real SSH connection for test mode
        ssh = SSHConnection(raw['host'])
        router = Router(host=raw['host'], ssh=ssh)

        iface_idx = 0
        for net in test_networks:
            for band, bssid_key in [('2g', 'bssids_2g'), ('5g', 'bssids_5g')]:
                bssid = raw[bssid_key]
                # Vary the last octet per SSID so BSSIDs are unique
                parts = bssid.split(':')
                parts[-1] = f"{int(parts[-1], 16) + test_networks.index(net) * 16:02X}"
                bssid = ':'.join(parts)
                nasid = bssid.replace(':', '')

                router.interfaces.append(WifiInterface(
                    index=iface_idx,
                    ssid=net.ssid,
                    device=f"radio{'0' if band == '2g' else '1'}",
                    band=band,
                    encryption='sae-mixed' if net.ssid != 'Guest-Network' else 'psk2',
                    mode='ap',
                    network='lan' if net.ssid == 'MyHome-WiFi' else ('iot' if net.ssid == 'IoT-Devices' else 'guest'),
                    bssid=bssid,
                    nasid=nasid,
                    ifname=f"wlan{iface_idx}",
                ))
                iface_idx += 1

        routers.append(router)

    return routers, test_networks


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            'OpenWRT 802.11r/k/v Roaming Configuration Helper\n'
            '\n'
            'Reads router and network definitions from a YAML config file,\n'
            'connects via SSH to discover interfaces, deletes matching\n'
            'wifi-iface sections and recreates them with the correct\n'
            'roaming configuration — all via SSH.\n'
            '\n'
            'Changes are staged but NOT committed. After running, review\n'
            'with "uci changes wireless" on each router, then commit\n'
            'with "uci commit wireless" and "wifi reload".\n'
            '\n'
            'Features configured per-network:\n'
            '  802.11r  Fast Transition (NAS ID, mobility domain, r0kh, r1kh)\n'
            '  802.11k  RRM (Neighbour Report, Beacon Report)\n'
            '  802.11v  BSS Transition, WNM Sleep Mode, ProxyARP'
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        '--config', '-c',
        default='config.yaml',
        help='Path to YAML config file (default: config.yaml)',
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Print UCI commands without executing them',
    )
    parser.add_argument(
        '--test', '-t',
        action='store_true',
        help='Dry-run with fabricated test data (no SSH, no config needed)',
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_arguments()
    dry_run = args.dry_run or args.test

    # -- Test mode or real mode ------------------------------------------------
    if args.test:
        print("⚙  TEST MODE – using fabricated data (no SSH connections)\n")
        routers, networks = _generate_test_data()
    else:
        routers, networks = load_config(args.config)
        print(f"Loaded {len(routers)} router(s) and {len(networks)} network(s) "
              f"from {args.config}\n")

        # -- Connect & discover ------------------------------------------------
        print("Connecting to routers and discovering interfaces …")
        for router in routers:
            if not router.ssh.test_connection():
                sys.exit(1)
            print(f"  ✓ {router.host}")
            discover_interfaces(router)

    # -- Show discovered interfaces --------------------------------------------
    if not args.test:
        for router in routers:
            if not router.interfaces:
                print(f"    (no WiFi interfaces found on {router.host})")
    for router in routers:
        print(f"  ✓ {router.host}")
        for iface in router.interfaces:
            band_label = "2.4 GHz" if iface.band == '2g' else "5 GHz"
            bssid_str = iface.bssid if iface.bssid else "unknown"
            print(f"    [{iface.index}] {iface.ssid:20s}  {band_label:8s}  "
                  f"BSSID: {bssid_str}  ({iface.encryption})  "
                  f"mode={iface.mode}  network={iface.network}")

    # -- Summarise plan --------------------------------------------------------
    print(f"\nNetworks to configure:")
    for net in networks:
        flags = []
        if net.ieee80211r:
            flags.append('r')
        if net.ieee80211k:
            flags.append('k')
        if net.ieee80211v:
            flags.append('v')
        label = ', '.join(f"802.11{f}" for f in flags) if flags else 'roaming disabled'
        if net.hidden:
            label += ', hidden'
        net_label = f", network={net.network}" if net.network else ''
        print(f"  • {net.ssid:20s}  [{label}{net_label}]")

    # -- Apply configuration ---------------------------------------------------
    if dry_run:
        print("\n⚙  DRY RUN – commands that would be executed:")
    else:
        print("\nApplying UCI configuration via SSH …")

    apply_config(routers, networks, dry_run=dry_run)

    print("✓ Done!")
    if dry_run:
        print("  (dry run – no changes were made)")
    else:
        print("  Changes staged on all routers. On each router, run:")
        print("    uci changes wireless   # review")
        print("    uci commit wireless    # commit")
        print("    wifi reload            # apply\n")


if __name__ == '__main__':
    main()
