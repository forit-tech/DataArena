"""Парето-доминирование.

Альтернатива a доминирует b, если a не хуже b ни по одному критерию и строго лучше
хотя бы по одному. Доминируемая альтернатива не может быть рациональным выбором
ни при каких неотрицательных весах — её исключение не теряет ни одного разумного
компромисса, но убирает её влияние на нормализацию и на идеал/антиидеал TOPSIS.
Одинаковые альтернативы друг друга не доминируют: обе остаются.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.domain.decision.models import Direction


def dominates(a: Sequence[float], b: Sequence[float], directions: Sequence[Direction]) -> bool:
    strictly_better = False
    for left, right, direction in zip(a, b, directions, strict=True):
        if direction == "max":
            if left < right:
                return False
            strictly_better = strictly_better or left > right
        else:
            if left > right:
                return False
            strictly_better = strictly_better or left < right
    return strictly_better


def dominated_by(matrix: Sequence[Sequence[float]], directions: Sequence[Direction]) -> list[list[int]]:
    """Для каждой строки — индексы строк, которые её доминируют (пусто — недоминируема)."""
    return [
        [j for j in range(len(matrix)) if j != i and dominates(matrix[j], matrix[i], directions)]
        for i in range(len(matrix))
    ]
