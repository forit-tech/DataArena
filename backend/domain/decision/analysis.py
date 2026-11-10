"""Три режима выбора на одной матрице и объяснение результата.

* **A. По основной метрике** — то, что делает человек, глядя на лидерборд:
  победитель по одной метрике качества, ограничения и ресурсы не учитываются.
* **B. Классический TOPSIS** — все полные альтернативы, ограничения не учитываются.
* **C. CP-TOPSIS-R** (constrained Pareto TOPSIS with robustness) — исследуемый подход:
  1. жёсткие ограничения: нарушившие и непроверяемые альтернативы исключаются
     из допустимого множества и **показываются** с причиной;
  2. Парето-фильтр: доминируемые допустимые альтернативы исключаются до ранжирования;
  3. TOPSIS на недоминируемом допустимом множестве;
  4. анализ устойчивости: интервалы весов, локальная и глобальная доля сценариев весов,
     устойчивость к шуму измерений; рекомендация получает вердикт устойчивости.

Свойство шага 2, проверяемое тестом: добавление альтернативы, доминируемой
какой-либо допустимой, не меняет ранжирование режима C (в классическом TOPSIS
такое добавление способно переставить лидеров — известный rank reversal).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from backend.domain.decision.models import (
    METHOD_VERSION,
    Alternative,
    Criterion,
    DecisionProblem,
    validate,
)
from backend.domain.decision.pareto import dominated_by
from backend.domain.decision.scales import fixed_closeness, fixed_distances, resolve_scales
from backend.domain.decision.sensitivity import (
    ShareResult,
    make_ranker,
    noise_shares,
    weight_intervals,
    weight_shares,
)
from backend.domain.decision.topsis import competition_ranks, topsis, winners

CORRELATION_WARNING = 0.9
MIN_ALTERNATIVES_FOR_CORRELATION = 4
#предупреждение о расхождении: заявлено не меньше 15%, а влияние меньше трети заявленного
INFLUENCE_DECLARED_MIN = 0.15
INFLUENCE_RATIO_WARNING = 1 / 3


def analyse(problem: DecisionProblem) -> dict[str, Any]:
    validate(problem)
    scored = list(problem.scored)
    effective = problem.effective_weights()
    weights = [effective[item.key] for item in scored]
    labels = {item.key: item.label for item in problem.alternatives}
    warnings: list[str] = []

    alternatives = [_describe_alternative(problem, alternative, scored) for alternative in problem.alternatives]
    by_key = {item["key"]: item for item in alternatives}
    complete = [alt for alt in problem.alternatives if not by_key[alt.key]["missing"]]

    primary = _primary_mode(problem, by_key)
    classic = _classic_mode(problem, scored, weights, complete, by_key, warnings)
    robust, robustness = _robust_mode(problem, scored, weights, complete, by_key, warnings)
    fixed = _fixed_mode(problem, scored, weights, complete, by_key, warnings)

    _warn_about_criteria(problem, scored, complete, warnings)
    _warn_about_influence(problem, classic, warnings)
    explanation = _explain(problem, scored, robust, robustness, labels)
    fixed["explanation"] = _explain(problem, scored, fixed, fixed, labels)

    return {
        "method": {
            "version": METHOD_VERSION,
            "normalization": problem.normalization,
            "seed": problem.sensitivity.seed,
            "samples": problem.sensitivity.samples,
            "concentration": problem.sensitivity.concentration,
            "grid_step": problem.sensitivity.grid_step,
            "stability_threshold": problem.sensitivity.stability_threshold,
            "input_sha256": problem_fingerprint(problem),
        },
        "criteria": [_describe_criterion(problem, item, effective) for item in problem.criteria],
        "alternatives": alternatives,
        "modes": {"primary": primary, "topsis": classic, "robust": robust, "fixed": fixed},
        "robustness": robustness,
        "agreement": {
            "primary_vs_topsis": primary["winner"] is not None and primary["winner"] == classic["winner"],
            "topsis_vs_robust": classic["winner"] is not None and classic["winner"] == robust["winner"],
            "primary_vs_robust": primary["winner"] is not None and primary["winner"] == robust["winner"],
            "robust_vs_fixed": robust["winner"] is not None and robust["winner"] == fixed["winner"],
        },
        "explanation": explanation,
        "warnings": warnings,
    }


def problem_fingerprint(problem: DecisionProblem) -> str:
    payload = json.dumps(asdict(problem), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _describe_criterion(problem: DecisionProblem, item: Criterion, effective: dict[str, float]) -> dict[str, Any]:
    constrained = any(constraint.criterion == item.key for constraint in problem.constraints)
    role = "scored" if item.weight > 0 else ("constraint_only" if constrained else "unused")
    return {
        "key": item.key,
        "label": item.label,
        "unit": item.unit,
        "direction": item.direction,
        "group": item.group,
        "weight": item.weight,
        "effective_weight": effective.get(item.key, 0.0),
        "role": role,
    }


def _describe_alternative(
    problem: DecisionProblem, alternative: Alternative, scored: list[Criterion]
) -> dict[str, Any]:
    missing = [item.key for item in scored if alternative.value_of(item.key) is None]
    constraints = []
    for constraint in problem.constraints:
        criterion = problem.criterion(constraint.criterion)
        observed = alternative.value_of(constraint.criterion)
        constraints.append(
            {
                "criterion": constraint.criterion,
                "op": constraint.op,
                "bound": constraint.bound,
                "description": constraint.describe(criterion.label, criterion.unit),
                "observed": observed,
                "satisfied": constraint.satisfied_by(observed),
            }
        )
    return {
        "key": alternative.key,
        "label": alternative.label,
        "meta": dict(alternative.meta),
        "values": {
            key: {"value": item.value, "std": item.std, "note": item.note} for key, item in alternative.values.items()
        },
        "missing": missing,
        "constraints": constraints,
        "violated": [item["description"] for item in constraints if item["satisfied"] is False],
        "unverified": [item["description"] for item in constraints if item["satisfied"] is None],
        #статус режима C заполняется ниже
        "status": "pending",
        "status_reason": "",
        "dominated_by": [],
    }


def _primary_mode(problem: DecisionProblem, by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = problem.primary_key()
    criterion = problem.criterion(key)
    measured = [alt for alt in problem.alternatives if alt.value_of(key) is not None]
    if not measured:
        return {"criterion": key, "ranking": [], "winner": None, "indistinguishable": [], "winner_violates": []}

    sign = 1.0 if criterion.direction == "max" else -1.0
    scores = [sign * float(alt.value_of(key) or 0.0) for alt in measured]
    ranks = competition_ranks(scores)
    best = measured[winners(scores)[0]]
    best_m = best.values[key]
    indistinguishable = []
    for alt in measured:
        if alt.key == best.key:
            continue
        other = alt.values[key]
        #та же граница, что в ModelArena: разница меньше собственного колебания
        measured_both = None not in (best_m.std, other.std, best_m.value, other.value)
        if measured_both and abs(float(best_m.value or 0) - float(other.value or 0)) <= max(
            float(best_m.std or 0), float(other.std or 0)
        ):
            indistinguishable.append(alt.key)

    ranking = sorted(
        (
            {"key": alt.key, "value": alt.value_of(key), "std": alt.values[key].std, "rank": rank}
            for alt, rank in zip(measured, ranks, strict=True)
        ),
        key=lambda item: (item["rank"], item["key"]),
    )
    return {
        "criterion": key,
        "ranking": ranking,
        "winner": best.key,
        "indistinguishable": indistinguishable,
        "winner_violates": by_key[best.key]["violated"],
    }


def _matrix(alternatives: list[Alternative], scored: list[Criterion]) -> list[list[float]]:
    return [[float(alt.value_of(item.key) or 0.0) for item in scored] for alt in alternatives]


def _classic_mode(
    problem: DecisionProblem,
    scored: list[Criterion],
    weights: list[float],
    complete: list[Alternative],
    by_key: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    excluded = [
        {"key": key, "reason": "нет измерений: " + ", ".join(problem.criterion(c).label for c in item["missing"])}
        for key, item in by_key.items()
        if item["missing"]
    ]
    if not complete:
        warnings.append("Ни у одной альтернативы нет полного набора измерений по выбранным критериям.")
        return {"ranking": [], "winner": None, "winner_violates": [], "excluded": excluded, "degenerate": []}

    result = topsis(_matrix(complete, scored), weights, [c.direction for c in scored], problem.normalization)
    ranking = _ranking(complete, result.closeness, result.d_plus, result.d_minus, result.ranks)
    winner = ranking[0]["key"]
    return {
        "ranking": ranking,
        "winner": winner,
        "winner_violates": by_key[winner]["violated"],
        "excluded": excluded,
        "degenerate": [scored[j].key for j in result.degenerate],
        "influence": influence(result.weighted, scored),
    }


def _ranking(
    alternatives: list[Alternative],
    closeness: list[float],
    d_plus: list[float],
    d_minus: list[float],
    ranks: list[int],
) -> list[dict[str, Any]]:
    rows = [
        {"key": alt.key, "closeness": c, "d_plus": dp, "d_minus": dm, "rank": r}
        for alt, c, dp, dm, r in zip(alternatives, closeness, d_plus, d_minus, ranks, strict=True)
    ]
    return sorted(rows, key=lambda item: (item["rank"], item["key"]))


def _robust_mode(
    problem: DecisionProblem,
    scored: list[Criterion],
    weights: list[float],
    complete: list[Alternative],
    by_key: dict[str, dict[str, Any]],
    warnings: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    directions = [c.direction for c in scored]
    settings = problem.sensitivity

    for key, item in by_key.items():
        if item["missing"]:
            item["status"] = "incomplete"
            item["status_reason"] = "Нет измерений: " + ", ".join(problem.criterion(c).label for c in item["missing"])
        elif item["violated"]:
            item["status"] = "infeasible"
            item["status_reason"] = "Нарушено: " + "; ".join(item["violated"])
        elif item["unverified"]:
            item["status"] = "unverified"
            item["status_reason"] = "Ограничение проверить нечем: " + "; ".join(item["unverified"])
        else:
            item["status"] = "feasible"
        _ = key

    feasible = [alt for alt in complete if by_key[alt.key]["status"] == "feasible"]
    empty_robustness: dict[str, Any] = {
        "local": {},
        "global": {},
        "noise": None,
        "noise_note": "",
        "intervals": [],
        "rank_share": {},
        "mean_rank_local": {},
    }
    if not feasible:
        warnings.append("Допустимых альтернатив нет: ни одна не проходит все ограничения с полными измерениями.")
        robust: dict[str, Any] = {
            "ranking": [],
            "winner": None,
            "pareto": [],
            "feasible_count": 0,
            "verdict": "none",
            "verdict_text": (
                "Рекомендации нет: допустимое множество пусто. Ослабьте ограничения или добавьте измерения."
            ),
        }
        return robust, empty_robustness

    matrix = _matrix(feasible, scored)
    dominance = dominated_by(matrix, directions)
    pareto = [alt for alt, by in zip(feasible, dominance, strict=True) if not by]
    for alt, by in zip(feasible, dominance, strict=True):
        if by:
            item = by_key[alt.key]
            item["status"] = "dominated"
            item["dominated_by"] = [feasible[index].key for index in by]
            item["status_reason"] = "Доминируется: " + ", ".join(feasible[index].label for index in by)
        else:
            by_key[alt.key]["status"] = "pareto"

    pareto_matrix = _matrix(pareto, scored)
    result = topsis(pareto_matrix, weights, directions, problem.normalization)
    ranking = _ranking(pareto, result.closeness, result.d_plus, result.d_minus, result.ranks)
    winner = ranking[0]["key"]
    winner_index = next(i for i, alt in enumerate(pareto) if alt.key == winner)
    ranker = make_ranker(directions, problem.normalization)
    keys = [alt.key for alt in pareto]

    local = weight_shares(
        pareto_matrix,
        weights,
        ranker,
        samples=settings.samples,
        seed=settings.seed,
        concentration=settings.concentration,
    )
    global_ = weight_shares(
        pareto_matrix, weights, ranker, samples=settings.samples, seed=settings.seed + 1, concentration=None
    )
    stds = [[alt.values[c.key].std or 0.0 for c in scored] for alt in pareto]
    unknown_std = sum(1 for alt in pareto for c in scored if alt.values[c.key].std is None)
    has_noise = any(value > 0 for row in stds for value in row)
    noise = (
        noise_shares(pareto_matrix, stds, weights, ranker, samples=settings.samples, seed=settings.seed + 2)
        if has_noise
        else None
    )
    noise_note = ""
    if not has_noise:
        noise_note = "Неопределённость измерений не задана ни для одного значения: устойчивость к шуму не оценивалась."
    elif unknown_std:
        noise_note = (
            f"У {unknown_std} значений неопределённость неизвестна; в анализе шума они считаются точными. "
            "Это оптимистичное допущение."
        )

    intervals = weight_intervals(pareto_matrix, weights, ranker, settings.grid_step)
    local_share = local.win_share[winner_index]
    noise_share = noise.win_share[winner_index] if noise else None
    verdict, verdict_text = _verdict(local_share, noise_share, len(pareto), settings.stability_threshold)

    def share_map(shares: ShareResult | None) -> dict[str, float]:
        return {} if shares is None else dict(zip(keys, shares.win_share, strict=True))

    robustness = {
        "local": share_map(local),
        "global": share_map(global_),
        "noise": share_map(noise) if noise else None,
        "noise_note": noise_note,
        "mean_rank_local": dict(zip(keys, local.mean_rank, strict=True)),
        "rank_share": dict(zip(keys, local.rank_share, strict=True)),
        "intervals": [
            {
                "criterion": scored[item.criterion].key,
                "current": item.current,
                "low": item.low,
                "high": item.high,
                "segments": [{"start": s.start, "end": s.end, "winner": keys[s.winner]} for s in item.segments],
            }
            for item in intervals
        ],
    }
    robust = {
        "ranking": ranking,
        "winner": winner,
        "pareto": keys,
        "feasible_count": len(feasible),
        "degenerate": [scored[j].key for j in result.degenerate],
        "influence": influence(result.weighted, scored),
        "verdict": verdict,
        "verdict_text": verdict_text,
        "winner_local_share": local_share,
        "winner_noise_share": noise_share,
    }
    return robust, robustness


def _fixed_mode(
    problem: DecisionProblem,
    scored: list[Criterion],
    weights: list[float],
    complete: list[Alternative],
    by_key: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    """Режим D: ограничения → полезности на фиксированных шкалах → TOPSIS с фиксированными идеалами.

    Допустимое множество то же, что у режима C. Парето-фильтр не нужен: при фиксированных
    идеалах доминирующая альтернатива не может стоять ниже доминируемой (монотонность),
    а лишние альтернативы ни на что не влияют.
    """
    settings = problem.sensitivity
    observed = {
        c.key: [float(alt.value_of(c.key) or 0) for alt in complete if alt.value_of(c.key) is not None] for c in scored
    }
    scales = resolve_scales(problem, scored, problem.scales, observed)
    described = [
        {
            "criterion": s.criterion,
            "kind": s.kind,
            "worst": s.worst,
            "best": s.best,
            "source": s.source,
            "note": s.note,
        }
        for s in scales
    ]
    observed_labels = [problem.criterion(s.criterion).label for s in scales if s.source == "observed"]
    if observed_labels:
        warnings.append(
            "Режим D: для «" + "», «".join(observed_labels) + "» шкала взята по наблюдаемым min/max. "
            "По этим критериям результат зависит от состава набора; задайте шкалу или бюджет (ограничение ≤)."
        )

    feasible = [alt for alt in complete if by_key[alt.key]["status"] in ("pareto", "dominated", "feasible")]
    empty: dict[str, Any] = {
        "ranking": [],
        "winner": None,
        "scales": described,
        "influence": {},
        "utilities": {},
        "verdict": "none",
        "verdict_text": "Рекомендации нет: допустимое множество пусто.",
        "local": {},
        "noise": None,
        "intervals": [],
    }
    if not feasible:
        return empty

    def to_utilities(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
        return [[scale.utility(value) for scale, value in zip(scales, row, strict=True)] for row in matrix]

    def ranker(matrix: Sequence[Sequence[float]], w: Sequence[float]) -> list[float]:
        return fixed_closeness(to_utilities(matrix), w)

    raw = _matrix(feasible, scored)
    utilities = to_utilities(raw)
    closeness = fixed_closeness(utilities, weights)
    d_plus, d_minus = fixed_distances(utilities, weights)
    ranking = _ranking(feasible, closeness, d_plus, d_minus, competition_ranks(closeness))
    winner = ranking[0]["key"]
    winner_index = next(i for i, alt in enumerate(feasible) if alt.key == winner)
    keys = [alt.key for alt in feasible]

    local = weight_shares(
        raw, weights, ranker, samples=settings.samples, seed=settings.seed, concentration=settings.concentration
    )
    stds = [[alt.values[c.key].std or 0.0 for c in scored] for alt in feasible]
    has_noise = any(value > 0 for row in stds for value in row)
    noise = (
        noise_shares(raw, stds, weights, ranker, samples=settings.samples, seed=settings.seed + 2)
        if has_noise
        else None
    )
    intervals = weight_intervals(raw, weights, ranker, settings.grid_step)
    noise_share = noise.win_share[winner_index] if noise else None
    verdict, verdict_text = _verdict(
        local.win_share[winner_index], noise_share, len(feasible), settings.stability_threshold
    )
    weighted = [[w * u for w, u in zip(weights, row, strict=True)] for row in utilities]
    return {
        "ranking": ranking,
        "winner": winner,
        "scales": described,
        "influence": influence(weighted, scored),
        "utilities": {
            alt.key: dict(zip([c.key for c in scored], row, strict=True))
            for alt, row in zip(feasible, utilities, strict=True)
        },
        "verdict": verdict,
        "verdict_text": verdict_text,
        "winner_local_share": local.win_share[winner_index],
        "winner_noise_share": noise_share,
        "local": dict(zip(keys, local.win_share, strict=True)),
        "noise": dict(zip(keys, noise.win_share, strict=True)) if noise else None,
        "intervals": [
            {
                "criterion": scored[item.criterion].key,
                "current": item.current,
                "low": item.low,
                "high": item.high,
                "segments": [{"start": s.start, "end": s.end, "winner": keys[s.winner]} for s in item.segments],
            }
            for item in intervals
        ],
    }


def influence(weighted: list[list[float]], scored: list[Criterion]) -> dict[str, float]:
    """Фактическое влияние критерия: доля размаха взвешенного столбца в сумме размахов.

    TOPSIS сравнивает альтернативы по расстояниям, а расстояние по критерию j не больше
    размаха w_j·(max r_j − min r_j). Если значения критерия почти не различаются
    в относительном масштабе (ROC-AUC 0.89 и 0.93 при векторной нормализации), его
    заявленный вес почти не влияет на выбор. Показатель описательный: он объясняет
    результат, но не входит в формулу.
    """
    spans = [max(row[j] for row in weighted) - min(row[j] for row in weighted) for j in range(len(scored))]
    total = sum(spans)
    if total == 0:
        return {item.key: 0.0 for item in scored}
    return {item.key: span / total for item, span in zip(scored, spans, strict=True)}


def _warn_about_influence(problem: DecisionProblem, classic: dict[str, Any], warnings: list[str]) -> None:
    effective = problem.effective_weights()
    for key, share in (classic.get("influence") or {}).items():
        declared = effective.get(key, 0.0)
        if declared >= INFLUENCE_DECLARED_MIN and share < declared * INFLUENCE_RATIO_WARNING:
            label = problem.criterion(key).label
            warnings.append(
                f"Вес «{label}» заявлен {declared:.0%}, но фактическое влияние на выбор — {share:.0%}: "
                "значения этого критерия почти не различаются в относительном масштабе, а другие различаются "
                "в разы. Если критерий важен, задайте по нему ограничение снизу (например, порог качества)."
            )


def _verdict(local: float, noise: float | None, pareto_size: int, threshold: float) -> tuple[str, str]:
    if pareto_size == 1:
        return "stable", "Допустимая недоминируемая альтернатива единственна: выбор не зависит от весов."
    weakest = min(local, noise) if noise is not None else local
    noise_part = f", при шуме измерений — {noise:.0%}" if noise is not None else ""
    text = f"Первая в {local:.0%} сценариев весов вокруг заявленных{noise_part}."
    if weakest >= threshold:
        return "stable", "Устойчивая рекомендация. " + text
    if weakest >= threshold / 2:
        return "conditional", "Условная рекомендация: при небольшом изменении приоритетов лидер меняется. " + text
    return "unstable", "Неустойчивая рекомендация: лидер определяется деталями весов или шумом. " + text


def _warn_about_criteria(
    problem: DecisionProblem, scored: list[Criterion], complete: list[Alternative], warnings: list[str]
) -> None:
    quality = [item.label for item in scored if item.group == "quality"]
    if len(quality) > 1:
        warnings.append(
            "Несколько метрик качества одновременно (" + ", ".join(quality) + "): качество учитывается "
            "несколько раз и получает больший суммарный вес, чем видно по отдельным весам."
        )

    if len(complete) >= MIN_ALTERNATIVES_FOR_CORRELATION:
        for a in range(len(scored)):
            for b in range(a + 1, len(scored)):
                xs = [float(alt.value_of(scored[a].key) or 0.0) for alt in complete]
                ys = [float(alt.value_of(scored[b].key) or 0.0) for alt in complete]
                r = _pearson(xs, ys)
                if r is not None and abs(r) >= CORRELATION_WARNING:
                    warnings.append(
                        f"Критерии «{scored[a].label}» и «{scored[b].label}» сильно коррелируют "
                        f"(r = {r:.2f} по {len(complete)} альтернативам): фактически это один критерий с двойным весом."
                    )

    for constraint in problem.constraints:
        if all(alt.value_of(constraint.criterion) is None for alt in problem.alternatives):
            label = problem.criterion(constraint.criterion).label
            warnings.append(
                f"Ограничение по «{label}» нечем проверить: этот критерий не измерен ни у одной альтернативы."
            )

    if problem.normalization == "minmax":
        warnings.append(
            "Min-max нормализация растягивает каждый критерий на [0, 1]: крошечная разница в качестве "
            "весит столько же, сколько огромная разница во времени. Базовый вариант TOPSIS — векторная."
        )


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def _explain(
    problem: DecisionProblem,
    scored: list[Criterion],
    robust: dict[str, Any],
    robustness: dict[str, Any],
    labels: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    winner = robust.get("winner")
    if winner is None:
        return [robust["verdict_text"]]

    alternatives = {alt.key: alt for alt in problem.alternatives}
    lines.append(f"Рекомендация: {labels[winner]}. {robust['verdict_text']}")
    ranking = robust["ranking"]
    if len(ranking) > 1:
        runner = ranking[1]["key"]
        advantages: list[str] = []
        drawbacks: list[str] = []
        for criterion in scored:
            a = alternatives[winner].value_of(criterion.key)
            b = alternatives[runner].value_of(criterion.key)
            if a is None or b is None or a == b:
                continue
            better = a > b if criterion.direction == "max" else a < b
            text = f"{criterion.label}: {a:g} против {b:g}{(' ' + criterion.unit) if criterion.unit else ''}"
            (advantages if better else drawbacks).append(text)
        lines.append(
            f"Против второй ({labels[runner]}, близость {ranking[1]['closeness']:.3f} против "
            f"{ranking[0]['closeness']:.3f}): лучше — "
            + ("; ".join(advantages) or "ни по одному критерию")
            + ("; хуже — " + "; ".join(drawbacks) if drawbacks else "")
            + "."
        )

    step = problem.sensitivity.grid_step
    for interval in robustness.get("intervals", []):
        criterion = problem.criterion(interval["criterion"])
        segments = interval["segments"]
        after = next((s for s in segments if s["start"] > interval["high"]), None)
        before = next((s for s in reversed(segments) if s["end"] < interval["low"]), None)
        current = interval["current"]
        if after is not None:
            if interval["high"] - current < step:
                lines.append(
                    f"Даже небольшое увеличение веса «{criterion.label}» (сейчас {current:.2f}) "
                    f"сделает первой {labels[after['winner']]}."
                )
            else:
                lines.append(
                    f"Если вес «{criterion.label}» вырастет с {current:.2f} выше {interval['high']:.2f}, "
                    f"первой станет {labels[after['winner']]}."
                )
        if before is not None:
            if current - interval["low"] < step:
                lines.append(
                    f"Даже небольшое уменьшение веса «{criterion.label}» (сейчас {current:.2f}) "
                    f"сделает первой {labels[before['winner']]}."
                )
            else:
                lines.append(
                    f"Если вес «{criterion.label}» опустится ниже {interval['low']:.2f}, "
                    f"первой станет {labels[before['winner']]}."
                )

    return lines
