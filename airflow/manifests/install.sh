#!/bin/bash
# Deploy Airflow for the carbon-aware scheduler benchmark.
# Run from the repo root: bash airflow/manifests/install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "==> Cleaning up previous install..."
helm uninstall airflow -n airflow 2>/dev/null || true
kubectl delete pvc --all -n airflow 2>/dev/null || true
kubectl delete pods --all -n airflow --force 2>/dev/null || true

echo "==> Creating namespace..."
kubectl create namespace airflow 2>/dev/null || true

echo "==> Deploying PostgreSQL..."
kubectl apply -f "$SCRIPT_DIR/postgres.yaml"
echo "Waiting for PostgreSQL to be ready..."
kubectl wait --for=condition=ready pod -l app=postgres -n airflow --timeout=120s

echo "==> Installing Airflow via Helm..."
helm repo add apache-airflow https://airflow.apache.org 2>/dev/null || true
helm repo update
helm install airflow apache-airflow/airflow \
  -n airflow \
  -f "$REPO_ROOT/airflow/values-airflow.yaml" \
  --version 1.16.0 \
  --timeout 10m

echo "==> Waiting for Airflow to be ready..."
kubectl wait --for=condition=ready pod -l component=webserver -n airflow --timeout=300s

echo "==> Copying DAG..."
SCHEDULER_POD=$(kubectl get pod -n airflow -l component=scheduler -o jsonpath='{.items[0].metadata.name}')
kubectl cp "$REPO_ROOT/airflow/dags/benchmark_dag.py" \
  "airflow/$SCHEDULER_POD:/opt/airflow/dags/benchmark_dag.py" -c scheduler

echo ""
echo "==> Airflow is ready!"
echo "    Access the UI: kubectl port-forward svc/airflow-webserver -n airflow 8181:8080"
echo "    Login: admin / admin"
