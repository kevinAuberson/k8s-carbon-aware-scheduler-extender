from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from temporal import DelayDecision, TemporalScheduler

# ─── Fixtures ────────────────────────────────────────────────────


def make_forecast(current_ci, hourly_values):
    """Génère un forecast à partir d'une liste d'intensités horaires."""
    now = datetime.now(UTC)
    return [
        {
            "datetime": (now + timedelta(hours=i + 1)).isoformat(),
            "carbon_intensity": ci,
        }
        for i, ci in enumerate(hourly_values)
    ]


def make_signal(ci, forecast=None):
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "grid_intensity_g_per_kwh": ci,
        "forecast_24h": forecast or [],
        "nodes": [],
    }


def make_pod_besteffort(annotations=None, age_hours=0):
    created = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return {
        "metadata": {
            "name": "test-be",
            "creationTimestamp": created,
            "annotations": annotations or {},
            "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
        },
        "status": {"qosClass": "BestEffort"},
    }


def make_pod_batch(annotations=None, age_hours=0):
    created = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return {
        "metadata": {
            "name": "test-batch",
            "creationTimestamp": created,
            "annotations": annotations or {},
            "ownerReferences": [{"kind": "Job", "controller": True}],
        },
        "status": {"qosClass": "Burstable"},
    }


@pytest.fixture
def scheduler():
    loader = MagicMock()
    return TemporalScheduler(loader), loader


# ─── Latency-sensitive jamais retardé ────────────────────────────


def test_latency_sensitive_never_delayed_even_red(scheduler):
    sched, loader = scheduler
    loader.load.return_value = make_signal(ci=120)  # pic record
    pod = {
        "metadata": {
            "name": "web",
            "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
        },
        "status": {"qosClass": "Guaranteed"},
    }
    decision, _ = sched.decide(pod)
    assert decision == DelayDecision.SCHEDULE_NOW


# ─── Best-effort : analyse du forecast ───────────────────────────


def test_besteffort_delayed_when_better_window_exists(scheduler):
    """Best-effort : retardé si une meilleure fenêtre existe."""
    sched, loader = scheduler
    # Actuellement à 65, mais ça descend à 30 dans 3h
    forecast = make_forecast(65, [60, 50, 30, 35, 40, 45])
    loader.load.return_value = make_signal(ci=65, forecast=forecast)

    decision, reason = sched.decide(make_pod_besteffort())
    assert decision == DelayDecision.DELAY
    assert "waiting" in reason.lower()


def test_besteffort_scheduled_when_already_optimal(scheduler):
    """Best-effort : scheduled si on est déjà au minimum."""
    sched, loader = scheduler
    # On est à 45, le forecast monte → autant scheduler maintenant
    forecast = make_forecast(45, [50, 60, 70, 65, 55, 50])
    loader.load.return_value = make_signal(ci=45, forecast=forecast)

    decision, reason = sched.decide(make_pod_besteffort())
    assert decision == DelayDecision.SCHEDULE_NOW
    assert "near-optimal" in reason.lower() or "green" in reason.lower()


def test_besteffort_scheduled_when_gain_is_small(scheduler):
    """Best-effort : pas retardé si le gain est trop petit."""
    sched, loader = scheduler
    # 45 maintenant, 42 dans 5h → gain de 3 seulement < MIN_GAIN_TO_DELAY (10)
    forecast = make_forecast(45, [44, 43, 43, 42, 42])
    loader.load.return_value = make_signal(ci=45, forecast=forecast)

    decision, reason = sched.decide(make_pod_besteffort())
    assert decision == DelayDecision.SCHEDULE_NOW
    assert "near-optimal" in reason.lower() or "green" in reason.lower()


# ─── Batch : seulement retardé en zone rouge ─────────────────────


def test_batch_flexible_delayed_in_red_zone(scheduler):
    """Batch flexible : retardé seulement si zone rouge."""
    sched, loader = scheduler
    forecast = make_forecast(85, [80, 60, 40, 50, 60])
    loader.load.return_value = make_signal(ci=85, forecast=forecast)

    pod = make_pod_batch(annotations={"carbon-aware/flexible": "true"})
    decision, _ = sched.decide(pod)
    assert decision == DelayDecision.DELAY


def test_batch_flexible_not_delayed_in_orange(scheduler):
    """Batch flexible : pas retardé en zone orange (40-70)."""
    sched, loader = scheduler
    forecast = make_forecast(55, [50, 40, 35, 40, 50])
    loader.load.return_value = make_signal(ci=55, forecast=forecast)

    pod = make_pod_batch(annotations={"carbon-aware/flexible": "true"})
    decision, _ = sched.decide(pod)
    assert decision == DelayDecision.SCHEDULE_NOW


def test_batch_not_flexible_never_delayed(scheduler):
    """Batch sans annotation flexible : jamais retardé."""
    sched, loader = scheduler
    forecast = make_forecast(120, [80, 60, 40, 50, 60])
    loader.load.return_value = make_signal(ci=120, forecast=forecast)

    decision, _ = sched.decide(make_pod_batch())
    assert decision == DelayDecision.SCHEDULE_NOW


# ─── Deadlines ───────────────────────────────────────────────────


def test_deadline_in_past_forces_schedule(scheduler):
    sched, loader = scheduler
    forecast = make_forecast(80, [60, 40, 30])
    loader.load.return_value = make_signal(ci=80, forecast=forecast)

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    pod = make_pod_besteffort(annotations={"carbon-aware/deadline": past})

    decision, reason = sched.decide(pod)
    assert decision == DelayDecision.SCHEDULE_NOW
    assert "deadline" in reason.lower()


def test_deadline_excludes_far_future_optimal(scheduler):
    """Le forecast est filtré par la deadline."""
    sched, loader = scheduler
    # Optimal à +5h, mais deadline dans +2h → on regarde uniquement +1h et +2h
    forecast = make_forecast(70, [65, 60, 55, 50, 30])  # min à +5h (30)
    loader.load.return_value = make_signal(ci=70, forecast=forecast)

    deadline = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    pod = make_pod_besteffort(annotations={"carbon-aware/deadline": deadline})

    decision, reason = sched.decide(pod)
    # Le min accessible avant deadline est 60 (+2h), gain de 10 → on attend
    # MAIS si MIN_GAIN_TO_DELAY=10, c'est borderline. Vérifie ton seuil.
    # (Avec gain=10 et MIN_GAIN_TO_DELAY=10, "< 10" est False, donc on attend)
    assert decision == DelayDecision.DELAY


# ─── Max delay ───────────────────────────────────────────────────


def test_max_delay_exceeded_forces_schedule(scheduler):
    """Si le pod attend depuis trop longtemps, on force le scheduling."""
    sched, loader = scheduler
    forecast = make_forecast(100, [80, 60, 40])
    loader.load.return_value = make_signal(ci=100, forecast=forecast)

    # Pod créé il y a 8h, max-delay par défaut = 6h → expired
    pod = make_pod_besteffort(age_hours=8)
    decision, reason = sched.decide(pod)
    assert decision == DelayDecision.SCHEDULE_NOW
    assert "deadline" in reason.lower() or "max-delay" in reason.lower()


# ─── Pas de forecast (fallback) ──────────────────────────────────


def test_no_forecast_falls_back_to_zone_logic(scheduler):
    """Sans forecast, on utilise la logique simple à 2 zones."""
    sched, loader = scheduler
    loader.load.return_value = make_signal(ci=85, forecast=None)

    decision, _ = sched.decide(make_pod_besteffort())
    assert decision == DelayDecision.DELAY


# ─── Pas de signal ────────────────────────────────────────────────


def test_no_signal_fails_safe(scheduler):
    """Sans signal, on schedule par défaut (fail-safe)."""
    sched, loader = scheduler
    loader.load.return_value = None

    decision, _ = sched.decide(make_pod_besteffort())
    assert decision == DelayDecision.SCHEDULE_NOW


# ─── Grid déjà verte ─────────────────────────────────────────────


def test_green_grid_always_schedules(scheduler):
    """Grid déjà verte → schedule tout le monde."""
    sched, loader = scheduler
    forecast = make_forecast(30, [25, 20, 15, 20, 25])
    loader.load.return_value = make_signal(ci=30, forecast=forecast)

    decision, reason = sched.decide(make_pod_besteffort())
    assert decision == DelayDecision.SCHEDULE_NOW
    assert "green" in reason.lower()
