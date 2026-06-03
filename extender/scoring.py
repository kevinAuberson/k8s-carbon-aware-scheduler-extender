
import logging
from typing import Optional

from workload_classifier import CarbonClass, PENALTY_FACTORS, classify

log = logging.getLogger("scoring")

# Score neutre quand tous les nodes sont équivalents
NEUTRAL_SCORE = 50
# Score max et min
MAX_SCORE = 100
MIN_SCORE = 0


class CarbonScorer:


    def __init__(self, signal_loader):
        self.signal_loader = signal_loader

    def score_nodes(self, pod: dict, node_names: list[str]) -> dict[str, int]:
        """
        Calcule le score 0-100 pour chaque node candidat.
        Retourne un dict {node_name: score}.
        """
        signal = self.signal_loader.load()
        if not signal:
            log.warning("No signal available, returning neutral scores")
            return {name: NEUTRAL_SCORE for name in node_names}

        carbon_class = classify(pod)
        alpha = PENALTY_FACTORS[carbon_class]
        ci = signal["grid_intensity_g_per_kwh"]
        ci_norm = self._normalize_ci(ci)

        log.info(
            f"Scoring pod {pod.get('metadata', {}).get('name', '?')} "
            f"class={carbon_class.value} α={alpha} CI={ci}gCO₂/kWh"
        )

        # 1. Calculer le coût marginal pour chaque node
        marginal_costs = {}
        for name in node_names:
            cost = self._marginal_cost(name, signal, alpha, ci_norm)
            if cost is not None:
                marginal_costs[name] = cost

        if not marginal_costs:
            log.warning("No node data available, returning neutral scores")
            return {name: NEUTRAL_SCORE for name in node_names}

        # 2. Normaliser en scores 0-100 (cf. 4.2.3)
        return self._normalize_to_scores(marginal_costs, node_names)

    def _marginal_cost(
        self, node_name: str, signal: dict, alpha: float, ci_norm: float
    ) -> Optional[float]:
        """
        Calcule C_marginal pour un node :
        C_marginal = (1 + α × CI_norm) × P_node × (1 + CPU_load)
        """
        node = next((n for n in signal["nodes"] if n["name"] == node_name), None)
        if not node:
            log.warning(f"Node {node_name} not found in signal")
            return None

        p_node = node["watts"]
        cpu_load = self._estimate_cpu_load(node)

        cost = (1 + alpha * ci_norm) * p_node * (1 + cpu_load)

        log.debug(
            f"{node_name}: P={p_node:.2f}W, CPU_load={cpu_load:.2f}, "
            f"α={alpha}, CI_norm={ci_norm:.2f} → C={cost:.4f}"
        )
        return cost

    def _estimate_cpu_load(self, node: dict) -> float:
        """
        Estime le ratio CPU_load (0-1) à partir des millicores.
        Hypothèse : un node typique a ~4000 millicores total.
        À ajuster selon ton cluster réel.
        """
        # TODO : récupérer la capacité réelle via /api/v1/nodes
        # Pour démarrer, on utilise une estimation
        cpu_capacity_mc = 4000  # 4 cores
        return min(node["cpu_millicores"] / cpu_capacity_mc, 1.0)

    def _normalize_ci(self, ci: int) -> float:
        """
        Normalise la grid intensity entre 0 et 1.
        En Suisse : 0-500 gCO₂/kWh est une fourchette réaliste.
        """
        return min(ci / 500.0, 1.0)

    def _normalize_to_scores(
        self, costs: dict[str, float], all_nodes: list[str]
    ) -> dict[str, int]:
        """
        Normalise les coûts marginaux en scores 0-100.
        Le node avec le coût le plus bas a le score le plus haut.
        """
        c_min = min(costs.values())
        c_max = max(costs.values())

        # Cas où tous les nodes sont équivalents (cf. 4.2.3)
        if abs(c_max - c_min) < 1e-9:
            log.info("All nodes equivalent, returning neutral score")
            return {name: NEUTRAL_SCORE for name in all_nodes}

        scores = {}
        for name in all_nodes:
            if name not in costs:
                # Node pas dans le signal → score neutre
                scores[name] = NEUTRAL_SCORE
                continue
            normalized = 1 - (costs[name] - c_min) / (c_max - c_min)
            scores[name] = int(round(MAX_SCORE * normalized))

        return scores