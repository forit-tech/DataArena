"""Многокритериальный выбор: описание задачи.

Задача выбора — это матрица «альтернатива × критерий», направление каждого критерия,
веса, жёсткие ограничения и параметры анализа устойчивости. Здесь только описание
и его проверка; вычисления — в соседних модулях.

Правила, которым подчинено описание:

* **отсутствующее измерение — это `None`, а не ноль.** Ноль миллисекунд инференса
  сделал бы модель идеальной по скорости, хотя её просто не измеряли;
* **вес ноль — законная роль критерия.** Такой критерий не участвует в оценке,
  но по нему можно задать ограничение («размер ≤ 100 МБ, но размер мне не важен»);
* **веса задаёт человек в любом масштабе**, а метод работает с нормированными:
  эффективные веса показываются отдельно, чтобы «5, 3, 2» и «0.5, 0.3, 0.2»
  не выглядели разными настройками.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from backend.core.errors import AppError

Direction = Literal["max", "min"]
Normalization = Literal["vector", "minmax"]
ConstraintOp = Literal["<=", ">="]
ScaleKind = Literal["linear", "log"]

METHOD_VERSION = "cp-topsis-r/2"

MAX_ALTERNATIVES = 200
MAX_CRITERIA = 30
MAX_SAMPLES = 20_000


class DecisionInputError(AppError):
    status_code = 422
    code = "decision_invalid"


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    direction: Direction
    weight: float
    unit: str = ""
    #смысловая группа: quality, time, size, memory, cost, stability. По ней ловится
    #двойной учёт качества, когда ROC-AUC и F1 выбраны одновременно
    group: str = ""


@dataclass(frozen=True)
class Measurement:
    value: float | None
    #стандартная неопределённость значения в тех же единицах. None — неизвестна,
    #и это не то же самое, что «точное»
    std: float | None = None
    note: str = ""


@dataclass(frozen=True)
class Alternative:
    key: str
    label: str
    values: Mapping[str, Measurement]
    meta: Mapping[str, str] = field(default_factory=dict)

    def value_of(self, criterion: str) -> float | None:
        measurement = self.values.get(criterion)
        return None if measurement is None else measurement.value


@dataclass(frozen=True)
class Constraint:
    criterion: str
    op: ConstraintOp
    bound: float

    def describe(self, label: str, unit: str = "") -> str:
        suffix = f" {unit}" if unit else ""
        return f"{label} {'≤' if self.op == '<=' else '≥'} {_fmt(self.bound)}{suffix}"

    def satisfied_by(self, observed: float | None) -> bool | None:
        #None — «проверить нечем», и это третье состояние, а не провал
        if observed is None:
            return None
        return observed <= self.bound if self.op == "<=" else observed >= self.bound


@dataclass(frozen=True)
class ScaleOverride:
    """Шкала критерия для режима D, заданная пользователем: значение worst → 0, best → 1."""

    criterion: str
    kind: ScaleKind
    worst: float
    best: float


@dataclass(frozen=True)
class SensitivitySettings:
    samples: int = 2000
    seed: int = 20261109
    #концентрация распределения Дирихле вокруг заявленных весов: чем больше,
    #тем ближе сценарии к заявленному; 50 даёт отклонение порядка ±0.05 на вес 0.3
    concentration: float = 50.0
    grid_step: float = 0.01
    #доля сценариев, начиная с которой рекомендация называется устойчивой
    stability_threshold: float = 0.6


@dataclass(frozen=True)
class DecisionProblem:
    criteria: tuple[Criterion, ...]
    alternatives: tuple[Alternative, ...]
    constraints: tuple[Constraint, ...] = ()
    normalization: Normalization = "vector"
    #ключ основной метрики для режима «по одной метрике»; None — выбрать автоматически
    primary: str | None = None
    sensitivity: SensitivitySettings = field(default_factory=SensitivitySettings)
    #шкалы режима D; критерии без явной шкалы получают шкалу по умолчанию (scales.py)
    scales: tuple[ScaleOverride, ...] = ()

    def criterion(self, key: str) -> Criterion:
        return next(item for item in self.criteria if item.key == key)

    @property
    def scored(self) -> tuple[Criterion, ...]:
        return tuple(item for item in self.criteria if item.weight > 0)

    def effective_weights(self) -> dict[str, float]:
        total = sum(item.weight for item in self.scored)
        return {item.key: item.weight / total for item in self.scored}

    def primary_key(self) -> str:
        if self.primary is not None:
            return self.primary
        quality = [item for item in self.scored if item.group == "quality"]
        if quality:
            return quality[0].key
        maximised = [item for item in self.scored if item.direction == "max"]
        return (maximised or list(self.scored))[0].key


def validate(problem: DecisionProblem) -> None:
    """Отказать с понятной причиной, а не посчитать что-то бессмысленное."""
    keys = [item.key for item in problem.criteria]
    if not problem.criteria:
        raise DecisionInputError("Не выбран ни один критерий.")
    if len(problem.criteria) > MAX_CRITERIA:
        raise DecisionInputError(f"Критериев больше {MAX_CRITERIA}: такую матрицу нельзя объяснить человеку.")
    if len(set(keys)) != len(keys):
        raise DecisionInputError("Ключи критериев повторяются.", details={"criteria": keys})

    for item in problem.criteria:
        if item.direction not in ("max", "min"):
            raise DecisionInputError(f"У критерия «{item.label}» направление должно быть max или min.")
        if not math.isfinite(item.weight) or item.weight < 0:
            raise DecisionInputError(
                f"Вес критерия «{item.label}» должен быть конечным неотрицательным числом.",
                details={"criterion": item.key, "weight": str(item.weight)},
            )

    if not problem.scored:
        raise DecisionInputError(
            "Сумма весов равна нулю: нечего оптимизировать. Дайте хотя бы одному критерию вес больше нуля."
        )

    alt_keys = [item.key for item in problem.alternatives]
    if len(problem.alternatives) < 2:
        raise DecisionInputError("Для выбора нужно хотя бы две альтернативы.")
    if len(problem.alternatives) > MAX_ALTERNATIVES:
        raise DecisionInputError(f"Альтернатив больше {MAX_ALTERNATIVES}.")
    if len(set(alt_keys)) != len(alt_keys):
        raise DecisionInputError("Ключи альтернатив повторяются.", details={"alternatives": alt_keys})

    for alternative in problem.alternatives:
        for key, measurement in alternative.values.items():
            value, std = measurement.value, measurement.std
            if value is not None and not math.isfinite(value):
                raise DecisionInputError(f"У «{alternative.label}» значение «{key}» не является конечным числом.")
            if std is not None and (not math.isfinite(std) or std < 0):
                raise DecisionInputError(
                    f"У «{alternative.label}» неопределённость «{key}» должна быть неотрицательной."
                )

    for constraint in problem.constraints:
        if constraint.criterion not in keys:
            raise DecisionInputError(f"Ограничение ссылается на неизвестный критерий «{constraint.criterion}».")
        if constraint.op not in ("<=", ">="):
            raise DecisionInputError("Ограничение должно быть вида ≤ или ≥.")
        if not math.isfinite(constraint.bound):
            raise DecisionInputError("Граница ограничения должна быть конечным числом.")

    for scale in problem.scales:
        if scale.criterion not in keys:
            raise DecisionInputError(f"Шкала задана для неизвестного критерия «{scale.criterion}».")
        if scale.kind not in ("linear", "log"):
            raise DecisionInputError("Шкала должна быть linear или log.")

    if problem.primary is not None and problem.primary not in keys:
        raise DecisionInputError(f"Основная метрика «{problem.primary}» не входит в критерии.")

    settings = problem.sensitivity
    if not 0 <= settings.samples <= MAX_SAMPLES:
        raise DecisionInputError(f"Число сценариев должно быть от 0 до {MAX_SAMPLES}.")
    if settings.concentration <= 0 or not math.isfinite(settings.concentration):
        raise DecisionInputError("Концентрация сценариев весов должна быть положительной.")
    if not 0.001 <= settings.grid_step <= 0.25:
        raise DecisionInputError("Шаг сетки весов должен быть от 0.001 до 0.25.")
    if not 0 < settings.stability_threshold <= 1:
        raise DecisionInputError("Порог устойчивости должен быть в (0, 1].")


def _fmt(value: float) -> str:
    return f"{value:g}"
