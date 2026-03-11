"""YAML configuration loader."""

import logging
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required.  Install with:  pip install pyyaml")
    sys.exit(1)

from models import NetworkConfig, Router
from ssh import SSHConnection

logger = logging.getLogger(__name__)


def load_config(path):
    """Load and validate config.yaml.  Returns (routers, networks)."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        logger.error("Config file not found: %s", cfg_path)
        sys.exit(1)

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    # -- Routers ---------------------------------------------------------------
    routers_cfg = cfg.get('routers', [])
    if not routers_cfg:
        logger.error("No routers defined in config.")
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
        logger.error("No networks defined in config.")
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
            logger.warning("No 'key' defined for network '%s'", ssid)

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
