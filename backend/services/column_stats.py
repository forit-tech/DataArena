"""Статистика по одной колонке для панели в шапке таблицы.

Всё считается на стороне сервера одним планом. Частые значения ограничены сверху:
колонка со ста тысячами уникальных значений не должна превращать панель в мегабайты JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from backend.domain.dataset.json_values import json_safe_value

#сколько частых значений показывать: больше десяти в панели не читается,
#а на колонке высокой кардинальности их подсчёт становится основной стоимостью запроса
TOP_VALUES_LIMIT = 10
#примеры значений: нужны, чтобы понять смысл колонки, а не увидеть данные целиком
EXAMPLES_LIMIT = 5
#число интервалов гистограммы фиксировано: адаптивное разбиение делает картинки
#несравнимыми между колонками
HISTOGRAM_BINS = 20


@dataclass(frozen=True, slots=True)
class ColumnStatistics:
    name: str
    dtype: str
    row_count: int
    null_count: int
    unique_count: int
    numeric: dict[str, Any] | None
    top_values: list[dict[str, Any]] | None
    histogram: dict[str, Any] | None
    examples: list[Any]

    @property
    def null_ratio(self) -> float:
        return round(self.null_count / self.row_count, 6) if self.row_count else 0.0

    @property
    def unique_ratio(self) -> float:
        return round(self.unique_count / self.row_count, 6) if self.row_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": self.dtype,
            "row_count": self.row_count,
            "null_count": self.null_count,
            "null_ratio": self.null_ratio,
            "unique_count": self.unique_count,
            "unique_ratio": self.unique_ratio,
            "numeric": self.numeric,
            "top_values": self.top_values,
            "histogram": self.histogram,
            "examples": self.examples,
        }


def compute_column_statistics(lazy: pl.LazyFrame, column: str) -> ColumnStatistics:
    #считается только запрошенная колонка: остальные не читаются вовсе, и на широкой
    #таблице стоимость не зависит от числа колонок
    projected = lazy.select(pl.col(column))
    series = projected.collect().to_series()
    dtype = series.dtype
    row_count = series.len()

    return ColumnStatistics(
        name=column,
        dtype=str(dtype),
        row_count=row_count,
        null_count=series.null_count(),
        unique_count=series.n_unique(),
        numeric=_numeric_summary(series) if dtype.is_numeric() else None,
        top_values=None if dtype.is_numeric() else _top_values(series),
        histogram=_histogram(series) if dtype.is_numeric() else None,
        examples=[json_safe_value(value) for value in series.drop_nulls().head(EXAMPLES_LIMIT)],
    )


def _numeric_summary(series: pl.Series) -> dict[str, Any] | None:
    values = series.drop_nulls()

    if values.is_empty():
        #колонка из одних пропусков: статистики не существует, и подставлять нули нельзя —
        #ноль здесь неотличим от настоящего нуля в данных
        return None

    return {
        key: json_safe_value(value)
        for key, value in {
            "min": values.min(),
            "max": values.max(),
            "mean": values.mean(),
            "median": values.median(),
            "std": values.std(),
            "q1": values.quantile(0.25, interpolation="linear"),
            "q3": values.quantile(0.75, interpolation="linear"),
        }.items()
    }


def _top_values(series: pl.Series) -> list[dict[str, Any]]:
    values = series.drop_nulls()

    if values.is_empty():
        return []

    try:
        counts = values.value_counts(sort=True).head(TOP_VALUES_LIMIT)
    except pl.exceptions.InvalidOperationError:
        #вложенные значения (списки, структуры) не подсчитываются как категории:
        #для них частотное распределение не определено, и пустой список честнее выдумки
        return []

    total = values.len()
    value_column, count_column = counts.columns[0], counts.columns[1]

    return [
        {
            "value": json_safe_value(row[value_column]),
            "count": row[count_column],
            "ratio": round(row[count_column] / total, 6),
        }
        for row in counts.to_dicts()
    ]


def _histogram(series: pl.Series) -> dict[str, Any] | None:
    values = series.drop_nulls()

    if values.is_empty():
        return None

    #min и max числовой серии — числа, но статически это Any: приведение делается явно,
    #чтобы ошибка типа не пряталась за игнорированием
    raw_min, raw_max = values.min(), values.max()

    if not isinstance(raw_min, int | float) or not isinstance(raw_max, int | float):
        return None

    minimum, maximum = float(raw_min), float(raw_max)

    if minimum == maximum:
        #одно значение на всю колонку: интервалов не построить, но факт стоит показать
        return {"bins": [minimum], "counts": [values.len()], "constant": True}

    width = (maximum - minimum) / HISTOGRAM_BINS
    edges = [minimum + width * index for index in range(HISTOGRAM_BINS + 1)]
    counts = [0] * HISTOGRAM_BINS

    for value in values:
        index = min(int((float(value) - minimum) / width), HISTOGRAM_BINS - 1)
        counts[index] += 1

    return {"bins": [round(edge, 6) for edge in edges], "counts": counts, "constant": False}
