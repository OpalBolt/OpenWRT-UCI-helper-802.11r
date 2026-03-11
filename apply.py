"""Apply roaming configuration to routers via UCI commands over SSH."""

import logging

from utils import random_hex

logger = logging.getLogger(__name__)


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
    logger.debug("UCI: %s", command)
    if dry_run:
        logger.info("    %s", command)
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
        logger.info("")
        logger.info("%s", '─' * 60)
        logger.info("  Router: %s", router.host)
        logger.info("%s", '─' * 60)

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
            logger.info("  Deleting existing wifi-iface sections …")
            for idx in indices_to_delete:
                _run_or_print(
                    router.ssh,
                    f"uci delete wireless.@wifi-iface[{idx}]",
                    dry_run,
                )
        else:
            logger.info("  (no existing wifi-iface sections to delete)")

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

            logger.info("")
            logger.info("  Creating: %s (%s) on %s [network=%s]",
                        net.ssid, band_label, iface.device, uci_network)

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
                logger.info("    # 802.11r disabled")
                continue

            # 802.11r group data
            gd = group_data.get((net.ssid, iface.band))
            if not gd:
                logger.warning("  Not enough APs with known BSSIDs for "
                               "'%s' (%s) – skipping 802.11r", net.ssid, band_label)
                continue

            # ── 802.11r Fast Transition ────────────────────────────────
            logger.info("    # 802.11r Fast Transition")
            _run_or_print(router.ssh, f"uci set {pf}.ieee80211r='1'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.mobility_domain='{gd['mobility_domain']}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.ft_over_ds='0'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.ft_psk_generate_local='0'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.pmk_r1_push='1'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.nasid='{iface.nasid}'", dry_run)
            _run_or_print(router.ssh, f"uci set {pf}.r1_key_holder='{iface.nasid}'", dry_run)

            # r0kh / r1kh peer lists
            logger.info("    # r0kh / r1kh peer lists")
            for entry in gd['r0kh_list']:
                _run_or_print(router.ssh, f"uci add_list {pf}.r0kh='{entry}'", dry_run)
            for entry in gd['r1kh_list']:
                _run_or_print(router.ssh, f"uci add_list {pf}.r1kh='{entry}'", dry_run)

            # ── 802.11k RRM ───────────────────────────────────────────
            if net.ieee80211k:
                logger.info("    # 802.11k RRM")
                _run_or_print(router.ssh, f"uci set {pf}.ieee80211k='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.rrm_neighbor_report='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.rrm_beacon_report='1'", dry_run)

            # ── 802.11v BSS Transition Management ─────────────────────
            if net.ieee80211v:
                logger.info("    # 802.11v BSS Transition Management")
                _run_or_print(router.ssh, f"uci set {pf}.ieee80211v='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.bss_transition='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.wnm_sleep_mode='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.wnm_sleep_mode_no_keys='1'", dry_run)
                _run_or_print(router.ssh, f"uci set {pf}.proxy_arp='1'", dry_run)

        logger.info("")
        logger.info("  ✓ %s – UCI changes staged (not committed)", router.host)
    logger.info("")
