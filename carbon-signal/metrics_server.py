"""
File:        metrics_server.py
Author:      Kevin Auberson
Created:     2026-05-11
Description: Client for the Kubernetes metrics-server. Retrieves CPU and
             memory usage per node and per pod via the metrics.k8s.io API.
             Complementary to Kepler: while Kepler tells us HOW MUCH energy
             a node consumes, metrics-server tells us WHAT it does (CPU
             and memory load), which is needed to compute carbon-efficiency
             scores in the scheduler.
"""

import os
import re
from kubernetes import client, config
from cache import cache


class MetricsServer:
    """Client for the Kubernetes metrics.k8s.io API (metrics-server)."""

    def __init__(self):
        self.in_cluster = os.environ.get("IN_CLUSTER", "false").lower() == "true"
        self.ttl = 30
        self._api = None

    def _connect(self):
        """
        Load the Kubernetes config and create the API client.

        Uses in-cluster config when running inside a pod (with a service
        account token), otherwise falls back to the local ~/.kube/config.
        """
        if self._api is not None:
            return

        if self.in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()

        self._api = client.CustomObjectsApi()

    def _parse_cpu(self, cpu_str):
        """
        Convert a Kubernetes CPU string into millicores.

        The metrics-server can return CPU values in different units depending
        on the runtime: nanocores ('1500000n'), microcores ('1500u'),
        millicores ('100m'), or whole cores ('0.5').

        Args:
            cpu_str: The raw CPU string returned by the API.

        Returns:
            CPU usage in millicores as an integer.
        """
        if cpu_str.endswith("n"):    # nanocores
            return int(int(cpu_str[:-1]) / 1_000_000)
        if cpu_str.endswith("u"):    # microcores
            return int(int(cpu_str[:-1]) / 1_000)
        if cpu_str.endswith("m"):    # millicores
            return int(cpu_str[:-1])
        return int(float(cpu_str) * 1000)  # whole cores

    def _parse_memory(self, mem_str):
        """
        Convert a Kubernetes memory string into MiB.

        Kubernetes uses binary suffixes (Ki, Mi, Gi, Ti) by default.

        Args:
            mem_str: The raw memory string returned by the API.

        Returns:
            Memory usage in MiB as an integer.
        """
        units = {"Ki": 1/1024, "Mi": 1, "Gi": 1024, "Ti": 1024*1024}
        match = re.match(r"^(\d+)([A-Za-z]*)$", mem_str)
        if not match:
            return 0
        value, unit = int(match.group(1)), match.group(2)
        return int(value * units.get(unit, 1/(1024*1024)))

    def get_node_usage(self):
        """
        Get current CPU and memory usage for every node in the cluster.

        Returns:
            A dict { node_name: {"cpu_millicores": int, "memory_mib": int} }.
        """
        cached = cache.get("metrics_nodes")
        if cached is not None:
            return cached

        self._connect()
        data = self._api.list_cluster_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            plural="nodes",
        )

        nodes = {}
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            nodes[name] = {
                "cpu_millicores": self._parse_cpu(item["usage"]["cpu"]),
                "memory_mib": self._parse_memory(item["usage"]["memory"]),
            }

        cache.set("metrics_nodes", nodes, self.ttl)
        return nodes

    def get_pod_usage(self, namespace=None):
        """
        Get current CPU and memory usage per pod, summed across containers.

        Args:
            namespace: Optional namespace filter. If None, queries all
                       namespaces in the cluster.

        Returns:
            A list of dicts, each with:
            - name (str): pod name
            - namespace (str): pod namespace
            - cpu_millicores (int): total CPU usage of all containers
            - memory_mib (int): total memory usage of all containers
        """
        cache_key = f"metrics_pods_{namespace or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        self._connect()
        if namespace:
            data = self._api.list_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
            )
        else:
            data = self._api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
            )

        pods = []
        for item in data.get("items", []):
            total_cpu = 0
            total_mem = 0
            for c in item.get("containers", []):
                total_cpu += self._parse_cpu(c["usage"]["cpu"])
                total_mem += self._parse_memory(c["usage"]["memory"])
            pods.append({
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "cpu_millicores": total_cpu,
                "memory_mib": total_mem,
            })

        cache.set(cache_key, pods, self.ttl)
        return pods


# Standalone test: python metrics_server.py
if __name__ == "__main__":
    ms = MetricsServer()
    print(ms.get_node_usage())