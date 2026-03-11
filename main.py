#!/usr/bin/env python3
"""OpenWRT 802.11r/k/v Roaming Configuration Helper

Reads router and network definitions from a YAML config file,
connects via SSH to discover WiFi interfaces and BSSIDs, then
applies UCI commands directly on each router via SSH.

The script does NOT commit changes — you review with
``uci changes wireless`` then run ``uci commit wireless``
and ``wifi reload`` on each router yourself.

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
from datetime import datetime

from apply import apply_config
from config_loader import load_config
from discovery import discover_interfaces
from testdata import generate_test_data

logger = logging.getLogger(__name__)


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
    else:
        routers, networks = load_config(args.config)
        logger.info("Loaded %d router(s) and %d network(s) from %s\n",
                     len(routers), len(networks), args.config)

        # -- Connect & discover ------------------------------------------------
        logger.info("Connecting to routers and discovering interfaces …")
        for router in routers:
            if not router.ssh.test_connection():
                sys.exit(1)
            logger.info("  ✓ %s", router.host)
            discover_interfaces(router)

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

    # -- Apply configuration ---------------------------------------------------
    if dry_run:
        logger.info("\n⚙  DRY RUN – commands that would be executed:")
    else:
        logger.info("\nApplying UCI configuration via SSH …")

    apply_config(routers, networks, dry_run=dry_run)

    logger.info("✓ Done!")
    if dry_run:
        logger.info("  (dry run – no changes were made)")
    else:
        logger.info("  Changes staged on all routers. On each router, run:")
        logger.info("    uci changes wireless   # review")
        logger.info("    uci commit wireless    # commit")
        logger.info("    wifi reload            # apply\n")


if __name__ == '__main__':
    main()
