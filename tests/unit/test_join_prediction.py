"""Прогноз числа строк соединения равен настоящему — при любых ключах.

Анализ и выполнение обязаны видеть ключи одинаково. Прежде анализ сравнивал значения как
текст, а соединение — как числа: Float32 против Float64 давал прогноз «пары найдутся»
и пустой результат, Int против Float — прогноз «пар нет» и пятисотку при выполнении,
NaN совпадал с NaN только в прогнозе, а -0.0 с 0.0 — только в соединении.

Случайные наборы — с фиксированным зерном: найденное расхождение воспроизводится.
"""

from __future__ import annotations

import random

import polars as pl
import pytest

from backend.domain.joins.models import JoinKind, JoinSpec, KeyPair
from backend.services.joins.analysis import analyse
from backend.services.joins.execute import build

KEY_TYPES = {
    "int64": (pl.Int64, lambda r: r.choice([1, 2, 3, None])),
    "int32": (pl.Int32, lambda r: r.choice([1, 2, 3, None])),
    "uint8": (pl.UInt8, lambda r: r.choice([1, 2, 3, None])),
    "float64": (pl.Float64, lambda r: r.choice([1.0, 2.0, 0.1, -0.0, 0.0, float("nan"), None])),
    "float32": (pl.Float32, lambda r: r.choice([1.0, 2.0, 0.1, 0.0, None])),
    "string": (pl.Utf8, lambda r: r.choice(["1", "2", "a", "", "ё", None])),
    "categorical": (pl.Categorical, lambda r: r.choice(["1", "2", "a", None])),
}

PAIRS = [
    ("int64", "int64"),
    ("int64", "int32"),
    ("int64", "uint8"),
    ("int64", "float64"),
    ("float32", "float64"),
    ("float64", "float64"),
    ("string", "string"),
    ("string", "categorical"),
    ("categorical", "categorical"),
]


def frame(rng: random.Random, name: str, kind: str, rows: int) -> pl.LazyFrame:
    dtype, value = KEY_TYPES[kind]

    return pl.LazyFrame(
        {name: pl.Series([value(rng) for _ in range(rows)], dtype=pl.Utf8 if dtype == pl.Categorical else None)}
    ).with_columns(pl.col(name).cast(dtype), pl.lit(1).alias(f"{name}_v"))


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: f"{pair[0]}-{pair[1]}")
@pytest.mark.parametrize("kind", list(JoinKind))
def test_the_forecast_equals_the_result(seed: int, pair: tuple[str, str], kind: JoinKind) -> None:
    rng = random.Random(seed)  # noqa: S311 - воспроизводимый набор, не криптография
    left = frame(rng, "k", pair[0], rng.randint(0, 12))
    right = frame(rng, "r", pair[1], rng.randint(0, 12))
    spec = JoinSpec(
        left_dataset_id="ds_l",
        right_dataset_id="ds_r",
        keys=(KeyPair("k", "r"),),
        kind=kind,
        right_suffix="_2",
    )

    predicted = analyse(left, right, spec).estimated_rows
    actual = build(left, right, spec).select(pl.len()).collect().item()

    assert predicted == actual, f"seed={seed}"


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("kind", list(JoinKind))
def test_the_forecast_equals_the_result_for_a_composite_key(seed: int, kind: JoinKind) -> None:
    rng = random.Random(seed)  # noqa: S311 - воспроизводимый набор, не криптография

    def side(first: str, second: str) -> pl.LazyFrame:
        rows = rng.randint(0, 15)

        return pl.LazyFrame(
            {
                first: [rng.choice([1, 2, None]) for _ in range(rows)],
                second: [rng.choice(["a", "b", None]) for _ in range(rows)],
            },
            schema={first: pl.Int64, second: pl.Utf8},
        )

    spec = JoinSpec(
        left_dataset_id="ds_l",
        right_dataset_id="ds_r",
        keys=(KeyPair("a", "x"), KeyPair("b", "y")),
        kind=kind,
        right_suffix="_2",
    )
    left, right = side("a", "b"), side("x", "y")

    assert analyse(left, right, spec).estimated_rows == build(left, right, spec).select(pl.len()).collect().item()
