"""Сравнение B / C / D на одинаковых данных (гипотеза H4).

    .venv/Scripts/python scripts/research/bcd_experiment.py

B — классический TOPSIS; C — ограничения → Парето → TOPSIS; D — ограничения → полезности
на фиксированных шкалах → TOPSIS с фиксированными идеалами. Метрики объявлены
в EXPERIMENT_PROTOCOL.md, разд. 8. Результат — docs/research/results/bcd_results.json.

Шкалы режима D задаются до сравнения и не зависят от состава набора:
* качество — естественные границы (ROC-AUC 0.5…1, R² 0…1);
* ресурсы — логарифмическая шкала «бюджет → бюджет/100». Бюджет: граница ограничения,
  если она есть; иначе «бюджет развёртывания» = 4 × медиана по датасету, зафиксированный
  до добавления приманок и новых альтернатив. Чувствительность к множителю 100 проверяется
  отдельно (10 и 1000).
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from decision_experiment import (  # noqa: E402
    PROFILES,
    SEED,
    cli_dirs,
    constraint_sets,
    decoys,
    load_matrices,
    problem_for,
)

from backend.domain.decision.analysis import analyse  # noqa: E402
from backend.domain.decision.models import (  # noqa: E402
    DecisionProblem,
    ScaleOverride,
    SensitivitySettings,
)

OUT = ROOT / "docs" / "research" / "results"
DECOYS = 10
INSERTIONS = 10
BUDGET_FACTOR = 4.0
MODES = ("topsis", "robust", "fixed")
NAMES = {"topsis": "B", "robust": "C", "fixed": "D"}


def scales_for(matrix: dict[str, Any], problem: DecisionProblem, span: float = 100.0) -> tuple[ScaleOverride, ...]:
    primary = matrix["primary"]
    natural = {"roc_auc": (0.5, 1.0), "r2": (0.0, 1.0)}[primary]
    result = [ScaleOverride(primary, "linear", *natural)]
    for key in ("inference_ms", "model_mb"):
        bound = next((c.bound for c in problem.constraints if c.criterion == key and c.op == "<="), None)
        if bound is None:
            bound = BUDGET_FACTOR * statistics.median(a["values"][key]["value"] for a in matrix["alternatives"])
        result.append(ScaleOverride(key, "log", bound, bound / span))
    return tuple(result)


def fast(problem: DecisionProblem) -> dict[str, Any]:
    return analyse(dataclasses.replace(problem, sensitivity=SensitivitySettings(samples=0, seed=SEED)))


def order(result: dict[str, Any], mode: str) -> list[str]:
    return [row["key"] for row in result["modes"][mode]["ranking"]]


def pair_flips(before: list[str], after: list[str]) -> float | None:
    """Доля пар исходных альтернатив, поменявших взаимный порядок."""
    common = [key for key in before if key in after]
    pairs = list(itertools.combinations(common, 2))
    if not pairs:
        return None
    position = {key: index for index, key in enumerate(after)}
    return sum(1 for a, b in pairs if position[a] > position[b]) / len(pairs)


def new_alternative(matrix: dict[str, Any], rng: random.Random, index: int) -> dict[str, Any]:
    """Новая альтернатива из диапазона наблюдаемых значений: может быть и недоминируемой."""
    item: dict[str, Any] = {"key": f"new_{index}"}
    for key in (matrix["primary"], "inference_ms", "model_mb"):
        values = [a["values"][key]["value"] for a in matrix["alternatives"]]
        item[key] = rng.uniform(min(values), max(values))
    return item


def quality(matrix: dict[str, Any], key: str | None) -> float | None:
    if key is None:
        return None
    alt = next(a for a in matrix["alternatives"] if a["key"] == key)
    return alt["values"][matrix["primary"]]["value"]


def study(matrices: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for matrix in matrices:
        for constraint_name, constraints in constraint_sets(matrix).items():
            for profile_name, profile in PROFILES.items():
                base = problem_for(matrix, profile, constraints)
                problem = dataclasses.replace(base, scales=scales_for(matrix, base))
                result = analyse(dataclasses.replace(problem, sensitivity=SensitivitySettings(samples=500, seed=SEED)))
                modes = result["modes"]
                feasible = [a["key"] for a in result["alternatives"] if a["status"] in ("pareto", "dominated")]
                best_feasible = max((quality(matrix, k) or 0.0) for k in feasible) if feasible else None
                row: dict[str, Any] = {
                    "dataset": matrix["context"]["dataset"],
                    "constraints": constraint_name,
                    "profile": profile_name,
                    "declared_quality": problem.effective_weights()[matrix["primary"]],
                }
                for mode in MODES:
                    winner = modes[mode]["winner"]
                    alt = next((a for a in result["alternatives"] if a["key"] == winner), None)
                    row[f"winner_{NAMES[mode]}"] = winner
                    row[f"violates_{NAMES[mode]}"] = bool(alt and alt["violated"])
                    q = quality(matrix, winner)
                    row[f"quality_gap_{NAMES[mode]}"] = (
                        None if q is None or best_feasible is None else round(best_feasible - q, 6)
                    )
                    row[f"influence_{NAMES[mode]}"] = (modes[mode].get("influence") or {}).get(matrix["primary"])
                    row[f"local_share_{NAMES[mode]}"] = modes[mode].get("winner_local_share")

                flips: dict[str, list[float]] = {mode: [] for mode in MODES}
                for item in decoys(problem, rng, DECOYS):
                    extended = fast(
                        dataclasses.replace(
                            problem_for(matrix, profile, constraints, extra=[item]), scales=problem.scales
                        )
                    )
                    for mode in MODES:
                        flips[mode].append(float(extended["modes"][mode]["winner"] != modes[mode]["winner"]))
                for mode in MODES:
                    row[f"decoy_winner_change_{NAMES[mode]}"] = statistics.fmean(flips[mode])

                inserted: dict[str, list[float]] = {mode: [] for mode in MODES}
                for index in range(INSERTIONS):
                    item = new_alternative(matrix, rng, index)
                    extended = fast(
                        dataclasses.replace(
                            problem_for(matrix, profile, constraints, extra=[item]), scales=problem.scales
                        )
                    )
                    for mode in MODES:
                        value = pair_flips(order(result, mode), order(extended, mode))
                        if value is not None:
                            inserted[mode].append(value)
                for mode in MODES:
                    row[f"insertion_pair_flips_{NAMES[mode]}"] = (
                        statistics.fmean(inserted[mode]) if inserted[mode] else None
                    )
                rows.append(row)
    return rows


def responsiveness(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Среднее число разных победителей по четырём профилям приоритетов (1 — приоритеты не влияют)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["dataset"], row["constraints"]), []).append(row)
    return {
        name: round(statistics.fmean(len({r[f"winner_{name}"] for r in group}) for group in groups.values()), 3)
        for name in NAMES.values()
    }


def span_sensitivity(matrices: list[dict[str, Any]]) -> dict[str, float]:
    """Доля сценариев, где победитель D при множителе 10 и 1000 совпадает с победителем при 100."""
    same = {"10": [], "1000": []}
    for matrix in matrices:
        for constraints in constraint_sets(matrix).values():
            for profile in PROFILES.values():
                base = problem_for(matrix, profile, constraints)
                reference = fast(dataclasses.replace(base, scales=scales_for(matrix, base, 100.0)))["modes"]["fixed"]
                for span in ("10", "1000"):
                    other = fast(dataclasses.replace(base, scales=scales_for(matrix, base, float(span))))
                    same[span].append(other["modes"]["fixed"]["winner"] == reference["winner"])
    return {span: round(sum(values) / len(values), 4) for span, values in same.items()}


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    constrained = [r for r in rows if r["constraints"] != "none"]

    def mean(key: str, subset: list[dict[str, Any]]) -> float | None:
        values = [r[key] for r in subset if r[key] is not None]
        return round(statistics.fmean(values), 4) if values else None

    summary: dict[str, Any] = {"scenarios": len(rows), "constrained": len(constrained)}
    for name in NAMES.values():
        summary[name] = {
            "violation_rate": mean(f"violates_{name}", constrained),
            "mean_quality_gap": mean(f"quality_gap_{name}", rows),
            "max_quality_gap": max((r[f"quality_gap_{name}"] or 0.0) for r in rows),
            "mean_quality_influence_balanced": mean(
                f"influence_{name}", [r for r in rows if r["profile"] == "balanced"]
            ),
            "decoy_winner_change": mean(f"decoy_winner_change_{name}", rows),
            "insertion_pair_flips": mean(f"insertion_pair_flips_{name}", rows),
            "mean_local_share": mean(f"local_share_{name}", rows),
        }
    summary["distinct_winners_over_profiles"] = responsiveness(rows)
    return summary


def main() -> None:
    directories, tag = cli_dirs()
    matrices = load_matrices(directories)
    rng = random.Random(SEED)  # noqa: S311 — воспроизводимые сценарии, не криптография
    started = time.perf_counter()
    rows = study(matrices, rng)
    result = {
        "format": "dataarena.bcd-experiment/1",
        "seed": SEED,
        "budget_factor": BUDGET_FACTOR,
        "scenarios": rows,
        "summary": summarise(rows),
        "span_sensitivity": span_sensitivity(matrices),
    }
    result["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"bcd_results{tag}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"] | {"span": result["span_sensitivity"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
