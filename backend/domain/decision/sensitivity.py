"""Анализ устойчивости ранжирования.

Три независимых вопроса, и ни один не подменяет другой:

1. **Интервалы устойчивости весов** (one-at-a-time). Вес критерия j проходит сетку
   t ∈ [0, 1], остальные веса масштабируются пропорционально: w_k(t) = w_k·(1 − t)/(1 − w_j).
   Ответ: в каком диапазоне веса j победитель не меняется и кто побеждает за его краями.
2. **Локальная устойчивость к весам.** Веса сэмплируются из распределения Дирихле
   с параметрами α_k = κ·w_k вокруг заявленных (κ — концентрация). Доля сценариев,
   в которых альтернатива первая, — мера того, насколько выбор зависит от неточности
   самих весов. Это **не вероятность** того, что модель «на самом деле лучше».
3. **Глобальная доля пространства весов** (идея SMAA, Lahdelma et al., 1998):
   веса равномерно распределены по симплексу (Дирихле с α = 1).
4. **Устойчивость к шуму измерений.** При фиксированных весах значения сэмплируются
   из N(x, σ²), где σ — заявленная неопределённость (например, разброс по фолдам).

Все случайные величины берутся из `random.Random(seed)`: повторный запуск с тем же
seed даёт побитово тот же результат.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from backend.domain.decision.models import Direction, Normalization
from backend.domain.decision.topsis import topsis, winners

Ranker = Callable[[Sequence[Sequence[float]], Sequence[float]], list[float]]


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    winner: int


@dataclass(frozen=True)
class WeightInterval:
    criterion: int
    current: float
    #диапазон веса, в котором победитель тот же, что при заявленных весах
    low: float
    high: float
    segments: list[Segment]


@dataclass(frozen=True)
class ShareResult:
    #доля сценариев, в которых альтернатива первая (ничья делит сценарий поровну)
    win_share: list[float]
    #доля сценариев, в которых альтернатива заняла место r (строка — альтернатива)
    rank_share: list[list[float]]
    mean_rank: list[float]
    samples: int


def make_ranker(directions: Sequence[Direction], normalization: Normalization) -> Ranker:
    def rank(matrix: Sequence[Sequence[float]], weights: Sequence[float]) -> list[float]:
        return topsis(matrix, weights, directions, normalization).closeness

    return rank


def _winner(scores: Sequence[float]) -> int:
    #при ничьей победителем интервала считается первый по порядку ввода: правило
    #объявлено и детерминировано
    return winners(scores)[0]


def weight_intervals(
    matrix: Sequence[Sequence[float]],
    weights: Sequence[float],
    ranker: Ranker,
    step: float,
) -> list[WeightInterval]:
    total = float(sum(weights))
    w = [item / total for item in weights]
    base_winner = _winner(ranker(matrix, w))
    points = round(1 / step)
    result: list[WeightInterval] = []

    for j, current in enumerate(w):
        others = 1.0 - current
        if others <= 0:
            #единственный значимый критерий: перераспределять нечего
            result.append(WeightInterval(j, current, 0.0, 1.0, [Segment(0.0, 1.0, base_winner)]))
            continue

        grid = sorted({k / points for k in range(points + 1)} | {current})
        owners: list[tuple[float, int]] = []
        for t in grid:
            scenario = [t if k == j else w[k] * (1 - t) / others for k in range(len(w))]
            if sum(scenario) <= 0:
                continue
            owners.append((t, _winner(ranker(matrix, scenario))))

        segments: list[Segment] = []
        for t, owner in owners:
            if segments and segments[-1].winner == owner:
                segments[-1] = Segment(segments[-1].start, t, owner)
            else:
                segments.append(Segment(t, t, owner))

        holding = next(
            (
                segment
                for segment in segments
                if segment.start <= current <= segment.end and segment.winner == base_winner
            ),
            None,
        )
        low, high = (holding.start, holding.end) if holding else (current, current)
        result.append(WeightInterval(j, current, low, high, segments))

    return result


def _dirichlet(rng: random.Random, alphas: Sequence[float]) -> list[float]:
    draws = [rng.gammavariate(alpha, 1.0) for alpha in alphas]
    total = sum(draws)
    if total <= 0:
        return [1.0 / len(alphas)] * len(alphas)
    return [draw / total for draw in draws]


def _shares(outcomes: list[list[float]], size: int) -> ShareResult:
    wins = [0.0] * size
    rank_counts = [[0.0] * size for _ in range(size)]
    rank_sum = [0.0] * size
    for scores in outcomes:
        top = winners(scores)
        for index in top:
            wins[index] += 1.0 / len(top)
        order = sorted(range(size), key=lambda index: (-scores[index], index))
        for position, index in enumerate(order):
            rank_counts[index][position] += 1
            rank_sum[index] += position + 1
    n = max(len(outcomes), 1)
    return ShareResult(
        win_share=[item / n for item in wins],
        rank_share=[[count / n for count in row] for row in rank_counts],
        mean_rank=[item / n for item in rank_sum],
        samples=len(outcomes),
    )


def weight_shares(
    matrix: Sequence[Sequence[float]],
    weights: Sequence[float],
    ranker: Ranker,
    *,
    samples: int,
    seed: int,
    concentration: float | None,
) -> ShareResult:
    """concentration=None — равномерно по симплексу (глобально), иначе вокруг заявленных весов."""
    rng = random.Random(seed)
    total = float(sum(weights))
    alphas = [1.0] * len(weights) if concentration is None else [max(concentration * w / total, 1e-3) for w in weights]
    outcomes = [ranker(matrix, _dirichlet(rng, alphas)) for _ in range(samples)]
    return _shares(outcomes, len(matrix))


def noise_shares(
    matrix: Sequence[Sequence[float]],
    stds: Sequence[Sequence[float]],
    weights: Sequence[float],
    ranker: Ranker,
    *,
    samples: int,
    seed: int,
) -> ShareResult:
    rng = random.Random(seed)
    outcomes = []
    for _ in range(samples):
        noisy = [
            [value + (rng.gauss(0.0, sigma) if sigma > 0 else 0.0) for value, sigma in zip(row, sigmas, strict=True)]
            for row, sigmas in zip(matrix, stds, strict=True)
        ]
        outcomes.append(ranker(noisy, weights))
    return _shares(outcomes, len(matrix))
