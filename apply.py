"""Apply roaming configuration to routers via UCI commands over SSH.

Two-phase approach:
  Phase 1 — Create base wifi-iface sections (no roaming settings).
            After committing and reloading, radios assign BSSIDs.
  Phase 2 — Apply 802.11r/k/v configuration using discovered BSSIDs
            for proper r0kh/r1kh peer lists.
"""

import logging

from utils import random_hex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_group_data(routers, networks):
    """Pre-compute shared secrets & r0kh/r1kh lists per (SSID, band).

    Returns a dict keyed by (ssid, band) with values:
        {mobility_domain, ft_key, r0kh_list, r1kh_list}
    Only populated for networks where 802.11r is enabled.
    Requires BSSIDs to be known (call after wifi reload).
    """
    group_data = {}

    for net in networks:
        if not net.ieee80211r:
            continue

        by_band = {}  # band -> [(router, iface), ...]
        for router in routers:
            for iface in router.interfaces:
                if iface.ssid.lower() == net.ssid.lower() and iface.bssid:
                    by_band.setdefault(iface.band, []).append((router, iface))

        for band, members in by_band.items():
            mobility_domain = random_hex(2)
            ft_key = random_hex(16)
            r0kh_list = [f"{i.bssid},{i.nasid},{ft_key}" for _, i in members]
            r1kh_list = [f"{i.bssid},{i.bssid},{ft_key}" for _, i in members]
            group_data[(net.ssid, band)] = {
                'mobility_domain': mobility_domain,
                'ft_key': ft_key,
                'r0kh_list': r0kh_list,
                'r1kh_list': r1kh_list,
            }

    return group_data


def _run_or_print(ssh, command, dry_run):
    """Execute *command* via SSH, or just print it when *dry_run* is True."""
    logger.debug("UCI: %s", command)
    if dry_run:
        logger.info("    %s", command)
    else:
        ssh.run(command, check=False)


def _managed_ssids(networks, num_routers):
    """Return the lowercased set of SSIDs we manage (including -rN suffixes)."""
    configured = set()
    for net in networks:
        configured.add(net.ssid.lower())
        if not net.ieee80211r:
            for n in range(1, num_routers + 1):
                configured.add(f"{net.ssid}-r{n}".lower())
    return configured


def _net_by_ssid_map(networks, num_routers):
    """Build a lowercased-ssid -> NetworkConfig lookup."""
    net_by_ssid = {}
    for net in networks:
        net_by_ssid[net.ssid.lower()] = net
        if not net.ieee80211r:
            for n in range(1, num_routers + 1):
                net_by_ssid[f"{net.ssid}-r{n}".lower()] = net
    return net_by_ssid


# ---------------------------------------------------------------------------
# Phase 1 -- base wifi-iface creation
# ---------------------------------------------------------------------------

def _create_base_wifi_iface(router_ssh, *, net, device, band, mode,
                            uci_network, effective_ssid, dry_run,
                            is_new=False):
    """Create a wifi-iface with basic settings (no roaming)."""
    band_label = "2.4 GHz" if band == '2g' else "5 GHz"
    new_tag = " (new)" if is_new else ""

    logger.info("")
    logger.info("  Creating%s: %s (%s) on %s [network=%s]",
                new_tag, net.ssid, band_label, device, uci_network)

    pf = "wireless.@wifi-iface[-1]"

    _run_or_print(router_ssh, "uci add wireless wifi-iface", dry_run)
    _run_or_print(router_ssh, f"uci set {pf}.device='{device}'", dry_run)
    _run_or_print(router_ssh, f"uci set {pf}.mode='{mode}'", dry_run)
    _run_or_print(router_ssh, f"uci set {pf}.network='{uci_network}'", dry_run)
    _run_or_print(router_ssh, f"uci set {pf}.ssid='{effective_ssid}'", dry_run)
    _run_or_print(router_ssh, f"uci set {pf}.encryption='{net.encryption}'", dry_run)
    if net.key:
        _run_or_print(router_ssh, f"uci set {pf}.key='{net.key}'", dry_run)
    _run_or_print(
        router_ssh,
        f"uci set {pf}.hidden='{'1' if net.hidden else '0'}'",
        dry_run,
    )


def apply_base_config(routers, networks, dry_run=False, protected_ssids=None):
    """Phase 1: Delete + recreate wifi-iface sections with basic settings.

    Creates interfaces with device, mode, network, ssid, encryption,
    key, and hidden -- but no 802.11r/k/v configuration.  After
    committing and reloading wifi, BSSIDs will be assigned by the
    hardware and can be discovered for phase 2.
    """
    if protected_ssids is None:
        protected_ssids = set()

    configured_ssids = _managed_ssids(networks, len(routers))

    for router_num, router in enumerate(routers, 1):
        logger.info("")
        logger.info("%s", "\u2500" * 60)
        logger.info("  Router: %s  (phase 1 \u2013 base interfaces)", router.host)
        logger.info("%s", "\u2500" * 60)

        # -- 1. Collect indices of wifi-iface sections we manage -----------
        indices_to_delete = sorted(
            [i.index for i in router.interfaces
             if i.ssid.lower() in configured_ssids
             and i.ssid.lower() not in protected_ssids],
            reverse=True,
        )
        ssid_by_index = {i.index: i.ssid for i in router.interfaces}

        ifaces_to_recreate = [
            i for i in router.interfaces
            if i.ssid.lower() in configured_ssids
            and i.ssid.lower() not in protected_ssids
        ]

        # Log protected
        skipped = [i for i in router.interfaces if i.ssid.lower() in protected_ssids]
        if skipped:
            logger.info("  Skipping protected wifi-iface sections:")
            for iface in skipped:
                band_label = "2.4 GHz" if iface.band == '2g' else "5 GHz"
                logger.info("    \u2298 [%d] %s (%s)", iface.index, iface.ssid, band_label)

        # -- 2. Delete existing matching sections (reverse order) ----------
        if indices_to_delete:
            logger.info("  Deleting existing wifi-iface sections \u2026")
            for idx in indices_to_delete:
                logger.info("    \u2717 [%d] %s", idx, ssid_by_index.get(idx, '?'))
                _run_or_print(
                    router.ssh,
                    f"uci delete wireless.@wifi-iface[{idx}]",
                    dry_run,
                )
        else:
            logger.info("  (no existing wifi-iface sections to delete)")

        # -- 3. Recreate each existing interface --------------------------
        net_by_ssid = _net_by_ssid_map(networks, len(routers))

        ordered_ifaces = sorted(
            ifaces_to_recreate,
            key=lambda i: (
                [n.ssid for n in networks].index(net_by_ssid[i.ssid.lower()].ssid),
                0 if i.band == '2g' else 1,
            ),
        )

        handled_combos = set()

        for iface in ordered_ifaces:
            net = net_by_ssid[iface.ssid.lower()]
            uci_network = net.network if net.network else iface.network
            effective_ssid = net.ssid if net.ieee80211r else f"{net.ssid}-r{router_num}"

            _create_base_wifi_iface(
                router.ssh,
                net=net,
                device=iface.device,
                band=iface.band,
                mode=iface.mode,
                uci_network=uci_network,
                effective_ssid=effective_ssid,
                dry_run=dry_run,
            )
            handled_combos.add((net.ssid.lower(), iface.band))

        # -- 4. Create interfaces for networks missing from this router ----
        for net in networks:
            if net.ssid.lower() in protected_ssids:
                continue
            for device, band in sorted(
                router.radios.items(),
                key=lambda x: (0 if x[1] == '2g' else 1),
            ):
                if (net.ssid.lower(), band) in handled_combos:
                    continue
                handled_combos.add((net.ssid.lower(), band))

                uci_network = net.network if net.network else 'lan'
                effective_ssid = net.ssid if net.ieee80211r else f"{net.ssid}-r{router_num}"

                _create_base_wifi_iface(
                    router.ssh,
                    net=net,
                    device=device,
                    band=band,
                    mode='ap',
                    uci_network=uci_network,
                    effective_ssid=effective_ssid,
                    dry_run=dry_run,
                    is_new=True,
                )

        logger.info("")
        logger.info("  \u2713 %s \u2013 base interfaces staged", router.host)
    logger.info("")


# ---------------------------------------------------------------------------
# Phase 2 -- roaming configuration (802.11r/k/v)
# ---------------------------------------------------------------------------

def _apply_roaming_to_iface(router_ssh, *, index, net, band, nasid,
                            group_data, dry_run):
    """Apply 802.11r/k/v settings to an existing wifi-iface by UCI index."""
    band_label = "2.4 GHz" if band == '2g' else "5 GHz"
    pf = f"wireless.@wifi-iface[{index}]"

    logger.info("")
    logger.info("  Configuring roaming: %s (%s) [@wifi-iface[%d]]",
                net.ssid, band_label, index)

    # -- 802.11r Fast Transition -------------------------------------------
    if net.ieee80211r:
        gd = group_data.get((net.ssid, band))
        if not gd:
            logger.warning("    \u26a0 No 802.11r group data for '%s' (%s) "
                          "\u2013 skipping 802.11r", net.ssid, band_label)
        else:
            logger.info("    # 802.11r Fast Transition")
            _run_or_print(router_ssh, f"uci set {pf}.ieee80211r='1'", dry_run)
            _run_or_print(router_ssh, f"uci set {pf}.mobility_domain='{gd['mobility_domain']}'", dry_run)
            _run_or_print(router_ssh, f"uci set {pf}.ft_over_ds='0'", dry_run)
            _run_or_print(router_ssh, f"uci set {pf}.ft_psk_generate_local='0'", dry_run)
            _run_or_print(router_ssh, f"uci set {pf}.pmk_r1_push='1'", dry_run)
            _run_or_print(router_ssh, f"uci set {pf}.nasid='{nasid}'", dry_run)
            _run_or_print(router_ssh, f"uci set {pf}.r1_key_holder='{nasid}'", dry_run)

            logger.info("    # r0kh / r1kh peer lists (%d entries)", len(gd['r0kh_list']))
            for entry in gd['r0kh_list']:
                _run_or_print(router_ssh, f"uci add_list {pf}.r0kh='{entry}'", dry_run)
            for entry in gd['r1kh_list']:
                _run_or_print(router_ssh, f"uci add_list {pf}.r1kh='{entry}'", dry_run)

    # -- 802.11k RRM -------------------------------------------------------
    if net.ieee80211k:
        logger.info("    # 802.11k RRM")
        _run_or_print(router_ssh, f"uci set {pf}.ieee80211k='1'", dry_run)
        _run_or_print(router_ssh, f"uci set {pf}.rrm_neighbor_report='1'", dry_run)
        _run_or_print(router_ssh, f"uci set {pf}.rrm_beacon_report='1'", dry_run)

    # -- 802.11v BSS Transition Management ---------------------------------
    if net.ieee80211v:
        logger.info("    # 802.11v BSS Transition Management")
        _run_or_print(router_ssh, f"uci set {pf}.ieee80211v='1'", dry_run)
        _run_or_print(router_ssh, f"uci set {pf}.bss_transition='1'", dry_run)
        _run_or_print(router_ssh, f"uci set {pf}.wnm_sleep_mode='1'", dry_run)
        _run_or_print(router_ssh, f"uci set {pf}.wnm_sleep_mode_no_keys='1'", dry_run)
        _run_or_print(router_ssh, f"uci set {pf}.proxy_arp='1'", dry_run)


def apply_roaming_config(routers, networks, dry_run=False, protected_ssids=None):
    """Phase 2: Apply 802.11r/k/v settings using discovered BSSIDs.

    Must be called after interfaces have been committed and reloaded
    so that BSSIDs are known.  Uses ``uci set`` on existing
    wifi-iface sections identified by their UCI index.
    """
    if protected_ssids is None:
        protected_ssids = set()

    group_data = _build_group_data(routers, networks)
    net_by_ssid = _net_by_ssid_map(networks, len(routers))

    for router in routers:
        logger.info("")
        logger.info("%s", "\u2500" * 60)
        logger.info("  Router: %s  (phase 2 \u2013 roaming configuration)", router.host)
        logger.info("%s", "\u2500" * 60)

        for iface in router.interfaces:
            if iface.ssid.lower() not in net_by_ssid:
                continue
            if iface.ssid.lower() in protected_ssids:
                continue

            net = net_by_ssid[iface.ssid.lower()]

            if not net.ieee80211r and not net.ieee80211k and not net.ieee80211v:
                continue

            _apply_roaming_to_iface(
                router.ssh,
                index=iface.index,
                net=net,
                band=iface.band,
                nasid=iface.nasid,
                group_data=group_data,
                dry_run=dry_run,
            )

        logger.info("")
        logger.info("  \u2713 %s \u2013 roaming configuration staged (not committed)", router.host)
    logger.info("")
