"""Режим D: TOPSIS на фиксированных шкалах (исследуемая модификация, гипотеза H4).

Классический TOPSIS нормирует каждый критерий **по текущему набору альтернатив**:
векторная норма, идеал и антиидеал зависят от того, кто ещё участвует в сравнении.
Отсюда и rank reversal, и «сжатие» метрик качества.

Режим D переводит каждое значение в полезность u ∈ [0, 1] по шкале, заданной
**до** сравнения и не зависящей от состава набора:

* линейная шкала: u = (x − x⁰) / (x¹ − x⁰);
* логарифмическая: u = (ln x − ln x⁰) / (ln x¹ − ln x⁰) — для ресурсов (время, размер,
  память), где важна кратность: ускорение 2 → 1 мс ценится так же, как 200 → 100 мс;

x⁰ — значение, которому соответствует u = 0 («худшее, что имеет смысл»), x¹ — u = 1
(«лучше уже не нужно»). Значения за пределами шкалы обрезаются до [0, 1].

Идеал и антиидеал фиксированы: A⁺ = w (все u = 1), A⁻ = 0. Тогда
D⁺ = ‖w ⊙ (1 − u)‖, D⁻ = ‖w ⊙ u‖, и близость альтернативы зависит **только от её
собственных значений**. Следствия (доказаны в MATHEMATICAL_MODEL.md, разд. 9):
добавление или удаление любой другой альтернативы не меняет порядок остальных,
и доминирующая альтернатива никогда не стоит ниже доминируемой.

Шкалы выбираются содержательно, а не произвольно. Источник каждой шкалы показывается:

* ``natural`` — у метрики есть естественные границы: ROC-AUC 0.5 (случайный
  классификатор) … 1; R² 0 (предсказание средним) … 1;
* ``budget`` — для ресурса задано ограничение «≤ B»: B и есть худшее допустимое значение;
  лучшее по умолчанию B/100 (два порядка), параметр меняется пользователем;
* ``user`` — шкалу задал пользователь;
* ``observed`` — разумной шкалы нет, и взяты min/max наблюдаемых значений. Такая шкала
  зависит от набора, и свойства инвариантности для этого критерия **теряются** —
  об этом выдаётся предупреждение.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from backend.domain.decision.models import (
    Criterion,
    DecisionInputError,
    DecisionProblem,
    ScaleKind,
    ScaleOverride,
)

ScaleSource = Literal["natural", "budget", "user", "observed"]

#во сколько раз «лучшее» значение ресурса меньше бюджета, если пользователь его не задал
DEFAULT_LOG_SPAN = 100.0

#метрики с естественными границами: ключ → (x⁰, x¹, пояснение)
NATURAL: dict[str, tuple[float, float, str]] = {
    "roc_auc": (0.5, 1.0, "0.5 — случайный классификатор, 1 — идеальное разделение"),
    "r2": (0.0, 1.0, "0 — не лучше предсказания средним; отрицательные значения обрезаются до 0"),
    "accuracy": (0.0, 1.0, "границы доли; уровень случайного угадывания зависит от баланса классов и здесь не учтён"),
    "balanced_accuracy": (0.0, 1.0, "границы доли; уровень случайного угадывания 1/k не учтён"),
    "f1": (0.0, 1.0, "границы доли"),
    "f1_macro": (0.0, 1.0, "границы доли"),
    "f1_weighted": (0.0, 1.0, "границы доли"),
    "precision": (0.0, 1.0, "границы доли"),
    "recall": (0.0, 1.0, "границы доли"),
    "pr_auc": (0.0, 1.0, "нижняя граница по смыслу — доля положительного класса; здесь взят 0"),
    "average_precision": (0.0, 1.0, "нижняя граница по смыслу — доля положительного класса; здесь взят 0"),
}
RESOURCE_GROUPS = {"time", "size", "memory", "cost"}


@dataclass(frozen=True)
class Scale:
    criterion: str
    kind: ScaleKind
    worst: float
    best: float
    source: ScaleSource
    note: str = ""

    def utility(self, value: float) -> float:
        if self.kind == "log":
            if value <= 0:
                #ноль миллисекунд лучше любого положительного значения ресурса
                return 1.0 if self.best < self.worst else 0.0
            u = (math.log(value) - math.log(self.worst)) / (math.log(self.best) - math.log(self.worst))
        else:
            u = (value - self.worst) / (self.best - self.worst)
        return min(1.0, max(0.0, u))


def validate_scale(scale: Scale, criterion: Criterion) -> None:
    if not (math.isfinite(scale.worst) and math.isfinite(scale.best)) or scale.worst == scale.best:
        raise DecisionInputError(f"Шкала «{criterion.label}»: границы должны быть конечными и различными.")
    if scale.kind == "log" and (scale.worst <= 0 or scale.best <= 0):
        raise DecisionInputError(f"Логарифмическая шкала «{criterion.label}» требует положительных границ.")
    better_is_higher = scale.best > scale.worst
    if better_is_higher != (criterion.direction == "max"):
        raise DecisionInputError(
            f"Шкала «{criterion.label}» противоречит направлению критерия: для "
            f"{'max' if criterion.direction == 'max' else 'min'} лучшее значение должно быть "
            f"{'больше' if criterion.direction == 'max' else 'меньше'} худшего."
        )


def resolve_scales(
    problem: DecisionProblem,
    scored: Sequence[Criterion],
    overrides: Sequence[ScaleOverride],
    observed: dict[str, list[float]],
) -> list[Scale]:
    """Шкала на каждый оцениваемый критерий: заданная пользователем, естественная, бюджетная или наблюдаемая."""
    by_override = {item.criterion: item for item in overrides}
    result: list[Scale] = []
    for criterion in scored:
        override = by_override.get(criterion.key)
        if override is not None:
            scale = Scale(criterion.key, override.kind, override.worst, override.best, "user", "задано пользователем")
        else:
            scale = _default_scale(problem, criterion, observed.get(criterion.key, []))
        validate_scale(scale, criterion)
        result.append(scale)
    return result


def _default_scale(problem: DecisionProblem, criterion: Criterion, values: list[float]) -> Scale:
    natural = NATURAL.get(criterion.key.lower())
    if natural is not None and criterion.direction == "max":
        return Scale(criterion.key, "linear", natural[0], natural[1], "natural", natural[2])

    if criterion.group in RESOURCE_GROUPS and criterion.direction == "min":
        budget = next(
            (c.bound for c in problem.constraints if c.criterion == criterion.key and c.op == "<=" and c.bound > 0),
            None,
        )
        if budget is not None:
            return Scale(
                criterion.key,
                "log",
                budget,
                budget / DEFAULT_LOG_SPAN,
                "budget",
                f"0 — бюджет из ограничения ({budget:g}), 1 — в {DEFAULT_LOG_SPAN:g} раз меньше бюджета",
            )
        positive = [value for value in values if value > 0]
        if positive and max(positive) > min(positive):
            return Scale(
                criterion.key,
                "log",
                max(positive),
                min(positive),
                "observed",
                "бюджет не задан: шкала по наблюдаемым min/max — зависит от состава набора",
            )

    if values and max(values) > min(values):
        low, high = min(values), max(values)
        worst, best = (low, high) if criterion.direction == "max" else (high, low)
        return Scale(
            criterion.key, "linear", worst, best, "observed", "естественной шкалы нет: min/max наблюдаемых значений"
        )

    #все значения одинаковы или их нет: шкала, на которой критерий не различает альтернативы
    anchor = values[0] if values else 0.0
    span = abs(anchor) or 1.0
    worst, best = (anchor - span, anchor) if criterion.direction == "max" else (anchor + span, anchor)
    return Scale(criterion.key, "linear", worst, best, "observed", "значения не различаются")


def fixed_closeness(utilities: Sequence[Sequence[float]], weights: Sequence[float]) -> list[float]:
    """Близость к фиксированному идеалу (u = 1) относительно фиксированного антиидеала (u = 0)."""
    total = float(sum(weights))
    w = [item / total for item in weights]
    result = []
    for row in utilities:
        d_plus = math.sqrt(sum((wj * (1 - u)) ** 2 for wj, u in zip(w, row, strict=True)))
        d_minus = math.sqrt(sum((wj * u) ** 2 for wj, u in zip(w, row, strict=True)))
        result.append(0.5 if d_plus + d_minus == 0 else d_minus / (d_plus + d_minus))
    return result


def fixed_distances(utilities: Sequence[Sequence[float]], weights: Sequence[float]) -> tuple[list[float], list[float]]:
    total = float(sum(weights))
    w = [item / total for item in weights]
    plus = [math.sqrt(sum((wj * (1 - u)) ** 2 for wj, u in zip(w, row, strict=True))) for row in utilities]
    minus = [math.sqrt(sum((wj * u) ** 2 for wj, u in zip(w, row, strict=True))) for row in utilities]
    return plus, minus
