"""Анализ ключей и прогноз размера результата — по данным, а не по намерению.

Число строк результата здесь **считается**, а не прикидывается. Для каждого значения
ключа известно, сколько раз оно встречается слева и справа; сумма произведений по общим
значениям — это ровно столько строк, сколько вернёт соединение. Стоит это одной
группировки с каждой стороны, то есть дешевле самого соединения, и отвечает на главный
вопрос до выполнения: «датасет вырастет в двадцать раз, вы этого хотели?»
"""

from __future__ import annotations

import polars as pl

from backend.domain.joins.models import (
    Cardinality,
    JoinAnalysis,
    JoinKind,
    JoinSpec,
    KeyAnalysis,
    KeyPair,
    types_compatible,
)
from backend.services.joins.execute import key_type, normalized_keys, require_key_columns

#доля различных значений, выше которой ключ считается уникальным. Не 1.0: единственный
#повтор в справочнике на сто тысяч строк не превращает связь в N:M по существу
UNIQUE_ENOUGH = 0.999


def analyse(
    left: pl.LazyFrame,
    right: pl.LazyFrame,
    spec: JoinSpec,
) -> JoinAnalysis:
    """Считает всё, что нужно знать до соединения."""
    left_schema = left.collect_schema()
    right_schema = right.collect_schema()
    require_key_columns(spec, left_schema, right_schema)
    #ключи сравниваются в том же общем типе, что и при соединении: прогноз, посчитанный
    #в другом типе, расходился с результатом — и в обе стороны
    left, right = normalized_keys(left, right, spec)

    left_names = [key.left for key in spec.keys]
    right_names = [key.right for key in spec.keys]

    left_rows = int(left.select(pl.len()).collect().item())
    right_rows = int(right.select(pl.len()).collect().item())

    #группировка по ключу с каждой стороны: одна за проход, и после неё известно всё
    left_groups = (
        left.group_by(left_names).agg(pl.len().alias("__left__")).collect()
    ).rename({name: f"__k{index}__" for index, name in enumerate(left_names)})
    right_groups = (
        right.group_by(right_names).agg(pl.len().alias("__right__")).collect()
    ).rename({name: f"__k{index}__" for index, name in enumerate(right_names)})

    common = [f"__k{index}__" for index in range(len(spec.keys))]
    #несравнимые типы (число против текста) соединение отвергнет. Посчитать для них
    #пересечение можно только в тексте — и анализ отдельно предупреждает, что пары,
    #найденные так, соединение не примет
    incomparable = [
        name
        for name, key in zip(common, spec.keys, strict=True)
        if left_groups.schema[name] != right_groups.schema[name]
        and key_type(left_groups.schema[name], right_groups.schema[name]) is None
    ]
    left_groups = left_groups.with_columns([pl.col(name).cast(pl.Utf8) for name in incomparable])
    right_groups = right_groups.with_columns([pl.col(name).cast(pl.Utf8) for name in incomparable])

    matched = left_groups.join(right_groups, on=common, how="inner")
    overlap_values = matched.height
    inner_rows = (
        int(matched.select((pl.col("__left__") * pl.col("__right__")).sum()).item())
        if overlap_values
        else 0
    )
    matched_left_rows = (
        int(matched.select(pl.col("__left__").sum()).item()) if overlap_values else 0
    )
    matched_right_rows = (
        int(matched.select(pl.col("__right__").sum()).item()) if overlap_values else 0
    )

    estimated = _estimated_rows(
        spec.kind,
        inner_rows=inner_rows,
        left_rows=left_rows,
        right_rows=right_rows,
        matched_left_rows=matched_left_rows,
        matched_right_rows=matched_right_rows,
    )

    keys = tuple(
        _key_analysis(
            key,
            left,
            right,
            left_schema,
            right_schema,
            left_rows=left_rows,
            right_rows=right_rows,
        )
        for key in spec.keys
    )
    cardinality = _cardinality(keys)

    return JoinAnalysis(
        keys=keys,
        cardinality=cardinality,
        left_rows=left_rows,
        right_rows=right_rows,
        matched_left_rows=matched_left_rows,
        matched_right_rows=matched_right_rows,
        estimated_rows=estimated,
        warnings=_warnings(keys, cardinality, estimated, left_rows, right_rows),
    )


def _key_analysis(  # noqa: PLR0913 - показатели считаются по обеим сторонам сразу
    key: KeyPair,
    left: pl.LazyFrame,
    right: pl.LazyFrame,
    left_schema: pl.Schema,
    right_schema: pl.Schema,
    *,
    left_rows: int,
    right_rows: int,
) -> KeyAnalysis:
    stats = (
        left.select(
            pl.col(key.left).null_count().alias("ln"),
            pl.col(key.left).drop_nulls().n_unique().alias("lu"),
        )
        .collect()
        .row(0)
    )
    other = (
        right.select(
            pl.col(key.right).null_count().alias("rn"),
            pl.col(key.right).drop_nulls().n_unique().alias("ru"),
        )
        .collect()
        .row(0)
    )

    left_type = left_schema[key.left]
    right_type = right_schema[key.right]

    #сколько разных значений ключа есть с обеих сторон. Прежде здесь стоял ноль, и анализ
    #показывал «общих значений: 0» при тысячах совпавших строк
    left_values = left.select(pl.col(key.left).drop_nulls().unique().alias("__value__"))
    right_values = right.select(pl.col(key.right).drop_nulls().unique().alias("__value__"))
    if not _compatible(left_type, right_type) or left_type != right_type:
        left_values = left_values.with_columns(pl.col("__value__").cast(pl.Utf8))
        right_values = right_values.with_columns(pl.col("__value__").cast(pl.Utf8))
    overlap = int(left_values.join(right_values, on="__value__", how="inner").select(pl.len()).collect().item())

    return KeyAnalysis(
        pair=key,
        left_type=str(left_type),
        right_type=str(right_type),
        types_compatible=_compatible(left_type, right_type),
        left_nulls=int(stats[0]),
        right_nulls=int(other[0]),
        left_unique_ratio=(int(stats[1]) / left_rows) if left_rows else 0.0,
        right_unique_ratio=(int(other[1]) / right_rows) if right_rows else 0.0,
        overlap_values=overlap,
        left_values=int(stats[1]),
        right_values=int(other[1]),
    )


def _compatible(left: pl.DataType, right: pl.DataType) -> bool:
    #та же функция, что и у выполнения: две реализации однажды разошлись бы, и анализ
    #обещал бы совместимость там, где соединение отказывается
    return types_compatible(str(left), str(right))


def _estimated_rows(  # noqa: PLR0913 - все слагаемые нужны одновременно
    kind: JoinKind,
    *,
    inner_rows: int,
    left_rows: int,
    right_rows: int,
    matched_left_rows: int,
    matched_right_rows: int,
) -> int:
    unmatched_left = left_rows - matched_left_rows
    unmatched_right = right_rows - matched_right_rows

    if kind is JoinKind.INNER:
        return inner_rows

    if kind is JoinKind.LEFT:
        return inner_rows + unmatched_left

    if kind is JoinKind.RIGHT:
        return inner_rows + unmatched_right

    return inner_rows + unmatched_left + unmatched_right


def _cardinality(keys: tuple[KeyAnalysis, ...]) -> Cardinality:
    left_unique = all(key.left_unique_ratio >= UNIQUE_ENOUGH for key in keys)
    right_unique = all(key.right_unique_ratio >= UNIQUE_ENOUGH for key in keys)

    if left_unique and right_unique:
        return Cardinality.ONE_TO_ONE

    if left_unique:
        return Cardinality.ONE_TO_MANY

    if right_unique:
        return Cardinality.MANY_TO_ONE

    return Cardinality.MANY_TO_MANY


def _warnings(
    keys: tuple[KeyAnalysis, ...],
    cardinality: Cardinality,
    estimated: int,
    left_rows: int,
    right_rows: int,
) -> tuple[str, ...]:
    messages: list[str] = []

    for key in keys:
        if not key.types_compatible:
            messages.append(
                f"Типы ключа не совпадают: «{key.pair.left}» — {key.left_type}, "
                f"«{key.pair.right}» — {key.right_type}. Пары, скорее всего, не найдутся."
            )

        if key.left_nulls or key.right_nulls:
            messages.append(
                f"В ключе есть пропуски: слева {key.left_nulls}, справа {key.right_nulls}. "
                "Строки с пропуском в ключе пару не находят."
            )

    largest = max(left_rows, right_rows, 1)

    if estimated > largest * 2:
        messages.append(
            f"Строк станет {estimated} — это больше, чем в любом из датасетов "
            f"({left_rows} и {right_rows}). Связь {cardinality.value} умножает строки."
        )

    if estimated == 0:
        messages.append("Ни одна пара не найдена: результат будет пустым.")

    return tuple(messages)
