"""Классический TOPSIS (Hwang & Yoon, 1981) — базовый метод сравнения.

Для матрицы X (m альтернатив × n критериев), весов w (Σw = 1) и направлений:

1. нормализация: векторная r_ij = x_ij / sqrt(Σ_i x_ij²) или min-max
   r_ij = (x_ij − min_j) / (max_j − min_j);
2. взвешивание: v_ij = w_j · r_ij;
3. идеал A⁺_j = max_i v_ij для max-критерия и min_i v_ij для min-критерия,
   антиидеал A⁻_j — наоборот;
4. расстояния D⁺_i = ‖v_i − A⁺‖₂, D⁻_i = ‖v_i − A⁻‖₂;
5. близость C_i = D⁻_i / (D⁺_i + D⁻_i) ∈ [0, 1], больше — лучше.

Краевые случаи решены явно, а не делением на ноль:

* столбец из одних нулей (векторная норма 0) или из одинаковых значений (размах 0
  при min-max) не различает альтернативы — его вклад нулевой, и критерий
  возвращается в списке вырожденных;
* если D⁺ + D⁻ = 0 для альтернативы, значит идеал совпал с антиидеалом по всем
  взвешенным критериям, то есть все альтернативы неразличимы: близость 0.5 у всех.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from backend.domain.decision.models import Direction, Normalization

#две близости, отличающиеся меньше, считаются равными: иначе порядок решала бы
#ошибка округления, а не данные
TIE_TOLERANCE = 1e-12


@dataclass(frozen=True)
class TopsisResult:
    closeness: list[float]
    d_plus: list[float]
    d_minus: list[float]
    normalized: list[list[float]]
    weighted: list[list[float]]
    ideal: list[float]
    anti_ideal: list[float]
    ranks: list[int]
    degenerate: list[int]
    weights: list[float]


def normalise_weights(weights: Sequence[float]) -> list[float]:
    total = float(sum(weights))
    if total <= 0 or not math.isfinite(total):
        raise ValueError("Сумма весов должна быть положительной.")
    return [float(item) / total for item in weights]


def topsis(
    matrix: Sequence[Sequence[float]],
    weights: Sequence[float],
    directions: Sequence[Direction],
    normalization: Normalization = "vector",
) -> TopsisResult:
    rows = len(matrix)
    cols = len(weights)
    if rows == 0:
        raise ValueError("Пустая матрица.")
    if any(len(row) != cols for row in matrix) or len(directions) != cols:
        raise ValueError("Размеры матрицы, весов и направлений не совпадают.")

    w = normalise_weights(weights)
    normalized = [[0.0] * cols for _ in range(rows)]
    degenerate: list[int] = []

    for j in range(cols):
        column = [float(matrix[i][j]) for i in range(rows)]
        if normalization == "vector":
            norm = math.sqrt(sum(value * value for value in column))
            if norm == 0:
                degenerate.append(j)
                continue
            for i in range(rows):
                normalized[i][j] = column[i] / norm
            if max(column) == min(column):
                degenerate.append(j)
        else:
            low, high = min(column), max(column)
            if high == low:
                degenerate.append(j)
                continue
            for i in range(rows):
                normalized[i][j] = (column[i] - low) / (high - low)

    weighted = [[w[j] * normalized[i][j] for j in range(cols)] for i in range(rows)]
    ideal: list[float] = []
    anti: list[float] = []
    for j in range(cols):
        column = [weighted[i][j] for i in range(rows)]
        best, worst = (max(column), min(column)) if directions[j] == "max" else (min(column), max(column))
        ideal.append(best)
        anti.append(worst)

    d_plus = [math.sqrt(sum((weighted[i][j] - ideal[j]) ** 2 for j in range(cols))) for i in range(rows)]
    d_minus = [math.sqrt(sum((weighted[i][j] - anti[j]) ** 2 for j in range(cols))) for i in range(rows)]
    closeness = [0.5 if d_plus[i] + d_minus[i] == 0 else d_minus[i] / (d_plus[i] + d_minus[i]) for i in range(rows)]

    return TopsisResult(
        closeness=closeness,
        d_plus=d_plus,
        d_minus=d_minus,
        normalized=normalized,
        weighted=weighted,
        ideal=ideal,
        anti_ideal=anti,
        ranks=competition_ranks(closeness),
        degenerate=sorted(set(degenerate)),
        weights=w,
    )


def competition_ranks(scores: Sequence[float]) -> list[int]:
    """Ранги «1, 1, 3»: равные по близости делят место, а не получают его по порядку ввода."""
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    ranks = [0] * len(scores)
    for position, index in enumerate(order):
        if position > 0 and abs(scores[order[position - 1]] - scores[index]) <= TIE_TOLERANCE:
            ranks[index] = ranks[order[position - 1]]
        else:
            ranks[index] = position + 1
    return ranks


def winners(scores: Sequence[float]) -> list[int]:
    """Индексы всех альтернатив, делящих первое место."""
    best = max(scores)
    return [index for index, value in enumerate(scores) if best - value <= TIE_TOLERANCE]
