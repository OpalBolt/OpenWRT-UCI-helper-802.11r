# OpenWRT UCI Helper for 802.11r/k/v Roaming

> **Disclaimer:** This project is AI-generated slop. The code was written with
> heavy AI assistance. Use at your own risk, read it before you run it, and
> don't blame anyone but yourself if it breaks your routers.

---

## What It Does

A Python script that configures 802.11r (Fast Transition), 802.11k (RRM), and
802.11v (BSS Transition Management) across multiple OpenWRT access points.

It reads router and network definitions from a YAML config file, connects to
each router over SSH to discover WiFi interfaces and BSSIDs, then deletes
matching `wifi-iface` sections and recreates them with the correct roaming
settings -- all via UCI commands executed over SSH.

Changes are **staged but not committed**. You review and commit manually.

## Features

- Automatic discovery of WiFi interfaces and BSSIDs via `ubus` and `iwinfo`.
- Per-network toggles for 802.11r, 802.11k, and 802.11v.
- Generates mobility domain, FT keys, and r0kh/r1kh peer lists for the full
  AP mesh.
- Supports WPA3-SAE, SAE-mixed, PSK2, and other encryption modes.
- Per-network UCI network assignment (e.g. `lan`, `guest`, `iot`).
- Hidden SSID support.
- Non-roaming networks automatically get a per-router SSID suffix (`-r1`,
  `-r2`, etc.) when 802.11r is disabled.
- Dry-run mode to preview commands without touching anything.
- Test mode with fabricated data -- no SSH, no config file needed.

## Requirements

- Python 3 with [PyYAML](https://pypi.org/project/PyYAML/)
- SSH access (key-based) to your OpenWRT routers
- OpenWRT routers with WiFi already broadly configured (the script manages
  `wifi-iface` sections, not radio setup)

### Nix

A `flake.nix` is provided. Run `nix develop` to get a shell with Python,
PyYAML, and OpenSSH.

### pip

```
pip install pyyaml
```

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit it.

```yaml
routers:
  - host: 192.168.1.1
    user: root        # default: root
    port: 22          # default: 22
  - host: 192.168.1.2

networks:
  - ssid: MyHome-WiFi
    key: my-secret-passphrase
    encryption: sae          # default: sae (WPA3-SAE)
    network: lan             # UCI network name; omit to keep discovered value
    802.11r: true            # default: true
    802.11k: true            # default: true
    802.11v: true            # default: true
    hidden: false            # default: false

  - ssid: IoT-Devices
    key: another-passphrase
    encryption: sae-mixed
    network: iot
    802.11r: true
    802.11k: true
    802.11v: false

  - ssid: Guest-Network
    key: guest-pass
    encryption: psk2
    network: guest
    802.11r: false           # disabling 802.11r forces k and v off too
    hidden: true
```

### Notes

- If `802.11r` is set to `false` for a network, `802.11k` and `802.11v` are
  forced to `false` as well.
- If `network` is omitted, the script keeps whatever UCI network name was
  already configured on the router for that interface.
- At least two APs with known BSSIDs on the same band are required for
  802.11r configuration to be generated for a given SSID/band pair.

## Usage

```
# Normal run (uses config.yaml, applies via SSH)
python3 helper.py

# Custom config file
python3 helper.py --config my.yaml

# Dry run -- prints UCI commands without executing
python3 helper.py --dry-run

# Test mode -- dry run with fabricated data, no SSH or config needed
python3 helper.py --test
```

## After Running

The script stages UCI changes but does **not** commit them. On each router:

```
uci changes wireless   # review staged changes
uci commit wireless    # commit
wifi reload            # apply
```

## How It Works

1. Connects to each router over SSH.
2. Discovers all `wifi-iface` sections via `uci show wireless`.
3. Retrieves BSSIDs using `ubus call network.wireless status` (with
   `iwinfo` as fallback).
4. Determines band (2.4 GHz / 5 GHz / 6 GHz) from the `band` or `hwmode`
   UCI option on each radio device.
5. For each managed SSID, deletes existing `wifi-iface` sections (highest
   index first to avoid index shifting).
6. Recreates each interface with `uci add` + `uci set`, including:
   - 802.11r: mobility domain, NAS ID, r1 key holder, FT keys, r0kh/r1kh
     peer lists, PMK-R1 push, FT-over-DS disabled.
   - 802.11k: neighbor report, beacon report.
   - 802.11v: BSS transition, WNM sleep mode, proxy ARP.

## License

None specified. Do whatever you want with it.
