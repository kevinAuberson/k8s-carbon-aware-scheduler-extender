"""
File:        benchmark_dag.py
Author:      Kevin Auberson
Created:     2026-06-24
Description: Airflow DAG for the carbon-aware scheduler benchmark.
             Submits three types of tasks every 15 minutes to exercise
             all three carbon classes:
             - latency_sensitive: Guaranteed QoS, never delayed
             - batch: Burstable QoS (Job-like), delayed only in red zone
             - best_effort: BestEffort QoS, delayed whenever a greener window exists

             Each task runs the same CPU-bound workload (SHA-256 for 5 min)
             so energy consumption is identical — only the scheduling
             behavior differs between classes.
"""

import hashlib
import os
import time
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

TASK_DURATION_SECONDS = int(os.getenv("BENCHMARK_TASK_DURATION", "300"))

PHASE = os.getenv("BENCHMARK_PHASE", "baseline")

POD_TEMPLATE_DIR = "/opt/airflow/pod_templates"


def cpu_burn():
    """CPU-bound workload: compute SHA-256 hashes for a fixed duration."""
    end = time.monotonic() + TASK_DURATION_SECONDS
    data = b"carbon-aware-benchmark-payload"
    iterations = 0
    while time.monotonic() < end:
        for _ in range(10_000):
            data = hashlib.sha256(data).digest()
            iterations += 1
    print(f"Completed {iterations} SHA-256 iterations in {TASK_DURATION_SECONDS}s")


TASK_CONFIGS = [
    {
        "task_id": "latency_sensitive",
        "carbon_class": "latency-sensitive",
        "qos": "Guaranteed",
        "requests": {"cpu": "500m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
        "gate": False,
    },
    {
        "task_id": "batch",
        "carbon_class": "batch",
        "qos": "Burstable",
        "requests": {"cpu": "250m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
        "gate": True,
    },
    {
        "task_id": "best_effort",
        "carbon_class": "best-effort",
        "qos": "BestEffort",
        "requests": {},
        "limits": {},
        "gate": True,
    },
]

default_args = {
    "owner": "benchmark",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
}

with DAG(
    dag_id="carbon_benchmark",
    default_args=default_args,
    description="Carbon-aware scheduler benchmark — 3 carbon classes every 15 min",
    schedule_interval="*/15 * * * *",
    start_date=datetime(2026, 6, 25),
    catchup=False,
    max_active_runs=4,
    tags=["benchmark", "carbon"],
) as dag:
    for cfg in TASK_CONFIGS:
        pod_override = {
            "metadata": {
                "labels": {
                    "benchmark": "carbon-aware",
                    "benchmark-phase": PHASE,
                    "carbon-class": cfg["carbon_class"],
                },
            },
            "spec": {},
        }

        if PHASE == "carbon":
            pod_override["spec"]["schedulerName"] = "carbon-aware"
            if cfg["gate"]:
                pod_override["spec"]["schedulingGates"] = [
                    {"name": "carbon-aware-gate"}
                ]

        containers = [
            {
                "name": "base",
                "image": "apache/airflow:2.10.5-python3.11",
            }
        ]
        if cfg["requests"]:
            containers[0]["resources"] = {
                "requests": cfg["requests"],
                "limits": cfg["limits"],
            }

        pod_override["spec"]["containers"] = containers

        PythonOperator(
            task_id=cfg["task_id"],
            python_callable=cpu_burn,
            executor_config={"pod_override": pod_override},
        )
