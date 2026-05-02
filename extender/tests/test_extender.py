import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def scores_file(tmp_path, monkeypatch):
    """Crée un fichier scores.json temporaire et configure l'env."""
    scores_data = {
        "node-green": 10,
        "node-yellow": 5,
        "node-red": 0,
    }
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(json.dumps(scores_data))
    monkeypatch.setenv("SCORES_FILE", str(scores_path))
    return scores_data


@pytest.fixture
def client(scores_file):
    """Recharge le module extender avec le fichier de test, puis crée un client."""
    # Reload pour que SCORES soit recalculé avec la nouvelle env var
    import importlib

    import extender

    importlib.reload(extender)
    return TestClient(extender.app)


# ─── Tests basiques ───────────────────────────────────────────


def test_healthz(client):
    """L'endpoint /healthz doit répondre 200."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_config_returns_loaded_scores(client, scores_file):
    """L'endpoint GET /config doit retourner les scores chargés depuis le fichier."""
    response = client.get("/config")
    assert response.status_code == 200
    assert response.json() == {"scores": scores_file}


def test_update_config(client):
    """L'endpoint POST /config doit mettre à jour les scores en mémoire."""
    new_scores = {"node-test": 42}
    response = client.post("/config", json=new_scores)
    assert response.status_code == 200
    assert response.json() == {"scores": new_scores}

    # Vérifier que le GET reflète le nouveau state
    response = client.get("/config")
    assert response.json() == {"scores": new_scores}


# ─── Tests de l'endpoint /prioritize ──────────────────────────


def test_prioritize_returns_correct_scores(client):
    """L'endpoint /prioritize doit retourner les bons scores pour chaque node."""
    payload = {
        "Pod": {"metadata": {"name": "test-pod"}},
        "NodeNames": ["node-green", "node-yellow", "node-red"],
    }
    response = client.post("/prioritize", json=payload)
    assert response.status_code == 200

    result = response.json()["HostPriorityList"]
    assert len(result) == 3

    scores_by_host = {item["Host"]: item["Score"] for item in result}
    assert scores_by_host["node-green"] == 10
    assert scores_by_host["node-yellow"] == 5
    assert scores_by_host["node-red"] == 0


def test_prioritize_unknown_node_gets_zero(client):
    """Un node inconnu doit avoir un score de 0."""
    payload = {
        "Pod": {"metadata": {"name": "test-pod"}},
        "NodeNames": ["node-inconnu"],
    }
    response = client.post("/prioritize", json=payload)
    assert response.status_code == 200
    assert response.json()["HostPriorityList"][0]["Score"] == 0


def test_prioritize_handles_nodes_items_format(client):
    """L'endpoint /prioritize doit aussi accepter le format Nodes.items."""
    payload = {
        "Pod": {"metadata": {"name": "test-pod"}},
        "Nodes": {
            "items": [
                {"metadata": {"name": "node-green"}},
                {"metadata": {"name": "node-red"}},
            ]
        },
    }
    response = client.post("/prioritize", json=payload)
    assert response.status_code == 200
    assert len(response.json()["HostPriorityList"]) == 2


# ─── Tests de l'endpoint /filter ──────────────────────────────


def test_filter_blocks_zero_score_nodes(client):
    """L'endpoint /filter doit bloquer les nodes avec un score de 0."""
    payload = {
        "Nodes": {
            "items": [
                {"metadata": {"name": "node-green"}},  # score 10 → PASS
                {"metadata": {"name": "node-red"}},  # score 0 → BLOCK
                {"metadata": {"name": "node-inconnu"}},  # score 0 → BLOCK
            ]
        }
    }
    response = client.post("/filter", json=payload)
    assert response.status_code == 200

    result = response.json()
    assert len(result["Nodes"]["items"]) == 1
    assert result["Nodes"]["items"][0]["metadata"]["name"] == "node-green"


def test_filter_passes_positive_score_nodes(client):
    """L'endpoint /filter doit laisser passer les nodes avec un score > 0."""
    payload = {
        "Nodes": {
            "items": [
                {"metadata": {"name": "node-green"}},
                {"metadata": {"name": "node-yellow"}},
            ]
        }
    }
    response = client.post("/filter", json=payload)
    assert response.status_code == 200
    assert len(response.json()["Nodes"]["items"]) == 2


def test_filter_with_no_nodes(client):
    """L'endpoint /filter doit gérer une liste vide."""
    payload = {"Nodes": {"items": []}}
    response = client.post("/filter", json=payload)
    assert response.status_code == 200
    assert response.json()["Nodes"]["items"] == []


# ─── Test du loader de scores ─────────────────────────────────


def test_load_scores_handles_missing_file(monkeypatch, tmp_path):
    """load_scores() doit retourner {} si le fichier n'existe pas."""
    monkeypatch.setenv("SCORES_FILE", str(tmp_path / "inexistant.json"))
    import importlib

    import extender

    importlib.reload(extender)
    assert extender.SCORES == {}


def test_load_scores_handles_invalid_json(monkeypatch, tmp_path):
    """load_scores() doit retourner {} si le JSON est invalide."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{ not valid json")
    monkeypatch.setenv("SCORES_FILE", str(bad_file))
    import importlib

    import extender

    importlib.reload(extender)
    assert extender.SCORES == {}
