"""WiFi interface discovery via UCI, ubus, and iwinfo."""

import json
import logging
import re

from models import WifiInterface

logger = logging.getLogger(__name__)


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
            logger.warning("Could not read wifi-iface[%d] on %s: %s", i, router.host, e)

    # Fill in BSSIDs
    if not _fill_bssids_ubus(ssh, interfaces):
        _fill_bssids_iwinfo(ssh, interfaces)
    else:
        # ubus may not have had every ifname; fall back for the rest
        _fill_bssids_iwinfo(ssh, interfaces)

    router.interfaces = interfaces
    return interfaces
