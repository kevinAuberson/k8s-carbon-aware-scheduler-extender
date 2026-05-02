import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("extender")

app = FastAPI()

# Chemin du fichier de scores (montée via ConfigMap en prod)
SCORES_FILE = os.getenv("SCORES_FILE", "/etc/extender/scores.json")


def load_scores() -> dict:
    """Charge les scores depuis le fichier monté en ConfigMap."""
    try:
        return json.loads(Path(SCORES_FILE).read_text())
    except FileNotFoundError:
        log.warning(f"Fichier {SCORES_FILE} introuvable, scores vides")
        return {}
    except json.JSONDecodeError as e:
        log.error(f"JSON invalide dans {SCORES_FILE}: {e}")
        return {}


SCORES = load_scores()
log.info(f"Scores chargés au démarrage : {SCORES}")


@app.post("/prioritize")
async def prioritize(request: Request):
    body = await request.json()
    node_names = body.get("NodeNames") or [
        n["metadata"]["name"] for n in body.get("Nodes", {}).get("items", [])
    ]
    pod_name = body.get("Pod", {}).get("metadata", {}).get("name", "?")

    results = []
    for name in node_names:
        score = SCORES.get(name, 0)
        results.append({"Host": name, "Score": score})
        log.info(f"Pod {pod_name} -> {name}: score={score}")

    if results:
        best = max(results, key=lambda x: x["Score"])
        log.info(f">>> Best node for {pod_name}: {best['Host']} (score={best['Score']})")

    return {"HostPriorityList": results}


@app.post("/filter")
async def filter_nodes(request: Request):
    body = await request.json()
    nodes = body.get("Nodes", {}).get("items", [])

    filtered = []
    for node in nodes:
        name = node["metadata"]["name"]
        score = SCORES.get(name, 0)
        if score > 0:
            filtered.append(node)
            log.info(f"FILTER: {name} -> PASS (score={score})")
        else:
            log.info(f"FILTER: {name} -> BLOCKED (score={score})")

    return {
        "Nodes": {"items": filtered},
        "FailedNodes": {},
        "Error": "",
    }


@app.post("/config")
async def update_scores(new_scores: dict):
    global SCORES
    SCORES = new_scores
    log.info(f"Scores updated: {SCORES}")
    return {"scores": SCORES}


@app.get("/config")
async def get_scores():
    return {"scores": SCORES}


@app.get("/healthz")
async def health():
    return {"status": "ok"}
