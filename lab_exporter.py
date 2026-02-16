#!/usr/bin/env python3
"""
Lab Exporter — GPU Node Monitoring Agent

Discovers local hardware (CPU, memory, GPU, disks, NICs),
registers with the Lab Portal backend, pulls monitoring config,
and reports snapshots every N seconds.

Usage:
    python lab_exporter.py --server https://lab.example.com
    python lab_exporter.py --server https://lab.example.com --hostname mynode01
    python lab_exporter.py --server https://lab.example.com --nogpu
    python lab_exporter.py --server https://lab.example.com --config ./my-config.json
"""

import argparse
import json
import logging
import os
import platform
import signal
import socket
import sys
import time
from pathlib import Path

import psutil
import requests

# ── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lab-exporter")

# ── GPU support (optional) ──────────────────────────────────

try:
    import pynvml

    HAS_NVML = True
except ImportError:
    HAS_NVML = False
    log.info("pynvml not installed — GPU monitoring disabled")

# ── Constants ───────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"
DEFAULT_REPORT_INTERVAL = 5  # seconds

# ── Hardware Discovery ──────────────────────────────────────


def discover_hardware(skip_gpu: bool = False) -> dict:
    """Probe local hardware and return capabilities dict."""
    caps: dict = {}

    # CPU
    caps["cpuCores"] = psutil.cpu_count(logical=True)

    # Memory
    mem = psutil.virtual_memory()
    caps["memoryTotalGB"] = round(mem.total / (1024**3), 1)

    # GPUs
    gpus = []
    if HAS_NVML and not skip_gpu:
        try:
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8")
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                gpus.append(
                    {
                        "index": i,
                        "name": name,
                        "memoryTotalMB": round(mem_info.total / (1024**2)),
                    }
                )
        except pynvml.NVMLError as e:
            log.warning("NVML error during discovery: %s", e)
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    caps["gpus"] = gpus

    # Disks
    disks = []
    for part in psutil.disk_partitions(all=False):
        # Skip pseudo/snap filesystems
        if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append(
                {
                    "mount": part.mountpoint,
                    "totalGB": round(usage.total / (1024**3), 1),
                }
            )
        except PermissionError:
            continue
    caps["disks"] = disks

    # NICs
    nics = []
    addrs = psutil.net_if_addrs()
    for nic_name, addr_list in addrs.items():
        if nic_name == "lo":
            continue
        ipv4 = ""
        for addr in addr_list:
            if addr.family == socket.AF_INET:
                ipv4 = addr.address
                break
        if ipv4:
            nics.append({"name": nic_name, "ipv4": ipv4})
    caps["nics"] = nics

    return caps


# ── Metric Collection ───────────────────────────────────────


def get_primary_ip() -> str:
    """Get the primary IP address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def collect_snapshot(config: dict, capabilities: dict, hostname_override: str = None, skip_gpu: bool = False) -> dict:
    """Collect a monitoring snapshot based on the active config."""

    hostname = hostname_override or platform.node()
    ip = get_primary_ip()

    # CPU
    cpu_pct = psutil.cpu_percent(interval=None)
    cpu_cores = capabilities.get("cpuCores", psutil.cpu_count(logical=True))

    # Memory (total - available = actual usage, matches htop)
    mem = psutil.virtual_memory()
    mem_used_gb = round((mem.total - mem.available) / (1024**3), 1)
    mem_total_gb = round(mem.total / (1024**3), 1)

    # Load average
    try:
        load = os.getloadavg()
        load_avg = [round(load[0], 1), round(load[1], 1), round(load[2], 1)]
    except (OSError, AttributeError):
        load_avg = [0.0, 0.0, 0.0]

    # Uptime
    uptime_seconds = int(time.time() - psutil.boot_time())

    # Process count
    process_count = len(psutil.pids())

    # GPUs
    gpus = []
    gpu_indices = config.get("gpuIndices")
    if HAS_NVML and not skip_gpu:
        try:
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                # If config specifies indices, only collect those
                if gpu_indices is not None and i not in gpu_indices:
                    continue
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8")
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(
                        handle, pynvml.NVML_TEMPERATURE_GPU
                    )
                except pynvml.NVMLError:
                    temp = 0
                try:
                    power_draw = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000  # mW → W
                except pynvml.NVMLError:
                    power_draw = 0
                try:
                    power_limit = (
                        pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000
                    )
                except pynvml.NVMLError:
                    power_limit = 0

                gpus.append(
                    {
                        "index": i,
                        "name": name,
                        "utilizationPct": util.gpu,
                        "memoryUsedMB": round(mem_info.used / (1024**2)),
                        "memoryTotalMB": round(mem_info.total / (1024**2)),
                        "temperatureC": temp,
                        "powerDrawW": round(power_draw),
                        "powerLimitW": round(power_limit),
                    }
                )
        except pynvml.NVMLError as e:
            log.warning("NVML error during collection: %s", e)
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    # Disks
    disks = []
    disk_mounts = config.get("diskMounts")
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
            continue
        if disk_mounts is not None and part.mountpoint not in disk_mounts:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append(
                {
                    "mount": part.mountpoint,
                    "totalGB": round(usage.total / (1024**3), 1),
                    "usedGB": round(usage.used / (1024**3), 1),
                }
            )
        except PermissionError:
            continue

    # Network — per-NIC (rate will be filled in by NetworkRateTracker)
    nic_names_cfg = config.get("nicNames")
    nics_snapshot = []
    net_counters = psutil.net_io_counters(pernic=True)
    nic_addrs = psutil.net_if_addrs()
    for nic_name, counters in net_counters.items():
        if nic_name == "lo":
            continue
        if nic_names_cfg is not None and nic_name not in nic_names_cfg:
            continue
        # Look up IPv4 address for this NIC
        ipv4 = ""
        if nic_name in nic_addrs:
            for addr in nic_addrs[nic_name]:
                if addr.family == socket.AF_INET:
                    ipv4 = addr.address
                    break
        nics_snapshot.append({
            "name": nic_name,
            "ipv4": ipv4,
            "rxMbps": 0.0,  # placeholder, filled by NetworkRateTracker
            "txMbps": 0.0,
        })

    snapshot = {
        "hostname": hostname,
        "ip": ip,
        "cpuUsagePct": round(cpu_pct, 1),
        "cpuCores": cpu_cores,
        "memoryUsedGB": mem_used_gb,
        "memoryTotalGB": mem_total_gb,
        "gpus": gpus,
        "disks": disks,
        "nics": nics_snapshot,
        "networkRxMbps": 0.0,  # total, filled by NetworkRateTracker
        "networkTxMbps": 0.0,
        "uptimeSeconds": uptime_seconds,
        "processCount": process_count,
        "loadAvg": load_avg,
    }
    return snapshot


# ── Network Rate Tracker ────────────────────────────────────


class NetworkRateTracker:
    """Track per-NIC network bytes and compute Mbps rate between snapshots."""

    def __init__(self):
        # { nic_name: { rx: bytes, tx: bytes } }
        self._prev: dict[str, dict[str, int]] = {}
        self._prev_time = 0.0
        self._initialized = False

    def update(self, snapshot: dict, config: dict):
        """Fill in per-NIC Mbps rates and combined totals in the snapshot."""
        nic_names_cfg = config.get("nicNames")
        net_counters = psutil.net_io_counters(pernic=True)
        now = time.time()
        dt = now - self._prev_time if self._initialized and (now - self._prev_time) > 0 else 0

        total_rx = 0.0
        total_tx = 0.0

        for nic in snapshot.get("nics", []):
            name = nic["name"]
            counters = net_counters.get(name)
            if not counters:
                continue

            cur_rx = counters.bytes_recv
            cur_tx = counters.bytes_sent

            if dt > 0 and name in self._prev:
                rx_mbps = ((cur_rx - self._prev[name]["rx"]) * 8) / (dt * 1_000_000)
                tx_mbps = ((cur_tx - self._prev[name]["tx"]) * 8) / (dt * 1_000_000)
                nic["rxMbps"] = round(max(rx_mbps, 0), 1)
                nic["txMbps"] = round(max(tx_mbps, 0), 1)
            else:
                nic["rxMbps"] = 0.0
                nic["txMbps"] = 0.0

            total_rx += nic["rxMbps"]
            total_tx += nic["txMbps"]

            self._prev[name] = {"rx": cur_rx, "tx": cur_tx}

        snapshot["networkRxMbps"] = round(total_rx, 1)
        snapshot["networkTxMbps"] = round(total_tx, 1)
        self._prev_time = now
        self._initialized = True


# ── Config File Management ──────────────────────────────────


def load_local_config(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_local_config(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Config saved to %s", path)


# ── Main ────────────────────────────────────────────────────

running = True


def signal_handler(signum, frame):
    global running
    log.info("Received signal %d, shutting down...", signum)
    running = False


def main():
    global running

    parser = argparse.ArgumentParser(description="Lab Exporter — GPU Node Monitoring Agent")
    parser.add_argument(
        "--server",
        required=True,
        help="Lab Portal backend URL (e.g., https://lab.example.com)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to local config file (default: ./config.json)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Report interval in seconds (overrides server config)",
    )
    parser.add_argument(
        "--hostname",
        default=None,
        help="Override hostname (default: system FQDN from platform.node())",
    )
    parser.add_argument(
        "--nogpu",
        action="store_true",
        help="Disable GPU monitoring (for nodes without NVIDIA GPUs)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.nogpu:
        log.info("GPU monitoring disabled via --nogpu")
    if args.hostname:
        log.info("Using custom hostname: %s", args.hostname)

    server_url = args.server.rstrip("/")
    config_path = Path(args.config)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Load local config (may contain token from previous registration)
    local_cfg = load_local_config(config_path)
    token = local_cfg.get("token")

    # ── Step 1: Discover hardware ───────────────────────────
    log.info("Discovering hardware...")
    capabilities = discover_hardware(skip_gpu=args.nogpu)
    log.info(
        "Found: %d CPU cores, %.1f GB RAM, %d GPUs, %d disks, %d NICs",
        capabilities["cpuCores"],
        capabilities["memoryTotalGB"],
        len(capabilities["gpus"]),
        len(capabilities["disks"]),
        len(capabilities["nics"]),
    )

    # ── Step 2: Register (if no token) ──────────────────────
    if not token:
        hostname = args.hostname or platform.node()
        log.info("No token found, registering as '%s'...", hostname)
        try:
            resp = requests.post(
                f"{server_url}/api/monitoring/register",
                json={"hostname": hostname, "capabilities": capabilities},
                timeout=10,
            )
            data = resp.json()
            if resp.status_code == 200:
                token = data["token"]
                local_cfg["token"] = token
                local_cfg["server"] = server_url
                save_local_config(config_path, local_cfg)
                log.info("Registered successfully (status: %s)", data.get("status"))
            elif resp.status_code == 409:
                log.error(
                    "Node '%s' is already registered. "
                    "Ask admin to delete the node or reset the token, "
                    "then delete %s and restart.",
                    hostname,
                    config_path,
                )
                sys.exit(1)
            else:
                log.error("Registration failed: %s", data)
                sys.exit(1)
        except requests.RequestException as e:
            log.error("Failed to connect to server: %s", e)
            sys.exit(1)

    headers = {"Authorization": f"Bearer {token}"}

    # ── Step 3: Pull monitoring config from server ──────────
    log.info("Pulling monitoring config from server...")
    monitor_config: dict = {}
    retry_count = 0
    while running:
        try:
            resp = requests.get(
                f"{server_url}/api/monitoring/config",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                monitor_config = resp.json()
                log.info("Config received: %s", json.dumps(monitor_config))
                break
            elif resp.status_code == 401:
                log.error(
                    "Token rejected (401). Delete %s and restart to re-register.",
                    config_path,
                )
                sys.exit(1)
            else:
                log.warning("Config fetch returned %d, retrying...", resp.status_code)
        except requests.RequestException as e:
            log.warning("Config fetch failed: %s, retrying...", e)

        retry_count += 1
        if retry_count > 60:
            log.warning("Still waiting for config after %d retries...", retry_count)
        time.sleep(5)

    if not running:
        return

    # Determine report interval
    interval = args.interval or monitor_config.get("reportIntervalSec", DEFAULT_REPORT_INTERVAL)
    log.info("Starting report loop (every %d seconds)...", interval)

    # Initialize CPU percent tracker (first call returns meaningless value)
    psutil.cpu_percent(interval=None)

    net_tracker = NetworkRateTracker()
    # Prime the network tracker with a real snapshot so _prev byte counters are set
    prime_snapshot = collect_snapshot(monitor_config, capabilities, hostname_override=args.hostname, skip_gpu=args.nogpu)
    net_tracker.update(prime_snapshot, monitor_config)

    # ── Step 4: Report loop ─────────────────────────────────
    known_config_version = -1  # will be set on first report response
    while running:
        need_config_refresh = False

        try:
            snapshot = collect_snapshot(monitor_config, capabilities, hostname_override=args.hostname, skip_gpu=args.nogpu)
            net_tracker.update(snapshot, monitor_config)

            resp = requests.post(
                f"{server_url}/api/monitoring/report",
                json=snapshot,
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                log.debug("Report sent OK")
                # Check configVersion returned by server
                data = resp.json()
                server_version = data.get("configVersion", 0)
                if known_config_version < 0:
                    # First report — just record the version
                    known_config_version = server_version
                elif server_version != known_config_version:
                    log.info("Config version changed (%d → %d), refreshing config...", known_config_version, server_version)
                    known_config_version = server_version
                    need_config_refresh = True
            elif resp.status_code == 401:
                log.error("Token rejected. Delete %s and re-register.", config_path)
                sys.exit(1)
            else:
                log.warning("Report returned %d: %s", resp.status_code, resp.text)
        except requests.RequestException as e:
            log.warning("Report failed: %s", e)

        # Refresh config when server signals a version change
        if need_config_refresh:
            try:
                resp = requests.get(
                    f"{server_url}/api/monitoring/config",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    new_config = resp.json()
                    if new_config != monitor_config:
                        monitor_config = new_config
                        log.info("Config updated: %s", json.dumps(monitor_config))
                        new_interval = monitor_config.get("reportIntervalSec", DEFAULT_REPORT_INTERVAL)
                        if args.interval is None and new_interval != interval:
                            interval = new_interval
                            log.info("Report interval changed to %d seconds", interval)
            except requests.RequestException:
                pass

        time.sleep(interval)

    log.info("Exporter stopped.")


if __name__ == "__main__":
    main()
