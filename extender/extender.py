"""
File:           extender.py
Author:         Kevin Auberson
Created:        2026-06-01
Description:    Carbon-aware scheduler extender — endpoints HTTP.
"""

import logging

from fastapi import FastAPI, Request

from scoring import CarbonScorer, NEUTRAL_SCORE
from signal_loader import SignalLoader

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("extender")

app = FastAPI()

# Singletons
signal_loader = SignalLoader()
scorer = CarbonScorer(signal_loader)


@app.post("/prioritize")
async def prioritize(request: Request):
    """Endpoint appelé par le scheduler pour scorer les nodes candidats."""
    body = await request.json()
    pod = body.get("Pod", {})
    pod_name = pod.get("metadata", {}).get("name", "?")
    node_names = body.get("NodeNames") or [
        n["metadata"]["name"]
        for n in body.get("Nodes", {}).get("items", [])
    ]

    scores = scorer.score_nodes(pod, node_names)

    results = [{"Host": name, "Score": scores.get(name, NEUTRAL_SCORE)}
               for name in node_names]

    if results:
        best = max(results, key=lambda x: x["Score"])
        log.info(f"Pod {pod_name} → best: {best['Host']} (score={best['Score']})")

    return {"HostPriorityList": results}


@app.post("/filter")
async def filter_nodes(request: Request):
    """Endpoint appelé par le scheduler pour filtrer les nodes inéligibles."""
    body = await request.json()
    nodes = body.get("Nodes", {}).get("items", [])

    # Pour l'instant : on laisse passer tous les nodes (pas de filtre dur).
    # Le scoring se fera dans /prioritize.
    # Plus tard, on pourra implémenter ici la logique de retardement
    # pour les pods batch flexibles.
    return {
        "Nodes": {"items": nodes},
        "FailedNodes": {},
        "Error": "",
    }


@app.get("/healthz")
async def health():
    """Healthcheck simple."""
    signal = signal_loader.load()
    age = signal_loader.age_seconds()
    return {
        "status": "ok",
        "signal_available": signal is not None,
        "signal_age_seconds": age,
    }


@app.get("/debug/signal")
async def debug_signal():
    """Affiche le signal courant (debug)."""
    return signal_loader.load() or {"error": "signal unavailable"}


@app.get("/debug/score")
async def debug_score(node: str = ""):
    """Affiche le scoring pour un pod fictif (debug)."""
    fake_pod = {
        "metadata": {"name": "debug-pod"},
        "status": {"qosClass": "Burstable"},
    }
    signal = signal_loader.load()
    if not signal:
        return {"error": "no signal"}
    node_names = [n["name"] for n in signal["nodes"]]
    if node:
        node_names = [node]
    return {"scores": scorer.score_nodes(fake_pod, node_names)}