"""
File:        kepler.py
Author:      Kevin Auberson
Created:     2026-05-11
Description: Client that retrieves per-node and per-pod power consumption
             from Kepler. Kepler exposes its metrics on Prometheus, so we
             query Prometheus using PromQL rather than scraping Kepler
             directly. This gives us aggregation, rate() conversion from
             Joules to Watts, and label-based filtering for free.
"""

import os
import requests
from cache import cache


class Kepler:
    """Client for Kepler metrics, accessed via Prometheus."""

    def __init__(self):
        self.prometheus_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
        self.ttl = 30  # 30s — fine granularity for scheduling decisions

    def _query(self, promql):
        """
        Run a PromQL query against Prometheus.

        Args:
            promql: The PromQL expression to evaluate.

        Returns:
            A list of result entries as returned by Prometheus, each with
            a 'metric' dict and a 'value' tuple. Empty list on failure.
        """
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            response = requests.get(url, params={"query": promql}, timeout=10)
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                return []
            return payload["data"]["result"]
        except requests.RequestException:
            return []

    def get_node_watts(self):
        """
        Get the current power consumption of every node in the cluster.

        Uses kepler_node_platform_joules_total (a counter in Joules) and
        applies rate() over 1 minute to convert it into Watts.

        Returns:
            A dict { node_name: watts_float }. Empty if Kepler is down.
        """
        cached = cache.get("kepler_nodes")
        if cached is not None:
            return cached

        results = self._query("rate(kepler_node_platform_joules_total[1m])")

        nodes = {}
        for r in results:
            node_name = r["metric"].get("instance", "unknown")
            watts = float(r["value"][1])
            nodes[node_name] = watts

        cache.set("kepler_nodes", nodes, self.ttl)
        return nodes

    def get_pod_watts(self):
        """
        Get the current power consumption per pod, aggregated across all
        containers in the pod.

        Returns:
            A list of dicts, each with:
            - pod (str): pod name
            - namespace (str): namespace the pod runs in
            - watts (float): estimated power consumption in Watts
        """
        cached = cache.get("kepler_pods")
        if cached is not None:
            return cached

        promql = (
            "sum by (pod_name, container_namespace) "
            "(rate(kepler_container_joules_total[1m]))"
        )
        results = self._query(promql)

        pods = []
        for r in results:
            pods.append(
                {
                    "pod": r["metric"].get("pod_name", "unknown"),
                    "namespace": r["metric"].get("container_namespace", "unknown"),
                    "watts": float(r["value"][1]),
                }
            )

        cache.set("kepler_pods", pods, self.ttl)
        return pods


# Standalone test: python kepler.py
if __name__ == "__main__":
    k = Kepler()
    print(k.get_node_watts())
