"""
File:        main.py
Author:      Kevin Auberson
Created:     2026-05-10
Description: Entry point of the Carbon Signal Aggregator. Runs a polling
             loop that collects data from all sources (Electricity Maps,
             Kepler, metrics-server), combines them into a single carbon
             signal snapshot, and publishes the result to a Kubernetes
             ConfigMap consumed by the scheduler extender (Layer 1) and
             the node eligibility controller (Layer 2).
"""

import json
import time
import signal
import sys
from datetime import datetime, timezone
from kubernetes import client, config

from electricity_maps import ElectricityMaps
from kepler import Kepler
from metrics_server import MetricsServer

# Polling and ConfigMap settings
POLL_INTERVAL = 30  # seconds between each aggregation cycle
NAMESPACE = "carbon-aware"
CONFIGMAP_NAME = "carbon-signal"

# Global flag used by the shutdown handler
running = True


def handle_shutdown(signum, frame):
    """
    Signal handler for graceful shutdown.

    Captures SIGINT (Ctrl+C) and SIGTERM (sent by Kubernetes when stopping
    the pod). Sets the global 'running' flag to False so the main loop
    exits cleanly at the end of the current cycle.
    """
    global running
    print(f"\nSignal {signum} received, shutting down...")
    running = False


def build_signal(emaps, kepler, metrics):
    """
    Collect data from all sources and build a complete carbon signal.

    Each source is queried independently with try/except so that one
    failing source does not prevent the others from being collected.

    Args:
        emaps: An ElectricityMaps instance.
        kepler: A Kepler instance.
        metrics: A MetricsServer instance.

    Returns:
        A dict containing the timestamp, grid carbon intensity, and a
        list of per-node entries with watts, CO2 emission rate, CPU
        and memory usage.
    """
    # Grid carbon intensity (with fallback if the API is down)
    try:
        emaps_data = emaps.get_current()
        grid_intensity = emaps_data["carbon_intensity"]
        zone = emaps_data["zone"]
    except Exception as e:
        print(f"[WARN] Electricity Maps unavailable: {e}")
        grid_intensity = 100.0  # Approximate Swiss grid average
        zone = "CH"

    # Per-node power (best-effort)
    try:
        node_watts = kepler.get_node_watts()
    except Exception as e:
        print(f"[WARN] Kepler unavailable: {e}")
        node_watts = {}

    # Per-node CPU/RAM usage (best-effort)
    try:
        node_usage = metrics.get_node_usage()
    except Exception as e:
        print(f"[WARN] metrics-server unavailable: {e}")
        node_usage = {}

    # Combine data per node (union of names seen by both sources)
    all_nodes = set(node_watts.keys()) | set(node_usage.keys())

    nodes = []
    for name in all_nodes:
        watts = node_watts.get(name, 0.0)
        usage = node_usage.get(name, {"cpu_millicores": 0, "memory_mib": 0})

        # CO2 per second:
        #   Watts * (gCO2/kWh) / (3600 s/h * 1000 W/kW) = gCO2/s
        co2_per_second = watts * grid_intensity / (3600 * 1000)

        nodes.append({
            "name": name,
            "watts": watts,
            "co2_g_per_s": co2_per_second,
            "cpu_millicores": usage["cpu_millicores"],
            "memory_mib": usage["memory_mib"],
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zone": zone,
        "grid_intensity_g_per_kwh": grid_intensity,
        "nodes": nodes,
    }


def write_configmap(signal_data):
    """
    Write the carbon signal to its Kubernetes ConfigMap.

    Tries to update the existing ConfigMap. If it does not exist yet
    (first run), creates it instead.

    Args:
        signal_data: The dict returned by build_signal().

    Raises:
        kubernetes.client.exceptions.ApiException: If the API rejects
        both the update and the create call.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    api = client.CoreV1Api()

    cm_body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, namespace=NAMESPACE),
        data={"signal.json": json.dumps(signal_data, indent=2)},
    )

    try:
        api.replace_namespaced_config_map(CONFIGMAP_NAME, NAMESPACE, cm_body)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            # First run: ConfigMap does not exist yet, create it
            api.create_namespaced_config_map(NAMESPACE, cm_body)
        else:
            raise


def main():
    """
    Main loop: instantiate clients, then poll all sources every
    POLL_INTERVAL seconds and publish the resulting signal to K8s.
    """
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Instantiate the source clients once (kept alive for the whole loop)
    emaps = ElectricityMaps()
    kepler = Kepler()
    metrics = MetricsServer()

    print(f"Carbon Signal Aggregator started (cycle every {POLL_INTERVAL}s)")

    while running:
        try:
            data = build_signal(emaps, kepler, metrics)
            write_configmap(data)
            print(
                f"[{data['timestamp']}] "
                f"CI={data['grid_intensity_g_per_kwh']:.1f} gCO2/kWh, "
                f"{len(data['nodes'])} nodes"
            )
        except Exception as e:
            # Log and keep going — we'll try again on the next cycle
            print(f"[ERROR] Cycle failed: {e}")

        # Sleep in 1-second chunks so we react quickly to a shutdown signal
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    print("Shutdown complete.")


if __name__ == "__main__":
    main()