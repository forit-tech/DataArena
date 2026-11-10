"""Сравнение режимов выбора A / B / C на измеренных матрицах и контролируемых синтетических.

    .venv/Scripts/python scripts/research/decision_experiment.py

Читает матрицы из docs/research/data (их снимает measure_models.py), пишет
docs/research/results/results.json и results.md. Протокол и заранее объявленные
метрики — docs/research/EXPERIMENT_PROTOCOL.md. Всё детерминировано seed-ом.

Обозначения режимов:
  A — победитель по основной метрике качества;
  B — классический TOPSIS (векторная нормализация) без ограничений;
  C — ограничения → Парето → TOPSIS (CP-TOPSIS-R).
"""

from __future__ import annotations

import json
import math
import platform
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.domain.decision.analysis import analyse, influence  # noqa: E402
from backend.domain.decision.models import (  # noqa: E402
    Alternative,
    Constraint,
    Criterion,
    DecisionProblem,
    Measurement,
    SensitivitySettings,
)
from backend.domain.decision.pareto import dominates  # noqa: E402
from backend.domain.decision.topsis import topsis  # noqa: E402

SEED = 20261109
DATA = ROOT / "docs" / "research" / "data"
OUT = ROOT / "docs" / "research" / "results"
DECOYS_PER_SCENARIO = 20
SAMPLES = 1000
SYNTHETIC_MATRICES = 300

#профили — те же, что в интерфейсе (useDecisionLab.ts): вес по смысловой группе
PROFILES = {
    "balanced": {"quality": 4, "time": 2, "size": 2},
    "quality": {"quality": 8, "time": 0.5, "size": 0.5},
    "edge": {"quality": 3, "time": 3, "size": 3},
    "latency": {"quality": 3, "time": 6, "size": 1},
}


def load_matrices(directories: list[Path] | None = None) -> list[dict[str, Any]]:
    paths = [path for directory in (directories or [DATA]) for path in sorted(directory.glob("*.json"))]
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def cli_dirs() -> tuple[list[Path], str]:
    """--data каталог [каталог ...] --tag имя: какие матрицы брать и как назвать файл результата."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--data", nargs="*", type=Path, default=[DATA])
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    return list(args.data), (f"_{args.tag}" if args.tag else "")


def problem_for(
    matrix: dict[str, Any],
    profile: dict[str, float],
    constraints: tuple[Constraint, ...] = (),
    normalization: str = "vector",
    extra: list[dict[str, Any]] | None = None,
) -> DecisionProblem:
    primary = matrix["primary"]
    keys = [primary, "inference_ms", "model_mb"]
    by_key = {item["key"]: item for item in matrix["criteria"]}
    criteria = tuple(
        Criterion(
            key,
            by_key[key]["label"],
            by_key[key]["direction"],
            profile[by_key[key]["group"]],
            by_key[key]["unit"],
            by_key[key]["group"],
        )
        for key in keys
    )
    alternatives = [
        Alternative(
            alt["key"],
            alt["label"],
            {key: Measurement(alt["values"][key]["value"], alt["values"][key]["std"]) for key in keys},
        )
        for alt in matrix["alternatives"]
    ]
    for item in extra or []:
        alternatives.append(Alternative(item["key"], item["key"], {k: Measurement(item[k]) for k in keys}))
    return DecisionProblem(
        criteria,
        tuple(alternatives),
        constraints,
        normalization,  # type: ignore[arg-type]
        primary,
        SensitivitySettings(samples=SAMPLES, seed=SEED),
    )


def constraint_sets(matrix: dict[str, Any]) -> dict[str, tuple[Constraint, ...]]:
    """Ограничения на медианах наблюдаемых значений: половина моделей отсекается по каждому."""
    primary = matrix["primary"]
    direction = next(c["direction"] for c in matrix["criteria"] if c["key"] == primary)

    def median(key: str) -> float:
        return statistics.median(alt["values"][key]["value"] for alt in matrix["alternatives"])

    quality_floor = Constraint(primary, ">=" if direction == "max" else "<=", median(primary))
    return {
        "none": (),
        "latency_median": (Constraint("inference_ms", "<=", median("inference_ms")),),
        "size_median": (Constraint("model_mb", "<=", median("model_mb")),),
        "quality_floor": (quality_floor,),
        "quality_floor+latency": (quality_floor, Constraint("inference_ms", "<=", median("inference_ms"))),
    }


def decoys(problem: DecisionProblem, rng: random.Random, count: int) -> list[dict[str, Any]]:
    """Альтернативы, доминируемые одной из существующих: хуже по каждому критерию в 1–3 раза."""
    result = []
    for index in range(count):
        base = rng.choice(problem.alternatives)
        item: dict[str, Any] = {"key": f"decoy_{index}"}
        for c in problem.criteria:
            value = float(base.value_of(c.key) or 0.0)
            if c.direction == "max":
                item[c.key] = value - abs(value) * rng.uniform(0.0, 0.05) - 1e-6
            else:
                item[c.key] = value * rng.uniform(1.0, 3.0) + 1e-6
        result.append(item)
    return result


def winner_violates(result: dict[str, Any], mode: str) -> bool | None:
    winner = result["modes"][mode]["winner"]
    if winner is None:
        return None
    alt = next(a for a in result["alternatives"] if a["key"] == winner)
    return bool(alt["violated"])


def share(xs: list[bool]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def real_study(matrices: list[dict[str, Any]], rng: random.Random) -> dict[str, Any]:
    scenarios = []
    for matrix in matrices:
        for profile_name, profile in PROFILES.items():
            for constraint_name, constraints in constraint_sets(matrix).items():
                problem = problem_for(matrix, profile, constraints)
                result = analyse(problem)
                modes = result["modes"]
                reversal_b, reversal_c = [], []
                for decoy in decoys(problem, rng, DECOYS_PER_SCENARIO):
                    extended = problem_for(matrix, profile, constraints, extra=[decoy])
                    fast = _winners_only(extended)
                    reversal_b.append(fast["topsis"] != modes["topsis"]["winner"])
                    reversal_c.append(fast["robust"] != modes["robust"]["winner"])
                quality_key = matrix["primary"]
                by_key = {a["key"]: a for a in matrix["alternatives"]}

                def q(key: str | None, quality_key: str = quality_key, by_key: dict = by_key) -> float | None:
                    return None if key is None else by_key[key]["values"][quality_key]["value"]

                scenarios.append(
                    {
                        "dataset": matrix["context"]["dataset"],
                        "profile": profile_name,
                        "constraints": constraint_name,
                        "winner_a": modes["primary"]["winner"],
                        "winner_b": modes["topsis"]["winner"],
                        "winner_c": modes["robust"]["winner"],
                        "violates_a": winner_violates(result, "primary"),
                        "violates_b": winner_violates(result, "topsis"),
                        "violates_c": winner_violates(result, "robust"),
                        "a_indistinguishable_from": modes["primary"]["indistinguishable"],
                        "decoy_reversal_b": share(reversal_b),
                        "decoy_reversal_c": share(reversal_c),
                        "verdict_c": modes["robust"]["verdict"],
                        "local_share_c": modes["robust"].get("winner_local_share"),
                        "noise_share_c": modes["robust"].get("winner_noise_share"),
                        "quality_a": q(modes["primary"]["winner"]),
                        "quality_c": q(modes["robust"]["winner"]),
                        "influence_quality_vector": modes["topsis"].get("influence", {}).get(quality_key),
                        "declared_quality": problem.effective_weights()[quality_key],
                    }
                )
    return {"scenarios": scenarios, "summary": summarise(scenarios)}


def _winners_only(problem: DecisionProblem) -> dict[str, str | None]:
    """Победители B и C без анализа устойчивости (он не влияет на победителя)."""
    fast = DecisionProblem(
        problem.criteria,
        problem.alternatives,
        problem.constraints,
        problem.normalization,
        problem.primary,
        SensitivitySettings(samples=0, seed=SEED),
    )
    modes = analyse(fast)["modes"]
    return {"topsis": modes["topsis"]["winner"], "robust": modes["robust"]["winner"]}


def summarise(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    constrained = [s for s in scenarios if s["constraints"] != "none"]

    def rate(key: str, rows: list[dict[str, Any]]) -> float | None:
        values = [s[key] for s in rows if s[key] is not None]
        return round(sum(values) / len(values), 4) if values else None

    def mean(key: str, rows: list[dict[str, Any]]) -> float | None:
        values = [s[key] for s in rows if s[key] is not None]
        return round(statistics.fmean(values), 4) if values else None

    verdicts: dict[str, int] = {}
    for s in scenarios:
        verdicts[s["verdict_c"]] = verdicts.get(s["verdict_c"], 0) + 1
    no_constraints = [s for s in scenarios if s["constraints"] == "none"]
    return {
        "scenarios": len(scenarios),
        "constrained_scenarios": len(constrained),
        "violation_rate": {m: rate(f"violates_{m}", constrained) for m in ("a", "b", "c")},
        "decoy_reversal_rate": {"b": mean("decoy_reversal_b", scenarios), "c": mean("decoy_reversal_c", scenarios)},
        "scenarios_with_any_reversal_b": sum(1 for s in scenarios if (s["decoy_reversal_b"] or 0) > 0),
        "agreement_a_b_unconstrained": share([s["winner_a"] == s["winner_b"] for s in no_constraints]),
        "a_winner_indistinguishable_share": share([bool(s["a_indistinguishable_from"]) for s in no_constraints]),
        "verdicts_c": verdicts,
        "influence_vs_declared_quality_unconstrained": [
            {
                "dataset": s["dataset"],
                "profile": s["profile"],
                "declared": round(s["declared_quality"], 4),
                "actual": None if s["influence_quality_vector"] is None else round(s["influence_quality_vector"], 4),
            }
            for s in no_constraints
        ],
    }


def normalization_study(matrices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for matrix in matrices:
        for normalization in ("vector", "minmax"):
            problem = problem_for(matrix, PROFILES["balanced"], normalization=normalization)
            scored = list(problem.scored)
            m = [[float(a.value_of(c.key) or 0.0) for c in scored] for a in problem.alternatives]
            res = topsis(m, [c.weight for c in scored], [c.direction for c in scored], normalization)  # type: ignore[arg-type]
            inf = influence(res.weighted, scored)
            winner = problem.alternatives[res.ranks.index(1)].label
            rows.append(
                {
                    "dataset": matrix["context"]["dataset"],
                    "normalization": normalization,
                    "winner_b": winner,
                    "influence": {k: round(v, 4) for k, v in inf.items()},
                    "declared": {k: round(v, 4) for k, v in problem.effective_weights().items()},
                }
            )
    return rows


def synthetic_study(rng: random.Random) -> dict[str, Any]:
    """Контролируемые синтетические матрицы (не измерения!): частота rank reversal от приманки."""
    rows = []
    for n in (4, 8, 16):
        flips_b = flips_c = 0
        for _ in range(SYNTHETIC_MATRICES):
            matrix = [
                [rng.uniform(0.80, 0.97), math.exp(rng.gauss(3, 1)), math.exp(rng.gauss(4, 1.2))] for _ in range(n)
            ]
            directions = ["max", "min", "min"]
            weights = [rng.uniform(0.1, 1) for _ in range(3)]
            base = rng.choice(matrix)
            decoy = [
                base[0] - rng.uniform(0, 0.03) - 1e-6,
                base[1] * rng.uniform(1, 3) + 1e-6,
                base[2] * rng.uniform(1, 3) + 1e-6,
            ]
            before = topsis(matrix, weights, directions).ranks.index(1)  # type: ignore[arg-type]
            after_ranks = topsis([*matrix, decoy], weights, directions).ranks  # type: ignore[arg-type]
            flips_b += after_ranks.index(1) != before
            pareto = [row for row in matrix if not any(dominates(o, row, directions) for o in matrix)]  # type: ignore[arg-type]
            pareto_with = [
                row
                for row in [*matrix, decoy]
                if not any(dominates(o, row, directions) for o in [*matrix, decoy])  # type: ignore[arg-type]
            ]
            c_before = topsis(pareto, weights, directions)  # type: ignore[arg-type]
            c_after = topsis(pareto_with, weights, directions)  # type: ignore[arg-type]
            flips_c += pareto[c_before.ranks.index(1)] != pareto_with[c_after.ranks.index(1)]
        rows.append(
            {
                "alternatives": n,
                "matrices": SYNTHETIC_MATRICES,
                "reversal_rate_b": round(flips_b / SYNTHETIC_MATRICES, 4),
                "reversal_rate_c": round(flips_c / SYNTHETIC_MATRICES, 4),
            }
        )
    return {"label": "СИНТЕТИКА: контролируемые случайные матрицы, не результаты моделей", "rows": rows}


def runtime_study(rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for n in (5, 20, 50, 100):
        criteria = (
            Criterion("q", "q", "max", 0.5, group="quality"),
            Criterion("t", "t", "min", 0.3),
            Criterion("s", "s", "min", 0.2),
        )
        alternatives = tuple(
            Alternative(
                f"a{i}",
                f"a{i}",
                {
                    "q": Measurement(rng.uniform(0.8, 0.97), 0.01),
                    "t": Measurement(math.exp(rng.gauss(3, 1))),
                    "s": Measurement(math.exp(rng.gauss(4, 1))),
                },
            )
            for i in range(n)
        )
        problem = DecisionProblem(criteria, alternatives, sensitivity=SensitivitySettings(samples=2000, seed=SEED))
        start = time.perf_counter()
        result = analyse(problem)
        rows.append(
            {
                "alternatives": n,
                "pareto_size": len(result["modes"]["robust"]["pareto"]),
                "seconds_with_2000_samples": round(time.perf_counter() - start, 3),
            }
        )
    return rows


def main() -> None:
    directories, tag = cli_dirs()
    matrices = load_matrices(directories)
    if not matrices:
        raise SystemExit("Нет матриц в docs/research/data: сначала запустите measure_models.py")
    rng = random.Random(SEED)  # noqa: S311 — воспроизводимые сценарии, не криптография
    started = time.perf_counter()
    results = {
        "format": "dataarena.decision-experiment/1",
        "seed": SEED,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "measurement_environment": matrices[0]["context"]["environment"],
        "datasets": [m["context"] | {"environment": None} for m in matrices],
        "real": real_study(matrices, rng),
        "normalization": normalization_study(matrices),
        "synthetic": synthetic_study(rng),
        "runtime": runtime_study(rng),
    }
    results["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"results{tag}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            results["real"]["summary"] | {"synthetic": results["synthetic"], "runtime": results["runtime"]},
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
