"""
File:        export_benchmark.py
Author:      Kevin Auberson
Created:     2026-06-24
Description: Queries Prometheus to export benchmark metrics for both phases
             (baseline vs carbon-aware) into a CSV file for analysis.

Usage:
    python export_benchmark.py \
        --prometheus http://prometheus.monitoring:9090 \
        --baseline-start "2026-06-20T00:00:00Z" \
        --baseline-end   "2026-06-22T00:00:00Z" \
        --carbon-start   "2026-06-22T00:00:00Z" \
        --carbon-end     "2026-06-24T00:00:00Z" \
        --output results.csv
"""

import argparse
import csv
import sys
from datetime import datetime, timezone

import requests

METRICS = [
    {
        "name": "ci_mean_g_per_kwh",
        "label": "Average CI at scheduling (gCO2eq/kWh)",
        "query": (
            "sum(increase(carbon_ci_at_decision_g_per_kwh_sum"
            '{{decision="schedule_now"}}[{duration}s]))'
            " / "
            "sum(increase(carbon_ci_at_decision_g_per_kwh_count"
            '{{decision="schedule_now"}}[{duration}s]))'
        ),
    },
    {
        "name": "co2_proportional_total",
        "label": "CO2 proportional total (sum of CI at decision)",
        "query": (
            "sum(increase(carbon_ci_at_decision_g_per_kwh_sum"
            '{{decision="schedule_now"}}[{duration}s]))'
        ),
    },
    {
        "name": "tasks_scheduled",
        "label": "Number of tasks scheduled",
        "query": (
            "sum(increase(carbon_scheduling_decisions_total"
            '{{decision="schedule_now"}}[{duration}s]))'
        ),
    },
    {
        "name": "co2_per_task",
        "label": "CO2 proportional per task",
        "query": (
            "sum(increase(carbon_ci_at_decision_g_per_kwh_sum"
            '{{decision="schedule_now"}}[{duration}s]))'
            " / "
            "sum(increase(carbon_scheduling_decisions_total"
            '{{decision="schedule_now"}}[{duration}s]))'
        ),
    },
    {
        "name": "tasks_delayed",
        "label": "Number of tasks delayed",
        "query": (
            "sum(increase(carbon_scheduling_decisions_total"
            '{{decision="delay"}}[{duration}s]))'
        ),
    },
    {
        "name": "delay_total_seconds",
        "label": "Total accumulated delay (seconds)",
        "query": (
            "sum(increase(carbon_gate_delay_duration_seconds_sum"
            "[{duration}s]))"
        ),
    },
    {
        "name": "delay_mean_seconds",
        "label": "Average delay per gated pod (seconds)",
        "query": (
            "sum(increase(carbon_gate_delay_duration_seconds_sum"
            "[{duration}s]))"
            " / "
            "sum(increase(carbon_gate_delay_duration_seconds_count"
            "[{duration}s]))"
        ),
    },
    {
        "name": "delay_gain_mean_g_per_kwh",
        "label": "Average CI gain from delaying (gCO2eq/kWh)",
        "query": (
            "sum(increase(carbon_delay_gain_g_per_kwh_sum"
            "[{duration}s]))"
            " / "
            "sum(increase(carbon_delay_gain_g_per_kwh_count"
            "[{duration}s]))"
        ),
    },
]


def parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str).astimezone(timezone.utc)


def query_prometheus(base_url: str, query: str, eval_time: datetime) -> float | None:
    resp = requests.get(
        f"{base_url}/api/v1/query",
        params={"query": query, "time": eval_time.timestamp()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "success":
        return None
    results = data["data"]["result"]
    if not results:
        return None
    value = float(results[0]["value"][1])
    if value != value:  # NaN check
        return None
    return value


def collect_phase(base_url: str, start: datetime, end: datetime) -> dict:
    duration = int((end - start).total_seconds())
    row = {}
    for m in METRICS:
        query = m["query"].format(duration=duration)
        row[m["name"]] = query_prometheus(base_url, query, end)
    return row


def main():
    parser = argparse.ArgumentParser(
        description="Export carbon-aware benchmark metrics from Prometheus"
    )
    parser.add_argument(
        "--prometheus", default="http://localhost:9090", help="Prometheus base URL"
    )
    parser.add_argument("--baseline-start", required=True, help="Baseline phase start (ISO 8601)")
    parser.add_argument("--baseline-end", required=True, help="Baseline phase end (ISO 8601)")
    parser.add_argument("--carbon-start", required=True, help="Carbon-aware phase start (ISO 8601)")
    parser.add_argument("--carbon-end", required=True, help="Carbon-aware phase end (ISO 8601)")
    parser.add_argument("--output", default="benchmark_results.csv", help="Output CSV file")
    args = parser.parse_args()

    baseline_start = parse_ts(args.baseline_start)
    baseline_end = parse_ts(args.baseline_end)
    carbon_start = parse_ts(args.carbon_start)
    carbon_end = parse_ts(args.carbon_end)

    print(f"Querying baseline phase: {baseline_start} → {baseline_end}")
    baseline = collect_phase(args.prometheus, baseline_start, baseline_end)

    print(f"Querying carbon-aware phase: {carbon_start} → {carbon_end}")
    carbon = collect_phase(args.prometheus, carbon_start, carbon_end)

    # Print summary
    print("\n" + "=" * 70)
    print(f"{'Metric':<45} {'Baseline':>10} {'Carbon':>10}")
    print("=" * 70)
    for m in METRICS:
        bv = baseline.get(m["name"])
        cv = carbon.get(m["name"])
        b_str = f"{bv:.2f}" if bv is not None else "N/A"
        c_str = f"{cv:.2f}" if cv is not None else "N/A"
        print(f"{m['label']:<45} {b_str:>10} {c_str:>10}")

    # CO2 gain
    b_co2 = baseline.get("co2_proportional_total")
    c_co2 = carbon.get("co2_proportional_total")
    if b_co2 and c_co2 and b_co2 > 0:
        gain = (b_co2 - c_co2) / b_co2 * 100
        print("-" * 70)
        print(f"{'CO2 gain (%)':<45} {gain:>10.1f}%")
    print("=" * 70)

    # Write CSV
    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "label", "baseline", "carbon_aware", "unit"])
        for m in METRICS:
            writer.writerow([
                m["name"],
                m["label"],
                baseline.get(m["name"], ""),
                carbon.get(m["name"], ""),
                "gCO2eq/kWh" if "g_per_kwh" in m["name"] else "seconds" if "seconds" in m["name"] else "count",
            ])
        if b_co2 and c_co2 and b_co2 > 0:
            writer.writerow(["co2_gain_percent", "CO2 gain", "", f"{gain:.2f}", "%"])

    print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
