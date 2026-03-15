#!/usr/bin/env python3
"""OpenWRT 802.11r/k/v Roaming Configuration Helper

Reads router and network definitions from a YAML config file,
connects via SSH to discover WiFi interfaces and BSSIDs, then
applies UCI commands directly on each router via SSH.

The script reviews staged changes with ``uci changes wireless``
before committing each phase with ``uci commit wireless`` and
``wifi reload``.

Usage:
    python3 main.py                       # uses config.yaml
    python3 main.py --config my.yaml      # custom config path
    python3 main.py --dry-run             # show commands without executing
    python3 main.py --test                # dry-run with fabricated data
    python3 main.py --test --output       # also write output to a log file
"""

import argparse
import logging
import sys
import time
from datetime import datetime

from apply import apply_base_config, apply_roaming_config
from config_loader import load_config
from discovery import discover_interfaces, discover_radios
from models import WifiInterface
from testdata import generate_test_data
from utils import random_hex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UCI indexing explanation
# ---------------------------------------------------------------------------

def _log_uci_indexing_note():
    """Print a short note explaining the UCI numbering difference."""
    logger.info("")
    logger.info("  ┌─────────────────────────────────────────────────────────────┐")
    logger.info("  │  NOTE: UCI uses two different numbering schemes             │")
    logger.info("  │                                                             │")
    logger.info("  │  ● @wifi-iface[N]  — positional index (0-based).            │")
    logger.info("  │    Counts all wifi-iface sections in order; indices shift   │")
    logger.info("  │    when sections are added or deleted.                      │")
    logger.info("  │    → Used by: uci get/set/delete commands (this script).    │")
    logger.info("  │                                                             │")
    logger.info("  │  ● wireless.wifinetN — auto-assigned section name.          │")
    logger.info("  │    Persistent, only increments; does NOT shift on delete.   │")
    logger.info("  │    Default interfaces use named sections (default_radioN),  │")
    logger.info("  │    so wifinetN numbering starts after those.                │")
    logger.info("  │    → Shown in: uci changes / uci show output (above).       │")
    logger.info("  │                                                             │")
    logger.info("  │  The indices above may not match the [N] numbers logged     │")
    logger.info("  │  during creation/deletion — this is expected.               │")
    logger.info("  └─────────────────────────────────────────────────────────────┘")


# ---------------------------------------------------------------------------
# Dry-run interface simulation
# ---------------------------------------------------------------------------

def _simulate_interfaces_after_reload(routers, networks, protected_ssids):
    """Build synthetic interface lists as they would look after phase 1.

    For dry-run mode: after phase 1 creates base interfaces and
    wifi is reloaded, each router would have interfaces with BSSIDs
    assigned by the hardware.  We simulate this by:

    1. Keeping any existing discovered interface that matches a managed
       SSID (preserving its real BSSID if known).
    2. Adding placeholder interfaces for (SSID, band) combos that
       don't exist yet — with generated placeholder BSSIDs.
    3. Giving a placeholder BSSID to any interface missing one.

    The routers' ``.interfaces`` lists are replaced in-place.
    """
    managed_ssids = set()
    for net in networks:
        managed_ssids.add(net.ssid.lower())
        if not net.ieee80211r:
            for n in range(1, len(routers) + 1):
                managed_ssids.add(f"{net.ssid}-r{n}".lower())

    net_by_ssid = {}
    for net in networks:
        net_by_ssid[net.ssid.lower()] = net
        if not net.ieee80211r:
            for n in range(1, len(routers) + 1):
                net_by_ssid[f"{net.ssid}-r{n}".lower()] = net

    for router_num, router in enumerate(routers, 1):
        new_ifaces = []
        idx = 0

        # Keep protected interfaces first (they wouldn't be deleted)
        for iface in router.interfaces:
            if iface.ssid.lower() in protected_ssids:
                iface.index = idx
                new_ifaces.append(iface)
                idx += 1

        # Track which (ssid, band) combos we've handled
        handled = set()

        # Re-add managed interfaces in config order
        for net in networks:
            if net.ssid.lower() in protected_ssids:
                continue

            for device, band in sorted(
                router.radios.items(),
                key=lambda x: (0 if x[1] == '2g' else 1),
            ):
                if (net.ssid.lower(), band) in handled:
                    continue
                handled.add((net.ssid.lower(), band))

                effective_ssid = net.ssid if net.ieee80211r else f"{net.ssid}-r{router_num}"

                # Try to find an existing interface to reuse its BSSID
                existing = None
                for iface in router.interfaces:
                    if (iface.ssid.lower() == net.ssid.lower()
                            or iface.ssid.lower() == effective_ssid.lower()):
                        if iface.band == band and iface.bssid:
                            existing = iface
                            break

                if existing and existing.bssid:
                    bssid = existing.bssid
                else:
                    # Generate a placeholder BSSID (locally administered)
                    bssid = "02:%s:%s:%s:%s:%s" % (
                        random_hex(1), random_hex(1), random_hex(1),
                        random_hex(1), random_hex(1),
                    )

                nasid = bssid.replace(':', '')
                uci_network = net.network if net.network else 'lan'

                new_ifaces.append(WifiInterface(
                    index=idx,
                    ssid=effective_ssid,
                    device=device,
                    band=band,
                    encryption=net.encryption,
                    mode='ap',
                    network=uci_network,
                    bssid=bssid,
                    nasid=nasid,
                ))
                idx += 1

        router.interfaces = new_ifaces

    # Log the simulated state
    logger.info("\n  Simulated interfaces after phase 1 commit + reload:")
    for router in routers:
        logger.info("  ✓ %s", router.host)
        for iface in router.interfaces:
            band_label = "2.4 GHz" if iface.band == '2g' else "5 GHz"
            logger.info("    [%d] %-20s  %-8s  BSSID: %s",
                        iface.index, iface.ssid, band_label, iface.bssid)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(output_path=None):
    """Configure the root logger with console and optional file handlers.

    Console handler: INFO level, ``%(message)s`` format (preserves the
    current look of the script's output).

    File handler (when *output_path* is given): DEBUG level with
    timestamps and log-level prefixes — captures everything including
    SSH commands.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console — INFO+ with bare messages
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(message)s'))
    root.addHandler(console)

    # File — DEBUG+ with full detail
    if output_path:
        fh = logging.FileHandler(output_path, mode='w', encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)-8s %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        root.addHandler(fh)
        logger.info("Logging to file: %s", output_path)


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
            'Each phase reviews staged changes (uci changes wireless)\n'
            'before committing with "uci commit wireless" and\n'
            '"wifi reload".\n'
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
    parser.add_argument(
        '--output', '-o',
        nargs='?',
        const='auto',
        default=None,
        metavar='FILE',
        help=('Write all output to a log file (DEBUG level).\n'
              'If no filename is given, auto-generates a timestamped name.'),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_arguments()
    dry_run = args.dry_run or args.test

    # Resolve output path
    output_path = None
    if args.output is not None:
        if args.output == 'auto':
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = f"openwrt_roaming_{ts}.log"
        else:
            output_path = args.output

    setup_logging(output_path)

    # -- Test mode or real mode ------------------------------------------------
    if args.test:
        logger.info("⚙  TEST MODE – using fabricated data (no SSH connections)\n")
        routers, networks = generate_test_data()
        protected_ssids = set()
    else:
        routers, networks, protected_ssids = load_config(args.config)
        logger.info("Loaded %d router(s) and %d network(s) from %s\n",
                     len(routers), len(networks), args.config)

        # -- Connect & discover ------------------------------------------------
        logger.info("Connecting to routers and discovering interfaces …")
        for router in routers:
            if not router.ssh.test_connection():
                sys.exit(1)
            logger.info("  ✓ %s", router.host)
            discover_interfaces(router)
            discover_radios(router)

    # -- Show discovered interfaces --------------------------------------------
    if not args.test:
        for router in routers:
            if not router.interfaces:
                logger.info("    (no WiFi interfaces found on %s)", router.host)
    for router in routers:
        logger.info("  ✓ %s", router.host)
        for iface in router.interfaces:
            band_label = "2.4 GHz" if iface.band == '2g' else "5 GHz"
            bssid_str = iface.bssid if iface.bssid else "unknown"
            logger.info("    [%d] %-20s  %-8s  BSSID: %s  (%s)  mode=%s  network=%s",
                        iface.index, iface.ssid, band_label, bssid_str,
                        iface.encryption, iface.mode, iface.network)

    # -- Summarise plan --------------------------------------------------------
    logger.info("\nNetworks to configure:")
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
        logger.info("  • %-20s  [%s%s]", net.ssid, label, net_label)

    # -- Phase 1: base interfaces ----------------------------------------------
    if dry_run:
        logger.info("\n\u2699  DRY RUN \u2013 Phase 1: base interface commands:")
    else:
        logger.info("\nPhase 1: Creating base interfaces via SSH \u2026")

    apply_base_config(routers, networks, dry_run=dry_run, protected_ssids=protected_ssids)

    # -- Commit, reload, rediscover (real mode only) ---------------------------
    if not dry_run:
        logger.info("\nPhase 1: Reviewing staged UCI changes \u2026")
        for router in routers:
            changes = router.ssh.run("uci changes wireless", check=False)
            logger.info("  %s uci changes wireless:", router.host)
            if changes:
                for line in changes.splitlines():
                    logger.info("    %s", line)
            else:
                logger.info("    (no changes)")

        _log_uci_indexing_note()

        confirm = input("\nPhase 1: Commit these changes? [y/N] ").strip().lower()
        if confirm != 'y':
            logger.info("Aborting phase 1 \u2013 reverting staged changes \u2026")
            for router in routers:
                router.ssh.run("uci revert wireless", check=False)
                logger.info("  %s: reverted", router.host)
            sys.exit(0)

        logger.info("Phase 1: Committing and reloading wireless on all routers \u2026")
        for router in routers:
            logger.info("  %s: uci commit wireless && wifi reload", router.host)
            router.ssh.run("uci commit wireless", check=False)
            router.ssh.run("wifi reload", check=False)

        logger.info("  Waiting for radios to initialise (15 s) \u2026")
        time.sleep(15)

        logger.info("\nRe-discovering interfaces and BSSIDs \u2026")
        for router in routers:
            discover_interfaces(router)
        for router in routers:
            logger.info("  \u2713 %s", router.host)
            for iface in router.interfaces:
                band_label = "2.4 GHz" if iface.band == '2g' else "5 GHz"
                bssid_str = iface.bssid if iface.bssid else "unknown"
                logger.info("    [%d] %-20s  %-8s  BSSID: %s",
                            iface.index, iface.ssid, band_label, bssid_str)
    else:
        # Dry-run: simulate what interfaces would look like after phase 1
        # so phase 2 can generate complete r0kh/r1kh peer lists.
        _simulate_interfaces_after_reload(routers, networks, protected_ssids)

    # -- Phase 2: roaming configuration ----------------------------------------
    if dry_run:
        logger.info("\n\u2699  DRY RUN \u2013 Phase 2: roaming configuration:")
    else:
        logger.info("\nPhase 2: Applying 802.11r/k/v roaming configuration \u2026")

    apply_roaming_config(routers, networks, dry_run=dry_run, protected_ssids=protected_ssids)

    # -- Phase 2: commit and reload (real mode only) ---------------------------
    if not dry_run:
        logger.info("Phase 2: Reviewing staged UCI changes \u2026")
        for router in routers:
            changes = router.ssh.run("uci changes wireless", check=False)
            logger.info("  %s uci changes wireless:", router.host)
            if changes:
                for line in changes.splitlines():
                    logger.info("    %s", line)
            else:
                logger.info("    (no changes)")

        _log_uci_indexing_note()

        confirm = input("\nPhase 2: Commit these changes? [y/N] ").strip().lower()
        if confirm != 'y':
            logger.info("Aborting phase 2 \u2013 reverting staged changes \u2026")
            for router in routers:
                router.ssh.run("uci revert wireless", check=False)
                logger.info("  %s: reverted", router.host)
            sys.exit(0)

        logger.info("Phase 2: Committing and reloading wireless on all routers \u2026")
        for router in routers:
            logger.info("  %s: uci commit wireless && wifi reload", router.host)
            router.ssh.run("uci commit wireless", check=False)
            router.ssh.run("wifi reload", check=False)

    logger.info("\u2713 Done!")
    if dry_run:
        logger.info("  (dry run \u2013 no changes were made)")
    else:
        logger.info("  All changes committed and applied on all routers.")


if __name__ == '__main__':
    main()
