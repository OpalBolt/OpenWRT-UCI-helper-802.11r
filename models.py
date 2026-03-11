"""Data classes used throughout the project."""

from dataclasses import dataclass, field

from ssh import SSHConnection


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
