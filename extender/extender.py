"""Carbon-aware scheduler extender — endpoints HTTP."""

import logging

from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from metrics import (
    CI_AT_DECISION,
    DELAY_GAIN,
    GRID_INTENSITY,
    MARGINAL_COST,
    NODE_SCORE,
    SCHEDULING_DECISIONS,
    SIGNAL_AGE,
)
from scoring import NEUTRAL_SCORE, CarbonScorer
from signal_loader import SignalLoader
from temporal import DelayDecision, TemporalScheduler
from workload_classifier import classify

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("extender")

app = FastAPI()
app.mount("/metrics", make_asgi_app())

# Singletons
signal_loader = SignalLoader()
scorer = CarbonScorer(signal_loader)
temporal = TemporalScheduler(signal_loader)


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
    GRID_INTENSITY.set(ci)
    SIGNAL_AGE.set(signal_loader.age_seconds() or 0.0)

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
    # ... ton code existant (inchangé)
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
