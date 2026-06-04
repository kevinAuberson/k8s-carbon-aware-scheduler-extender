"""Classifie un pod selon son owner, sa QoS et son éventuel label explicite."""
import logging
from enum import Enum
from typing import Optional

log = logging.getLogger("classifier")


class CarbonClass(str, Enum):
    LATENCY_SENSITIVE = "latency-sensitive"
    BATCH = "batch"
    BEST_EFFORT = "best-effort"


PENALTY_FACTORS = {
    CarbonClass.LATENCY_SENSITIVE: 0.0,
    CarbonClass.BATCH: 0.5,
    CarbonClass.BEST_EFFORT: 1.0,
}

# Labels valides pour override manuel
VALID_LABELS = {c.value for c in CarbonClass}


LONG_RUNNING_KINDS = {"ReplicaSet", "StatefulSet"}
BATCH_KINDS = {"Job"}
DAEMON_KINDS = {"DaemonSet"}


def classify(pod: dict) -> CarbonClass:
    """
    Classifie un pod selon la table 4.1 de la spec.

    Ordre de priorité :
    1. Label explicite `carbon-class` sur le pod (override manuel)
    2. Classification automatique : owner kind + QoS class

    Returns:
        CarbonClass: latency-sensitive | batch | best-effort
    """
    metadata = pod.get("metadata", {})
    pod_name = metadata.get("name", "?")

    # ── Étape 1 : Label explicite (override manuel) ───────────
    labels = metadata.get("labels", {})
    explicit = labels.get("carbon-class")
    if explicit in VALID_LABELS:
        log.info(f"Pod {pod_name}: explicit label carbon-class={explicit}")
        return CarbonClass(explicit)

    if explicit:
        log.warning(
            f"Pod {pod_name}: invalid carbon-class label '{explicit}', "
            f"falling back to automatic classification"
        )

    # ── Étape 2 : Classification automatique ──────────────────
    owner_kind = _get_controller_kind(metadata)
    qos = pod.get("status", {}).get("qosClass", "BestEffort")

    log.debug(f"Pod {pod_name}: owner={owner_kind}, qos={qos}")

    # 2a. DaemonSet : toujours latency-sensitive (cf. tableau)
    if owner_kind in DAEMON_KINDS:
        log.info(f"Pod {pod_name}: DaemonSet → latency-sensitive")
        return CarbonClass.LATENCY_SENSITIVE

    # 2b. BestEffort : toujours best-effort (sauf DaemonSet géré au-dessus)
    if qos == "BestEffort":
        log.info(f"Pod {pod_name}: QoS BestEffort → best-effort")
        return CarbonClass.BEST_EFFORT

    # 2c. Long-running (Deployment/StatefulSet/ReplicaSet)
    if owner_kind in LONG_RUNNING_KINDS:
        log.info(f"Pod {pod_name}: {owner_kind} + {qos} → latency-sensitive")
        return CarbonClass.LATENCY_SENSITIVE

    # 2d. Batch (Job/CronJob)
    if owner_kind in BATCH_KINDS:
        log.info(f"Pod {pod_name}: {owner_kind} + {qos} → batch")
        return CarbonClass.BATCH

    # 2e. Fallback : pod orphelin ou kind inconnu
    log.warning(
        f"Pod {pod_name}: unknown owner '{owner_kind}', "
        f"defaulting to best-effort"
    )
    return CarbonClass.BEST_EFFORT


def _get_controller_kind(metadata: dict) -> Optional[str]:
    """
    Récupère le kind du controller owner du pod.

    Un pod peut avoir plusieurs owners, mais un seul est marqué
    `controller: true` — c'est celui qui pilote son cycle de vie.

    Returns:
        Le kind du controller (ex: "ReplicaSet", "Job"), ou None si orphelin.
    """
    refs = metadata.get("ownerReferences", [])

    # Cherche celui marqué controller=true (le bon)
    for ref in refs:
        if ref.get("controller", False):
            return ref.get("kind")

    # Fallback : premier owner si aucun n'est marqué controller
    return refs[0].get("kind") if refs else None