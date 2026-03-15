# OpenWRT UCI Helper for 802.11r/k/v Roaming

> **Notice:** This project is 100% AI-generated code.
> It may contain rough edges, inconsistencies, or patterns that no human would willingly produce.
> Review before trusting it with your network.

---

A Python script that automates 802.11r/k/v (Fast Transition, RRM, BSS Transition Management) configuration across multiple OpenWRT access points. It connects to routers over SSH, discovers existing WiFi interfaces and BSSIDs, then deletes all managed wifi-iface sections and recreates them from scratch with the correct UCI settings -- including per-band r0kh/r1kh peer lists -- so you don't have to do it by hand.

## What It Does

Configuring 802.11r roaming across multiple APs is tedious. Each access point needs to know the BSSIDs and shared keys of every other AP in the same roaming group, per SSID, per band. This script handles that automatically:

1. **Discovers** existing WiFi interfaces and radio bands on each router via UCI and ubus.
2. **Phase 1** -- Deletes and recreates wifi-iface sections with base settings (SSID, encryption, key, network assignment). Commits and reloads so the hardware assigns BSSIDs.
3. **Phase 2** -- Re-discovers BSSIDs, computes the full r0kh/r1kh mesh for each (SSID, band) group, and applies 802.11r/k/v settings. Commits and reloads again.

Each phase shows staged `uci changes wireless` output and asks for confirmation before committing.

## Features Configured Per Network

- **802.11r** -- Fast Transition: mobility domain, NAS ID, r0kh/r1kh peer lists, PMK-R1 push, FT-over-DS disabled
- **802.11k** -- Radio Resource Management: neighbour report, beacon report
- **802.11v** -- BSS Transition Management, WNM sleep mode, proxy ARP

When 802.11r is disabled for a network, 802.11k and 802.11v are forced off as well. Networks without 802.11r get per-router SSID suffixes (`-r1`, `-r2`, ...) instead of a shared SSID.

## Requirements

- Python 3
- [PyYAML](https://pypi.org/project/PyYAML/) (`pip install pyyaml`)
- `ssh` and optionally `sshpass` (for password-based auth) available on PATH
- OpenWRT routers reachable over SSH

If you use Nix, a `flake.nix` is included that provides a dev shell with Python + PyYAML, OpenSSH, and sshpass.

## Setup

1. Copy the example config and edit it:

   ```
   cp config.yaml.example config.yaml
   ```

2. Define your routers (host, user, port, optional password) and networks (SSID, key, encryption, feature toggles) in `config.yaml`. See the example file for all available options.

3. Optionally list SSIDs under `protected_ssids` -- these wifi-iface sections will never be deleted or modified.

## Usage

```
python3 main.py                        # uses config.yaml
python3 main.py --config my.yaml       # custom config path
python3 main.py --dry-run              # show UCI commands without executing
python3 main.py --test                 # dry-run with fabricated test data (no SSH needed)
python3 main.py --test --output        # also write debug-level output to a timestamped log file
python3 main.py --output run.log       # real run, log everything to run.log
```

### Flags

| Flag | Short | Description |
|---|---|---|
| `--config FILE` | `-c` | Path to YAML config file (default: `config.yaml`) |
| `--dry-run` | `-n` | Print UCI commands without executing them |
| `--test` | `-t` | Dry-run with fabricated test data, no SSH or config required |
| `--output [FILE]` | `-o` | Write debug-level output to a file; auto-generates a timestamped filename if none given |

### Dry-Run Mode

`--dry-run` connects to routers and discovers interfaces but only prints the UCI commands that would be executed. `--test` goes further and skips SSH entirely, using built-in fabricated data with three routers and three networks.

## Configuration Reference

```yaml
protected_ssids:
  - my-bridge-network       # these SSIDs are never touched

routers:
  - host: 192.168.1.1
    user: root
    port: 22
    password: my-ssh-password   # omit to use SSH keys

networks:
  - ssid: home-wifi
    key: my-passphrase
    encryption: sae             # WPA3-SAE (default), sae-mixed, psk2, psk-mixed, ...
    network: lan                # UCI network name; omit to keep discovered value
    802.11r: true
    802.11k: true
    802.11v: false
    hidden: false
```

### Encryption Defaults

If `encryption` is omitted, it defaults to `sae` (WPA3-SAE).

### Network Assignment

If `network` is omitted, the script preserves whatever UCI network name was already configured on each router for that interface.

## Project Structure

| File | Purpose |
|---|---|
| `main.py` | Entry point, argument parsing, two-phase orchestration |
| `config_loader.py` | Loads and validates `config.yaml` |
| `models.py` | Data classes: `Router`, `WifiInterface`, `NetworkConfig` |
| `ssh.py` | SSH helper using ControlMaster multiplexing |
| `discovery.py` | Interface and radio discovery via UCI, ubus, iwinfo |
| `apply.py` | Phase 1 (base interfaces) and Phase 2 (roaming config) UCI commands |
| `utils.py` | Small utilities (random hex generation) |
| `testdata.py` | Fabricated test data for `--test` mode |
| `flake.nix` | Nix flake dev shell |

## How SSH Works

The script uses SSH ControlMaster multiplexing. The first connection to each router establishes a persistent master socket (10-minute timeout). All subsequent commands reuse that socket, avoiding repeated authentication. If `password` is set in the config, `sshpass` is used for the initial connection. Otherwise, standard SSH key-based or interactive authentication applies.

## Acknowledgements

Inspired by [walidmadkour/OpenWRT-UCI-helper-802.11r](https://github.com/walidmadkour/OpenWRT-UCI-helper-802.11r).

## License

No license file is included. Treat accordingly.
