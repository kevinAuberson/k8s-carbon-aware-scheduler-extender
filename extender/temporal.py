"""
Logique de retardement carbon-aware (carbon-aware shifting).

Décide si un pod doit être schedulé maintenant ou retardé jusqu'à une
fenêtre temporelle plus favorable (intensité carbone plus basse), en
s'appuyant sur le forecast 24h de l'API ElectricityMaps.
"""
import logging
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from workload_classifier import CarbonClass, classify

log = logging.getLogger("temporal")


# ─── Configuration ─────────────────────────────────────────────────

# Seuils calibrés sur Suisse 2025 lifecycle (ElectricityMaps)
# Source : analyse statistique des données 2025
#   Médiane annuelle : 41.9 gCO₂eq/kWh
#   P75              : 58.3 gCO₂eq/kWh
#   P90              : 73.6 gCO₂eq/kWh
GREEN_THRESHOLD = int(os.getenv("GREEN_THRESHOLD_G_PER_KWH", "40"))
DIRTY_THRESHOLD = int(os.getenv("DIRTY_THRESHOLD_G_PER_KWH", "70"))

# Délai max par défaut si le pod n'a pas d'annotation deadline/max-delay
DEFAULT_MAX_DELAY_HOURS = int(os.getenv("DEFAULT_MAX_DELAY_HOURS", "6"))

# Gain minimum (gCO₂eq/kWh) pour qu'il vaille la peine d'attendre
# Évite de retarder un pod pour gagner 2 gCO₂/kWh = négligeable
MIN_GAIN_TO_DELAY = int(os.getenv("MIN_GAIN_TO_DELAY_G_PER_KWH", "10"))


# ─── Annotations supportées ────────────────────────────────────────

ANN_FLEXIBLE = "carbon-aware/flexible"
ANN_DEADLINE = "carbon-aware/deadline"
ANN_MAX_DELAY = "carbon-aware/max-delay-hours"


# ─── Types ─────────────────────────────────────────────────────────

class DelayDecision(StrEnum):
    SCHEDULE_NOW = "schedule_now"
    DELAY = "delay"


# ─── Moteur de décision ────────────────────────────────────────────

class TemporalScheduler:
    """
    Décide si un pod doit être schedulé maintenant ou retardé.

    Stratégie :
    1. Pods latency-sensitive → jamais retardés
    2. Pods non flexibles → jamais retardés
    3. Sans signal/forecast → fail-safe vers SCHEDULE_NOW
    4. Avec forecast :
       - Cherche la fenêtre optimale (CI minimale) avant la deadline
       - Si la fenêtre actuelle est déjà optimale ou quasi → SCHEDULE_NOW
       - Sinon, attend (le scheduler réessaiera dans ~30s)
    5. Si deadline atteinte ou max_delay dépassé → SCHEDULE_NOW de force
    """

    def __init__(
        self,
        signal_loader,
        green_threshold: int = GREEN_THRESHOLD,
        dirty_threshold: int = DIRTY_THRESHOLD,
        min_gain_to_delay: int = MIN_GAIN_TO_DELAY,
    ):
        self.signal_loader = signal_loader
        self.green_threshold = green_threshold
        self.dirty_threshold = dirty_threshold
        self.min_gain_to_delay = min_gain_to_delay

    # ─── Point d'entrée ──────────────────────────────────────────

    def decide(self, pod: dict) -> tuple[DelayDecision, str]:
        """Décide pour un pod donné : schedule maintenant ou retarder."""
        carbon_class = classify(pod)

        # 1. Latency-sensitive : jamais retardé
        if carbon_class == CarbonClass.LATENCY_SENSITIVE:
            return self._now("latency-sensitive: never delayed")

        # 2. Pas flexible : jamais retardé (consentement explicite requis)
        if not self._is_flexible(pod, carbon_class):
            return self._now(f"{carbon_class.value}: not flexible")

        # 3. Charger le signal
        signal = self.signal_loader.load()
        if not signal:
            return self._now("no signal available (fail-safe)")

        current_ci = signal["grid_intensity_g_per_kwh"]

        # 4. Si déjà très propre → schedule
        if current_ci <= self.green_threshold:
            return self._now(
                f"grid already green ({current_ci} ≤ {self.green_threshold})"
            )

        # 5. Vérifier la deadline (atteinte ?)
        deadline = self._parse_deadline(pod)
        max_delay_end = self._compute_max_delay_end(pod)
        effective_deadline = self._min_dt(deadline, max_delay_end)

        if effective_deadline and datetime.now(UTC) >= effective_deadline:
            return self._now("deadline/max-delay reached, forcing schedule")

        # 6. Analyser le forecast pour trouver la fenêtre optimale
        forecast = signal.get("forecast_24h", [])
        if not forecast:
            # Pas de forecast : fallback sur logique simple à 2 zones
            return self._decide_without_forecast(pod, carbon_class, current_ci)

        return self._decide_with_forecast(
            pod, carbon_class, current_ci, forecast, effective_deadline
        )

    # ─── Logique avec forecast (le bijou) ────────────────────────

    def _decide_with_forecast(
        self,
        pod: dict,
        carbon_class: CarbonClass,
        current_ci: float,
        forecast: list[dict],
        effective_deadline: datetime | None,
    ) -> tuple[DelayDecision, str]:
        """
        Cherche le moment optimal pour exécuter le pod dans le forecast.
        """
        # Filtrer le forecast pour ne garder que les points avant la deadline
        valid_forecast = self._filter_before_deadline(forecast, effective_deadline)

        if not valid_forecast:
            return self._now("no forecast point before deadline")

        # Trouver le minimum d'intensité dans la fenêtre disponible
        min_point = min(valid_forecast, key=lambda p: p["carbon_intensity"])
        min_ci = min_point["carbon_intensity"]
        min_dt = min_point["datetime"]

        gain = current_ci - min_ci

        # Cas 1 : on est déjà au minimum (ou quasi) → schedule maintenant
        if gain < self.min_gain_to_delay:
            return self._now(
                f"current CI={current_ci:.0f} is already near-optimal "
                f"(min forecast={min_ci:.0f}, gain={gain:.0f} < {self.min_gain_to_delay})"
            )

        # Cas 2 : ça vaut la peine d'attendre
        # On ne schedule QUE si l'intensité actuelle est dans une zone "rouge",
        # OU si le gain est très important
        if carbon_class == CarbonClass.BEST_EFFORT:
            # Best-effort : on est strict, on attend dès que ça vaut le coup
            return self._delay(
                f"best-effort: waiting for better window "
                f"(now={current_ci:.0f}, optimal={min_ci:.0f} at {min_dt}, "
                f"gain={gain:.0f})"
            )

        if carbon_class == CarbonClass.BATCH:
            # Batch : on attend seulement si on est en zone rouge
            if current_ci > self.dirty_threshold:
                return self._delay(
                    f"batch in red zone: waiting "
                    f"(now={current_ci:.0f}, optimal={min_ci:.0f} at {min_dt}, "
                    f"gain={gain:.0f})"
                )
            return self._now(
                f"batch in orange zone (CI={current_ci:.0f}), scheduling now"
            )

        return self._now(f"unhandled class {carbon_class}, defaulting to now")

    # ─── Logique sans forecast (fallback) ────────────────────────

    def _decide_without_forecast(
        self, pod: dict, carbon_class: CarbonClass, current_ci: float
    ) -> tuple[DelayDecision, str]:
        """
        Sans forecast, on fait une décision simple basée sur les seuils :
        - Zone rouge → retarde best-effort + batch flexibles
        - Zone orange → retarde uniquement best-effort
        - Zone verte → schedule tout (déjà géré dans decide())
        """
        if current_ci > self.dirty_threshold:
            return self._delay(
                f"red zone (CI={current_ci:.0f} > {self.dirty_threshold}), "
                f"no forecast available"
            )

        # Zone orange (entre green et dirty)
        if carbon_class == CarbonClass.BEST_EFFORT:
            return self._delay(
                f"orange zone (CI={current_ci:.0f}), delaying best-effort"
            )

        return self._now(
            f"orange zone (CI={current_ci:.0f}), batch can proceed"
        )

    # ─── Helpers : flexibilité ───────────────────────────────────

    def _is_flexible(self, pod: dict, carbon_class: CarbonClass) -> bool:
        """
        Un pod est flexible si :
        - annotation explicite carbon-aware/flexible=true
        - OU annotation deadline présente (consentement implicite)
        - OU classe best-effort (flexible par défaut)
        """
        annotations = pod.get("metadata", {}).get("annotations", {})

        if annotations.get(ANN_FLEXIBLE, "").lower() == "true":
            return True
        if ANN_DEADLINE in annotations:
            return True
        if carbon_class == CarbonClass.BEST_EFFORT:
            return True
        return False

    # ─── Helpers : deadlines & temps ─────────────────────────────

    def _parse_deadline(self, pod: dict) -> datetime | None:
        """Parse l'annotation carbon-aware/deadline en datetime UTC."""
        deadline_str = (
            pod.get("metadata", {}).get("annotations", {}).get(ANN_DEADLINE)
        )
        if not deadline_str:
            return None
        try:
            dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError as e:
            log.warning(f"Invalid deadline '{deadline_str}': {e}")
            return None

    def _compute_max_delay_end(self, pod: dict) -> datetime | None:
        """Calcule la fin du max-delay = creationTimestamp + max_delay_hours."""
        metadata = pod.get("metadata", {})
        created_str = metadata.get("creationTimestamp")
        if not created_str:
            return None
        try:
            created = datetime.fromisoformat(
                created_str.replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
        except ValueError:
            return None

        custom = metadata.get("annotations", {}).get(ANN_MAX_DELAY)
        max_hours = DEFAULT_MAX_DELAY_HOURS
        if custom:
            try:
                max_hours = float(custom)
            except ValueError:
                log.warning(f"Invalid max-delay-hours '{custom}'")

        return created + timedelta(hours=max_hours)

    def _min_dt(
        self, a: datetime | None, b: datetime | None
    ) -> datetime | None:
        """Retourne le plus petit datetime non-None."""
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)

    def _filter_before_deadline(
        self, forecast: list[dict], deadline: datetime | None
    ) -> list[dict]:
        """Garde les points du forecast situés avant la deadline."""
        if not deadline:
            return forecast
        result = []
        for point in forecast:
            try:
                dt = datetime.fromisoformat(
                    point["datetime"].replace("Z", "+00:00")
                )
                if dt < deadline:
                    result.append(point)
            except (ValueError, KeyError):
                continue
        return result

    # ─── Helpers : formatage ────────────────────────────────────

    def _now(self, reason: str) -> tuple[DelayDecision, str]:
        return (DelayDecision.SCHEDULE_NOW, reason)

    def _delay(self, reason: str) -> tuple[DelayDecision, str]:
        return (DelayDecision.DELAY, reason)

    # ─── API debug ──────────────────────────────────────────────

    def find_optimal_window(
        self, hours_ahead: int = 24
    ) -> dict | None:
        """
        Outil debug : retourne le moment optimal dans les N prochaines heures.
        Utile pour /debug/forecast.
        """
        signal = self.signal_loader.load()
        if not signal:
            return None

        forecast = signal.get("forecast_24h", [])
        if not forecast:
            return None

        horizon = datetime.now(UTC) + timedelta(hours=hours_ahead)
        valid = self._filter_before_deadline(forecast, horizon)
        if not valid:
            return None

        min_point = min(valid, key=lambda p: p["carbon_intensity"])
        return {
            "optimal_datetime": min_point["datetime"],
            "optimal_carbon_intensity": min_point["carbon_intensity"],
            "current_carbon_intensity": signal["grid_intensity_g_per_kwh"],
            "potential_gain": signal["grid_intensity_g_per_kwh"]
            - min_point["carbon_intensity"],
        }
