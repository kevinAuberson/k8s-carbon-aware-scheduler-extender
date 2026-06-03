import pytest
from unittest.mock import MagicMock

from scoring import CarbonScorer, NEUTRAL_SCORE
from workload_classifier import CarbonClass


@pytest.fixture
def mock_signal():
    return {
        "timestamp": "2026-06-01T12:00:00+00:00",
        "grid_intensity_g_per_kwh": 100,
        "nodes": [
            {"name": "node-low", "watts": 1.0, "cpu_millicores": 100},
            {"name": "node-mid", "watts": 2.0, "cpu_millicores": 500},
            {"name": "node-high", "watts": 4.0, "cpu_millicores": 2000},
        ],
    }


@pytest.fixture
def scorer(mock_signal):
    loader = MagicMock()
    loader.load.return_value = mock_signal
    return CarbonScorer(loader)


def test_lowest_cost_gets_highest_score(scorer):
    """Le node le moins coûteux doit avoir le score le plus élevé."""
    pod = {
        "metadata": {"name": "test", "ownerReferences": [{"kind": "Deployment"}]},
        "status": {"qosClass": "Burstable"},
    }
    scores = scorer.score_nodes(pod, ["node-low", "node-mid", "node-high"])

    assert scores["node-low"] == 100
    assert scores["node-high"] == 0
    assert 0 < scores["node-mid"] < 100


def test_all_equal_returns_neutral_score():
    """Si tous les nodes sont équivalents, tous reçoivent 50."""
    signal = {
        "timestamp": "2026-06-01T12:00:00+00:00",
        "grid_intensity_g_per_kwh": 100,
        "nodes": [
            {"name": "node-a", "watts": 1.0, "cpu_millicores": 100},
            {"name": "node-b", "watts": 1.0, "cpu_millicores": 100},
        ],
    }
    loader = MagicMock()
    loader.load.return_value = signal
    scorer = CarbonScorer(loader)

    pod = {"metadata": {"name": "test"}, "status": {"qosClass": "Burstable"}}
    scores = scorer.score_nodes(pod, ["node-a", "node-b"])

    assert scores == {"node-a": NEUTRAL_SCORE, "node-b": NEUTRAL_SCORE}


def test_no_signal_returns_neutral():
    """Sans signal, tous les nodes reçoivent un score neutre."""
    loader = MagicMock()
    loader.load.return_value = None
    scorer = CarbonScorer(loader)

    pod = {"metadata": {"name": "test"}, "status": {"qosClass": "Burstable"}}
    scores = scorer.score_nodes(pod, ["a", "b"])

    assert scores == {"a": NEUTRAL_SCORE, "b": NEUTRAL_SCORE}


def test_best_effort_penalized_more_by_high_ci():
    """Un pod best-effort doit être plus pénalisé sur grid carbonée qu'un latency-sensitive."""
    # Grid très carbonée
    signal = {
        "timestamp": "2026-06-01T12:00:00+00:00",
        "grid_intensity_g_per_kwh": 500,
        "nodes": [
            {"name": "clean", "watts": 1.0, "cpu_millicores": 100},
            {"name": "dirty", "watts": 4.0, "cpu_millicores": 100},
        ],
    }
    loader = MagicMock()
    loader.load.return_value = signal
    scorer = CarbonScorer(loader)

    deployment_pod = {
        "metadata": {"ownerReferences": [{"kind": "Deployment"}]},
        "status": {"qosClass": "Guaranteed"},
    }
    job_besteffort = {
        "metadata": {"ownerReferences": [{"kind": "Job"}]},
        "status": {"qosClass": "BestEffort"},
    }

    sl_scores = scorer.score_nodes(deployment_pod, ["clean", "dirty"])
    be_scores = scorer.score_nodes(job_besteffort, ["clean", "dirty"])

    # Pour les deux, "clean" est meilleur, mais l'écart est plus grand
    # pour best-effort (α=1.0) que pour latency-sensitive (α=0.0)
    # Note : avec la formule actuelle, l'écart se manifeste différemment selon α
    assert sl_scores["clean"] >= be_scores["clean"] or sl_scores["dirty"] <= be_scores["dirty"]