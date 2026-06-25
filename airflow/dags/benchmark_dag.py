"""
File:        benchmark_dag.py
Author:      Kevin Auberson
Created:     2026-06-24
Description: Airflow DAG for the carbon-aware scheduler benchmark.
             Submits a CPU-bound task every 15 minutes via KubernetesExecutor.
             The task computes SHA-256 hashes in a loop for ~5 minutes,
             ensuring consistent and reproducible energy consumption.

             The diversity of carbon classes comes from the Airflow stack itself:
             - Deployment (webserver, scheduler)  → latency-sensitive
             - StatefulSet (PostgreSQL)            → latency-sensitive
             - Job (DB migrations)                 → batch
             - Worker pods (this DAG)              → batch
             - CronJob (cleanup)                   → best-effort
"""

import hashlib
import os
import time
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

TASK_DURATION_SECONDS = int(os.getenv("BENCHMARK_TASK_DURATION", "300"))


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


default_args = {
    "owner": "benchmark",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
}

with DAG(
    dag_id="carbon_benchmark",
    default_args=default_args,
    description="Carbon-aware scheduler benchmark — CPU task every 15 min",
    schedule_interval="*/15 * * * *",
    start_date=datetime(2026, 6, 25),
    catchup=False,
    max_active_runs=4,
    tags=["benchmark", "carbon"],
) as dag:
    cpu_task = PythonOperator(
        task_id="cpu_burn",
        python_callable=cpu_burn,
    )
