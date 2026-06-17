"""
File:        signal_loader.py
Author:      Kevin Auberson
Created:     2026-06-03
Description: Loads the carbon signal from the JSON file mounted from the
             carbon-signal ConfigMap. Includes an in-memory cache to avoid
             re-reading the file on every /filter and /prioritize call,
             and a staleness guard that rejects signals older than MAX_SIGNAL_AGE.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("signal_loader")

# Chemin du fichier signal monté depuis le ConfigMap carbon-signal.
# Le défaut correspond au volumeMount de production ; SIGNAL_FILE est
# surchargeable en développement local pour pointer vers un fichier mock.
SIGNAL_FILE = os.getenv("SIGNAL_FILE", "/etc/carbon-signal/signal.json")

# Durée du cache mémoire (secondes). Le fichier n'est relu que si
# le cache est expiré. Réduit les I/O disque de ~3x par requête.
CACHE_TTL = int(os.getenv("SIGNAL_CACHE_TTL", "5"))

# Âge maximum accepté pour le signal (secondes). Au-delà, load()
# retourne None et l'extender fait un fail-safe (schedule_now).
MAX_SIGNAL_AGE = int(os.getenv("MAX_SIGNAL_AGE", "600"))


class SignalLoader:
    """Charge le signal carbone avec cache mémoire et garde de fraîcheur."""

    def __init__(self, signal_file: str = SIGNAL_FILE):
        self.signal_file = Path(signal_file)
        self._cache: dict | None = None
        self._cache_time: float = 0.0

    def load(self) -> dict | None:
        """Lit le signal courant avec cache. Retourne None si indisponible ou périmé."""
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_time) < CACHE_TTL:
            return self._cache

        data = self._read_file()
        if data is None:
            self._cache = None
            return None

        age = self._compute_age(data)
        if age is not None and age > MAX_SIGNAL_AGE:
            log.warning(
                f"Signal is {age:.0f}s old (max {MAX_SIGNAL_AGE}s), "
                f"rejecting stale data — extender will fail-safe to schedule_now"
            )
            self._cache = None
            return None

        self._cache = data
        self._cache_time = now
        return data

    def _read_file(self) -> dict | None:
        try:
            data = json.loads(self.signal_file.read_text())
            self._validate(data)
            return data
        except FileNotFoundError:
            log.warning(f"Signal file {self.signal_file} not found")
            return None
        except (json.JSONDecodeError, ValueError) as e:
            log.error(f"Invalid signal data: {e}")
            return None

    def _validate(self, data: dict) -> None:
        required = {"timestamp", "grid_intensity_g_per_kwh", "nodes"}
        if not required.issubset(data.keys()):
            raise ValueError(f"Missing keys: {required - data.keys()}")
        if not isinstance(data["nodes"], list) or not data["nodes"]:
            raise ValueError("nodes must be a non-empty list")

    def _compute_age(self, data: dict) -> float | None:
        try:
            ts = datetime.fromisoformat(data["timestamp"])
            return (datetime.now(ts.tzinfo) - ts).total_seconds()
        except (ValueError, KeyError):
            return None

    def get_node_data(self, node_name: str) -> dict | None:
        signal = self.load()
        if not signal:
            return None
        for node in signal["nodes"]:
            if node["name"] == node_name:
                return node
        return None

    def age_seconds(self) -> float | None:
        signal = self.load()
        if not signal:
            return None
        return self._compute_age(signal)
