"""Charge et expose les données de carbon-signal."""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger("signal_loader")

SIGNAL_FILE = os.getenv("SIGNAL_FILE", "/etc/carbon-signal/signal.json")


class SignalLoader:
    """Charge le signal carbone depuis le fichier monté."""

    def __init__(self, signal_file: str = SIGNAL_FILE):
        self.signal_file = Path(signal_file)

    def load(self) -> dict | None:
        """Lit le signal courant. Retourne None si indisponible."""
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
        """Vérifie la structure du signal."""
        required = {"timestamp", "grid_intensity_g_per_kwh", "nodes"}
        if not required.issubset(data.keys()):
            raise ValueError(f"Missing keys: {required - data.keys()}")
        if not isinstance(data["nodes"], list) or not data["nodes"]:
            raise ValueError("nodes must be a non-empty list")

    def get_node_data(self, node_name: str) -> dict | None:
        """Retourne les données d'un node spécifique."""
        signal = self.load()
        if not signal:
            return None
        for node in signal["nodes"]:
            if node["name"] == node_name:
                return node
        return None

    def age_seconds(self) -> float | None:
        """Âge du signal en secondes (utile pour détecter signal périmé)."""
        signal = self.load()
        if not signal:
            return None
        ts = datetime.fromisoformat(signal["timestamp"])
        return (datetime.now(ts.tzinfo) - ts).total_seconds()
