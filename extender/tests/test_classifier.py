from workload_classifier import CarbonClass, classify

# ─── Override par label explicite ─────────────────────────────


def test_explicit_label_overrides_classification():
    """Le label carbon-class doit prendre le pas sur la logique auto."""
    pod = {
        "metadata": {
            "name": "test",
            "labels": {"carbon-class": "best-effort"},
            "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
        },
        "status": {"qosClass": "Guaranteed"},
    }
    # Sans le label, serait latency-sensitive ; avec le label, best-effort
    assert classify(pod) == CarbonClass.BEST_EFFORT


def test_invalid_label_falls_back_to_auto():
    """Un label invalide est ignoré, on passe en auto."""
    pod = {
        "metadata": {
            "name": "test",
            "labels": {"carbon-class": "invalid-value"},
            "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
        },
        "status": {"qosClass": "Guaranteed"},
    }
    # Doit faire la classification auto
    assert classify(pod) == CarbonClass.LATENCY_SENSITIVE


# ─── controller: true vs [0] ──────────────────────────────────


def test_picks_controller_owner_not_first():
    """Doit choisir l'owner avec controller=true, pas juste [0]."""
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [
                {"kind": "SomeOther", "controller": False},
                {"kind": "Job", "controller": True},  # ← celui-ci
            ],
        },
        "status": {"qosClass": "Burstable"},
    }
    assert classify(pod) == CarbonClass.BATCH


# ─── Logique de classification (mise à jour) ──────────────────


def test_daemonset_is_latency_sensitive():
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [{"kind": "DaemonSet", "controller": True}],
        },
        "status": {"qosClass": "BestEffort"},  # même BestEffort
    }
    assert classify(pod) == CarbonClass.LATENCY_SENSITIVE


def test_deployment_via_replicaset():
    """Un Pod de Deployment apparaît avec owner=ReplicaSet."""
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
        },
        "status": {"qosClass": "Guaranteed"},
    }
    assert classify(pod) == CarbonClass.LATENCY_SENSITIVE


def test_statefulset_burstable_is_latency_sensitive():
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [{"kind": "StatefulSet", "controller": True}],
        },
        "status": {"qosClass": "Burstable"},
    }
    assert classify(pod) == CarbonClass.LATENCY_SENSITIVE


def test_job_guaranteed_is_batch():
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [{"kind": "Job", "controller": True}],
        },
        "status": {"qosClass": "Guaranteed"},
    }
    assert classify(pod) == CarbonClass.BATCH


def test_job_besteffort_is_best_effort():
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [{"kind": "Job", "controller": True}],
        },
        "status": {"qosClass": "BestEffort"},
    }
    assert classify(pod) == CarbonClass.BEST_EFFORT


def test_deployment_besteffort_is_best_effort():
    pod = {
        "metadata": {
            "name": "test",
            "ownerReferences": [{"kind": "ReplicaSet", "controller": True}],
        },
        "status": {"qosClass": "BestEffort"},
    }
    assert classify(pod) == CarbonClass.BEST_EFFORT


def test_standalone_pod_defaults_to_best_effort():
    """Pod sans owner (kubectl run direct) → best-effort par défaut."""
    pod = {
        "metadata": {"name": "test"},
        "status": {"qosClass": "BestEffort"},
    }
    assert classify(pod) == CarbonClass.BEST_EFFORT
