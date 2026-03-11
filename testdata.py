"""Fabricated test data for dry-run previews (no SSH required)."""

from models import NetworkConfig, Router, WifiInterface
from ssh import SSHConnection


def generate_test_data():
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
