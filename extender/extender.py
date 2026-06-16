"""
File:        extender.py
Author:      Kevin Auberson
Created:     2026-05-02
Description: HTTP entry point for the carbon-aware scheduler extender.
             Exposes /filter (temporal shifting — delays pods to greener
             windows) and /prioritize (node scoring by marginal carbon cost).
             Also serves /metrics (Prometheus), /healthz and /debug/* endpoints.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from metrics import (
    CI_AT_DECISION,
    DELAY_GAIN,
    DIRTY_THRESHOLD_METRIC,
    GREEN_THRESHOLD_METRIC,
    GRID_INTENSITY,
    MARGINAL_COST,
    NODE_CO2_G_PER_S,
    NODE_SCORE,
    NODE_SELECTED,
    NODE_WATTS,
    SCHEDULING_DECISIONS,
    SIGNAL_AGE,
)
from scoring import NEUTRAL_SCORE, CarbonScorer
from signal_loader import SignalLoader
from temporal import DelayDecision, TemporalScheduler
from workload_classifier import classify

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("extender")

# Singletons (created before lifespan so they're available in route handlers)
signal_loader = SignalLoader()
scorer = CarbonScorer(signal_loader)
temporal = TemporalScheduler(signal_loader)


async def _refresh_metrics_loop() -> None:
    """Background task: push signal-level gauges every 30 s.

    Decouples Grafana liveness from pod scheduling events so dashboards
    stay fresh even when no workloads are being scheduled.
    """
    while True:
        try:
            signal = signal_loader.load()
            if signal:
                ci = signal["grid_intensity_g_per_kwh"]
                GRID_INTENSITY.set(ci)
                SIGNAL_AGE.set(signal_loader.age_seconds() or 0.0)

                green = signal.get("green_threshold_g_per_kwh")
                dirty = signal.get("dirty_threshold_g_per_kwh")
                if green is not None:
                    GREEN_THRESHOLD_METRIC.set(green)
                if dirty is not None:
                    DIRTY_THRESHOLD_METRIC.set(dirty)

                for node in signal.get("nodes", []):
                    name = node["name"]
                    NODE_WATTS.labels(node=name).set(node.get("watts", 0.0))
                    NODE_CO2_G_PER_S.labels(node=name).set(node.get("co2_g_per_s", 0.0))
        except Exception as exc:
            log.warning(f"Background metrics refresh failed: {exc}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_refresh_metrics_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.post("/filter")
async def filter_nodes(request: Request):
    """
    Filtre les nodes éligibles. Peut aussi retarder le scheduling
    d'un pod en retournant tous les nodes dans FailedNodes.
    """
    body = await request.json()
    pod = body.get("Pod", {})
    pod_name = pod.get("metadata", {}).get("name", "?")
    nodes = body.get("Nodes", {}).get("items", [])
    node_names_str = [n["metadata"]["name"] for n in nodes]

    # Décision temporelle : on schedule maintenant ou on attend ?
    decision, reason = temporal.decide(pod)
    log.info(f"Pod {pod_name}: {decision.value} ({reason})")

    carbon_class = classify(pod).value
    signal = signal_loader.load()
    ci = signal["grid_intensity_g_per_kwh"] if signal else 0.0

    SCHEDULING_DECISIONS.labels(carbon_class=carbon_class, decision=decision.value).inc()
    CI_AT_DECISION.labels(carbon_class=carbon_class, decision=decision.value).observe(ci)

    if decision == DelayDecision.DELAY:
        # Extraire le gain depuis le forecast si disponible
        optimal = temporal.find_optimal_window()
        if optimal and optimal["potential_gain"] > 0:
            DELAY_GAIN.labels(carbon_class=carbon_class).observe(optimal["potential_gain"])

        return {
            "Nodes": {"items": []},
            "FailedNodes": {name: f"carbon-aware delay: {reason}" for name in node_names_str},
            "Error": "",
        }

    # SCHEDULE_NOW : on laisse passer tous les nodes, le scoring fera le reste
    return {
        "Nodes": {"items": nodes},
        "FailedNodes": {},
        "Error": "",
    }


@app.post("/prioritize")
async def prioritize(request: Request):
    body = await request.json()
    pod = body.get("Pod", {})
    pod_name = pod.get("metadata", {}).get("name", "?")
    node_names = body.get("NodeNames") or [
        n["metadata"]["name"] for n in body.get("Nodes", {}).get("items", [])
    ]

    scores = scorer.score_nodes(pod, node_names)
    results = [{"Host": name, "Score": scores.get(name, NEUTRAL_SCORE)} for name in node_names]

    carbon_class = classify(pod).value
    signal = signal_loader.load()

    for name, score in scores.items():
        NODE_SCORE.labels(node=name, carbon_class=carbon_class).set(score)

    if signal:
        for node_data in signal.get("nodes", []):
            if node_data["name"] in scores:
                MARGINAL_COST.labels(node=node_data["name"]).observe(
                    node_data.get("co2_g_per_s", 0.0)
                )

    if results:
        best = max(results, key=lambda x: x["Score"])
        NODE_SELECTED.labels(node=best["Host"], carbon_class=carbon_class).inc()
        log.info(f"Pod {pod_name} → best: {best['Host']} (score={best['Score']})")

    return {"HostPriorityList": results}


@app.get("/healthz")
async def health():
    signal = signal_loader.load()
    age = signal_loader.age_seconds()
    return {
        "status": "ok",
        "signal_available": signal is not None,
        "signal_age_seconds": age,
        "green_threshold": temporal.green_threshold,
    }


@app.get("/debug/decide")
async def debug_decide():
    """Affiche la décision pour un pod fictif (debug)."""
    fake_pods = {
        "deployment-pod": {
            "metadata": {
                "name": "test",
                "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
            },
            "status": {"qosClass": "Burstable"},
        },
        "batch-rigid": {
            "metadata": {
                "name": "test",
                "ownerReferences": [{"kind": "Job", "controller": True}],
            },
            "status": {"qosClass": "Burstable"},
        },
        "batch-flexible": {
            "metadata": {
                "name": "test",
                "annotations": {"carbon-aware/flexible": "true"},
                "ownerReferences": [{"kind": "Job", "controller": True}],
            },
            "status": {"qosClass": "Burstable"},
        },
        "best-effort": {
            "metadata": {
                "name": "test",
                "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
            },
            "status": {"qosClass": "BestEffort"},
        },
    }
    return {
        name: {"decision": d[0].value, "reason": d[1]}
        for name, pod in fake_pods.items()
        for d in [temporal.decide(pod)]
    }


@app.get("/debug/forecast")
async def debug_forecast():
    """Affiche le forecast et le moment optimal."""
    signal = signal_loader.load()
    if not signal:
        return {"error": "no signal"}

    optimal = temporal.find_optimal_window()
    return {
        "current": {
            "datetime": signal["timestamp"],
            "carbon_intensity": signal["grid_intensity_g_per_kwh"],
        },
        "forecast_24h": signal.get("forecast_24h", []),
        "optimal_window": optimal,
        "thresholds": {
            "green": temporal.green_threshold,
            "dirty": temporal.dirty_threshold,
        },
    }
