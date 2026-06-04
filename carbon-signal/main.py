"""
File:        main.py
Author:      Kevin Auberson
Created:     2026-05-21
Description: Entry point of the Carbon Signal Aggregator. Polls all data
             sources every POLL_INTERVAL seconds and publishes the result
             to a Kubernetes ConfigMap consumed by the scheduler extender
             (Layer 1) and the node eligibility controller (Layer 2).

             Data architecture:
             - Watts per K8s node    -> vSphere (real ESXi measurement)
             - CPU/RAM per K8s node  -> metrics-server
             - Watts per pod         -> Kepler (complementary, observability)
             - Grid carbon intensity -> Electricity Maps
"""

import json
import os
import signal
import time
from datetime import UTC, datetime

import yaml
from dotenv import load_dotenv
from electricity_maps import ElectricityMaps
from kubernetes import client, config
from metrics_server import MetricsServer
from vsphere import VSphere

# Load environment variables before any other import that depends on them
load_dotenv()

# Polling and ConfigMap settings
POLL_INTERVAL = 30
NAMESPACE = "carbon-scheduler"
CONFIGMAP_NAME = "carbon-signal"
NODE_MAPPING_FILE = "node_mapping.yaml"

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


def load_node_mapping(path):
    """
    Load the K8s node -> vSphere VM name mapping from a YAML file.

    Args:
        path: Path to the YAML mapping file.

    Returns:
        A dict { k8s_node_name: vsphere_vm_name }. Empty dict if the file
        is missing or invalid.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data.get("mapping", {})
    except FileNotFoundError:
        print(f"[WARN] {path} not found, no node mapping available")
        return {}
    except yaml.YAMLError as e:
        print(f"[WARN] Failed to parse {path}: {e}")
        return {}


def build_signal(emaps, vsphere, metrics, node_mapping):
    """
    Collect data from all sources and build a complete carbon signal.

    Each source is queried independently with try/except so that one
    failing source does not prevent the others from being collected.

    Args:
        emaps: An ElectricityMaps instance.
        vsphere: A VSphere instance.
        metrics: A MetricsServer instance.
        node_mapping: Dict { k8s_node_name: vsphere_vm_name }.

    Returns:
        A dict containing the timestamp, grid carbon intensity, and a
        list of per-node entries with watts (from vSphere), CO2 emission
        rate, CPU and memory usage (from metrics-server).
    """
    # 1. Grid carbon intensity (with fallback if the API is down)
    try:
        emaps_data = emaps.get_current()
        grid_intensity = emaps_data["carbon_intensity"]
        zone = emaps_data["zone"]
    except Exception as e:
        print(f"[WARN] Electricity Maps unavailable: {e}")
        grid_intensity = 100.0  # Approximate Swiss grid average
        zone = "CH"

    # 2. Per-VM watts from vSphere (ground truth from ESXi hardware sensor)
    try:
        vsphere_vms = vsphere.get_vm_estimated_watts()
        # Build a lookup dict { vm_name: watts } for fast access
        vm_watts = {vm["name"]: vm["watts"] for vm in vsphere_vms}
    except Exception as e:
        print(f"[WARN] vSphere unavailable: {e}")
        vm_watts = {}

    # 3. Per-node CPU/RAM from metrics-server
    try:
        node_usage = metrics.get_node_usage()
    except Exception as e:
        print(f"[WARN] metrics-server unavailable: {e}")
        node_usage = {}

    # 4. Combine all sources per node
    # Iterate over K8s nodes (the authoritative list) and resolve their
    # vSphere counterpart via the mapping file
    nodes = []
    for k8s_name, usage in node_usage.items():
        # Resolve the corresponding vSphere VM name (if mapped)
        vsphere_name = node_mapping.get(k8s_name)
        if vsphere_name is None:
            print(f"[WARN] No vSphere mapping for K8s node '{k8s_name}'")
            watts = 0.0
        else:
            watts = vm_watts.get(vsphere_name, 0.0)
            if watts == 0.0:
                print(
                    f"[WARN] vSphere VM '{vsphere_name}' (mapped from "
                    f"K8s '{k8s_name}') returned 0 watts or not found"
                )

        # CO2 per second:
        #   Watts * (gCO2/kWh) / (3600 s/h * 1000 W/kW) = gCO2/s
        co2_per_second = watts * grid_intensity / (3600 * 1000)

        nodes.append(
            {
                "name": k8s_name,
                "vsphere_name": vsphere_name,
                "watts": watts,
                "co2_g_per_s": co2_per_second,
                "cpu_millicores": usage["cpu_millicores"],
                "memory_mib": usage["memory_mib"],
            }
        )
    try:
        raw_forecast = emaps.get_forecast_24h()
        forecast_24h = [
          {
              "datetime": point["datetime"],
              "carbon_intensity": point["carbonIntensity"],
          }
          for point in raw_forecast
      ]
    except Exception as e:
      print(f"[WARN] Forecast unavailable: {e}")
      forecast_24h = []

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "zone": zone,
        "grid_intensity_g_per_kwh": grid_intensity,
        "forecast_24h": forecast_24h,
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
    Main loop: load node mapping, instantiate clients, then poll all
    sources every POLL_INTERVAL seconds and publish the resulting signal.
    """
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Load Kubernetes config ONCE for the whole process.
    # In-cluster uses the pod's ServiceAccount token; otherwise local kubeconfig.
    if os.environ.get("IN_CLUSTER", "false").lower() == "true":
        config.load_incluster_config()
        print("Loaded in-cluster Kubernetes config")
    else:
        config.load_kube_config()
        print("Loaded local kubeconfig")

    # Load the K8s node <-> vSphere VM mapping
    node_mapping = load_node_mapping(NODE_MAPPING_FILE)
    print(f"Loaded {len(node_mapping)} node mappings from {NODE_MAPPING_FILE}")

    # Instantiate the source clients once (kept alive for the whole loop)
    emaps = ElectricityMaps()
    vsphere = VSphere()
    metrics = MetricsServer()

    print(f"Carbon Signal Aggregator started (cycle every {POLL_INTERVAL}s)")

    while running:
        try:
            data = build_signal(emaps, vsphere, metrics, node_mapping)
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
