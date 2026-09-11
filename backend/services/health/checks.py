"""Проверки датасета.

Все колоночные показатели собираются **проходами по всем колонкам сразу**, а не
по одной: отдельный проход на колонку означал бы тысячу проходов на широком датасете.
Наружу выходят только агрегаты и ограниченная выборка примеров — ничего не
материализуется целиком.

Проходов ровно три:
1. батарея показателей (пропуски, кардинальность, типовые счётчики, квантили);
2. доминирующее значение для колонок-кандидатов в почти постоянные;
3. точный счёт затронутых строк — тем же предикатом, которым их потом покажут.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from backend.domain.dataset.json_values import json_safe_value
from backend.domain.health.models import (
    ActionKind,
    CheckCode,
    Evidence,
    Exactness,
    Finding,
    RowFilter,
    RowFilterKind,
    Scope,
    Severity,
    SuggestedAction,
    order_findings,
)
from backend.domain.health.thresholds import THRESHOLDS
from backend.domain.health.wording import rows as say_rows
from backend.domain.health.wording import values as say_values
from backend.services.health.predicates import count_affected

#за этим объёмом точный подсчёт различных значений перестаёт быть дешёвым, и вместо него
#берётся приближённый. Находки, выведенные из приближённой кардинальности, помечаются
#выборочными: показать приближение как точное число — значит соврать
APPROXIMATE_CARDINALITY_CELLS = 20_000_000
#доминирующее значение имеет смысл искать, только когда значений хотя бы два:
#у колонки с одним значением это уже отдельная находка
MIN_UNIQUE_FOR_DOMINANT = 2


@dataclass(frozen=True, slots=True)
class ColumnFacts:
    name: str
    dtype: pl.DataType
    nulls: int
    unique: int
    empty_strings: int
    surrounding_whitespace: int
    normalised_unique: int
    not_numeric: int
    nan_or_inf: int
    low: float | None
    high: float | None

    @property
    def filled(self) -> int:
        return self.total - self.nulls

    total: int = 0


def analyse(lazy: pl.LazyFrame, schema: pl.Schema, total_rows: int) -> list[Finding]:
    """Возвращает находки по датасету. Ничего не изменяет и ничего не пишет."""
    if total_rows == 0:
        #пустой датасет — не набор дефектов, а пустой датасет. Все доли здесь были бы
        #делением на ноль, а все выводы — бессмысленными
        return []

    approximate = total_rows * max(len(schema), 1) > APPROXIMATE_CARDINALITY_CELLS
    facts = _collect_facts(lazy, schema, total_rows, approximate=approximate)
    findings: list[Finding] = []

    findings.extend(_dataset_findings(lazy, schema, total_rows))

    for column in facts.values():
        findings.extend(
            _column_findings(lazy, schema, column, total_rows, approximate=approximate)
        )

    return order_findings(findings)


# ── сбор показателей ──────────────────────────────────────────────────────────


def _collect_facts(
    lazy: pl.LazyFrame, schema: pl.Schema, total_rows: int, *, approximate: bool
) -> dict[str, ColumnFacts]:
    names = list(schema.names())
    collected: dict[str, ColumnFacts] = {}

    #план дробится по группам колонок: тысяча колонок на десяток показателей — это
    #десять тысяч выражений в одном запросе, и такой план строить нельзя
    for start in range(0, len(names), THRESHOLDS.columns_per_batch):
        batch = names[start : start + THRESHOLDS.columns_per_batch]
        row = lazy.select(
            [
                expression
                for name in batch
                for expression in _facts_expressions(name, schema[name], approximate=approximate)
            ]
        ).collect()

        for name in batch:
            collected[name] = _facts_from_row(name, schema[name], row, total_rows)

    return collected


def _facts_expressions(name: str, dtype: pl.DataType, *, approximate: bool) -> list[pl.Expr]:
    column = pl.col(name)
    text = column.cast(pl.Utf8, strict=False)
    unique = (
        column.drop_nulls().approx_n_unique() if approximate else column.drop_nulls().n_unique()
    )

    expressions = [
        column.null_count().alias(f"nulls::{name}"),
        unique.alias(f"unique::{name}"),
    ]

    if dtype == pl.Utf8:
        expressions += [
            (text.str.len_chars() == 0).sum().alias(f"empty::{name}"),
            (text != text.str.strip_chars()).fill_null(value=False).sum().alias(f"ws::{name}"),
            text.str.strip_chars()
            .str.to_lowercase()
            .drop_nulls()
            .n_unique()
            .alias(f"norm::{name}"),
            #значение непусто, но числом не становится
            (text.is_not_null() & text.str.strip_chars().cast(pl.Float64, strict=False).is_null())
            .sum()
            .alias(f"notnum::{name}"),
        ]
    else:
        expressions += [
            pl.lit(0).alias(f"empty::{name}"),
            pl.lit(0).alias(f"ws::{name}"),
            pl.lit(0).alias(f"norm::{name}"),
            pl.lit(0).alias(f"notnum::{name}"),
        ]

    if dtype.is_float():
        finite = column.filter(column.is_finite())
        expressions += [
            (column.is_nan() | column.is_infinite())
            .fill_null(value=False)
            .sum()
            .alias(f"naninf::{name}"),
            #квантили считаются по конечным значениям: одна бесконечность делает
            #верхнюю границу бесконечной, и выбросов не находится никогда
            finite.quantile(0.25).alias(f"q1::{name}"),
            finite.quantile(0.75).alias(f"q3::{name}"),
        ]
    elif dtype.is_numeric():
        expressions += [
            pl.lit(0).alias(f"naninf::{name}"),
            column.quantile(0.25).alias(f"q1::{name}"),
            column.quantile(0.75).alias(f"q3::{name}"),
        ]
    else:
        expressions += [
            pl.lit(0).alias(f"naninf::{name}"),
            pl.lit(None, dtype=pl.Float64).alias(f"q1::{name}"),
            pl.lit(None, dtype=pl.Float64).alias(f"q3::{name}"),
        ]

    return expressions


def _facts_from_row(
    name: str, dtype: pl.DataType, row: pl.DataFrame, total_rows: int
) -> ColumnFacts:
    def value(prefix: str) -> Any:
        return row[f"{prefix}::{name}"][0]

    return ColumnFacts(
        name=name,
        dtype=dtype,
        nulls=int(value("nulls")),
        unique=int(value("unique")),
        empty_strings=int(value("empty") or 0),
        surrounding_whitespace=int(value("ws") or 0),
        normalised_unique=int(value("norm") or 0),
        not_numeric=int(value("notnum") or 0),
        nan_or_inf=int(value("naninf") or 0),
        low=value("q1"),
        high=value("q3"),
        total=total_rows,
    )


# ── находки уровня датасета ───────────────────────────────────────────────────


def _dataset_findings(lazy: pl.LazyFrame, schema: pl.Schema, total_rows: int) -> list[Finding]:
    names = tuple(schema.names())

    if not names:
        return []

    #колонки перечисляются явно, а не через pl.all(): к плану добавляется служебная
    #колонка с номером строки, и pl.all() захватил бы её — тогда каждая строка стала бы
    #уникальной и дубликатов не нашлось бы никогда. Проверено измерением
    duplicates = RowFilter(kind=RowFilterKind.DUPLICATED_BY, columns=names)
    affected = count_affected(lazy, duplicates, schema)

    if affected == 0:
        return []

    return [
        Finding(
            code=CheckCode.DUPLICATE_ROWS,
            severity=Severity.WARNING,
            scope=Scope.DATASET,
            columns=(),
            title="Полностью совпадающие строки",
            explanation=(
                f"{say_rows(affected)} повторяют другие строки по всем колонкам. Это бывает "
                "следствием повторной загрузки или объединения источников. Если каждая "
                "строка должна быть отдельным наблюдением, повторы исказят любой подсчёт."
            ),
            affected_rows=affected,
            affected_ratio=affected / total_rows,
            evidence=Evidence(counts={"duplicate_rows": affected, "total_rows": total_rows}),
            suggested_action=SuggestedAction(
                kind=ActionKind.DROP_DUPLICATE_ROWS,
                hint="Оставить по одной строке из каждой группы повторов.",
            ),
            row_filter=duplicates,
        )
    ]


# ── находки уровня колонки ────────────────────────────────────────────────────


def _column_findings(
    lazy: pl.LazyFrame,
    schema: pl.Schema,
    facts: ColumnFacts,
    total_rows: int,
    *,
    approximate: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    enough_rows = total_rows >= THRESHOLDS.min_rows_for_column_checks

    findings.extend(_missing_values(facts, total_rows))
    findings.extend(_nan_or_infinity(facts, total_rows))
    findings.extend(_whitespace(facts, total_rows))
    findings.extend(_empty_versus_null(facts, total_rows))
    findings.extend(_not_numeric(facts, total_rows))
    findings.extend(_case_variants(lazy, schema, facts, total_rows))

    if enough_rows:
        findings.extend(_shape_of_values(lazy, schema, facts, total_rows, approximate=approximate))
        findings.extend(_outliers(lazy, schema, facts, total_rows))

    return findings


def _missing_values(facts: ColumnFacts, total_rows: int) -> list[Finding]:
    if facts.nulls == 0:
        return []

    if facts.nulls == total_rows:
        return [
            Finding(
                code=CheckCode.ALL_NULL_COLUMN,
                severity=Severity.PROBLEM,
                scope=Scope.COLUMN,
                columns=(facts.name,),
                title=f"Колонка «{facts.name}» пуста целиком",
                explanation=(
                    "Ни одного значения во всех строках. Такая колонка не несёт сведений "
                    "и обычно означает, что поле не заполнялось или потерялось при выгрузке."
                ),
                affected_rows=total_rows,
                affected_ratio=1.0,
                evidence=Evidence(counts={"nulls": facts.nulls, "total_rows": total_rows}),
                suggested_action=SuggestedAction(
                    kind=ActionKind.DROP_COLUMN,
                    columns=(facts.name,),
                    hint="Удалить колонку, если она не нужна.",
                ),
                #провала в строки нет: затронуты все строки, и «показать их» —
                #это просто открыть датасет
                row_filter=None,
            )
        ]

    ratio = facts.nulls / total_rows

    return [
        Finding(
            code=CheckCode.MISSING_VALUES,
            severity=Severity.WARNING,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"Пропуски в колонке «{facts.name}»",
            explanation=(
                f"{say_rows(facts.nulls)} из {total_rows} не имеют значения "
                f"({_percent(ratio)}). Подсчёты по этой колонке будут считаться "
                "по меньшему числу строк, чем кажется."
            ),
            affected_rows=facts.nulls,
            affected_ratio=ratio,
            evidence=Evidence(counts={"nulls": facts.nulls, "total_rows": total_rows}),
            suggested_action=SuggestedAction(
                kind=ActionKind.FILL_MISSING,
                columns=(facts.name,),
                hint="Заполнить значением по умолчанию или отбросить эти строки.",
            ),
            row_filter=RowFilter(kind=RowFilterKind.IS_NULL, columns=(facts.name,)),
        )
    ]


def _nan_or_infinity(facts: ColumnFacts, total_rows: int) -> list[Finding]:
    if facts.nan_or_inf == 0:
        return []

    return [
        Finding(
            code=CheckCode.NAN_OR_INFINITY,
            severity=Severity.WARNING,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"NaN или бесконечность в «{facts.name}»",
            explanation=(
                f"{say_values(facts.nan_or_inf)} не являются числами в обычном смысле. "
                "Среднее, сумма и сортировка по такой колонке дают неожиданный результат, "
                "а часть форматов экспорта их вообще не принимает."
            ),
            affected_rows=facts.nan_or_inf,
            affected_ratio=facts.nan_or_inf / total_rows,
            evidence=Evidence(counts={"nan_or_inf": facts.nan_or_inf}),
            suggested_action=SuggestedAction(
                kind=ActionKind.REVIEW_MANUALLY,
                columns=(facts.name,),
                hint="Решить, чем заменить эти значения.",
            ),
            row_filter=RowFilter(kind=RowFilterKind.IS_NAN_OR_INF, columns=(facts.name,)),
        )
    ]


def _whitespace(facts: ColumnFacts, total_rows: int) -> list[Finding]:
    if facts.surrounding_whitespace == 0:
        return []

    return [
        Finding(
            code=CheckCode.WHITESPACE_ANOMALY,
            severity=Severity.WARNING,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"Пробелы по краям значений в «{facts.name}»",
            explanation=(
                f"{say_values(facts.surrounding_whitespace)} начинаются или заканчиваются "
                "пробелом. Глазами это незаметно, но «Москва» и «Москва » — разные "
                "значения при группировке, объединении и сравнении."
            ),
            affected_rows=facts.surrounding_whitespace,
            affected_ratio=facts.surrounding_whitespace / total_rows,
            evidence=Evidence(counts={"with_whitespace": facts.surrounding_whitespace}),
            suggested_action=SuggestedAction(
                kind=ActionKind.TRIM_WHITESPACE,
                columns=(facts.name,),
                hint="Убрать пробелы по краям значений.",
            ),
            row_filter=RowFilter(
                kind=RowFilterKind.HAS_SURROUNDING_WHITESPACE, columns=(facts.name,)
            ),
        )
    ]


def _empty_versus_null(facts: ColumnFacts, total_rows: int) -> list[Finding]:
    if facts.empty_strings == 0 or facts.nulls == 0:
        return []

    return [
        Finding(
            code=CheckCode.EMPTY_STRING_VS_NULL,
            severity=Severity.NOTICE,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"В «{facts.name}» есть и пустые строки, и пропуски",
            explanation=(
                f"{say_rows(facts.empty_strings)} пустых и {facts.nulls} пропусков в одной "
                "колонке. Обычно это два способа записать одно и то же, пришедшие "
                "из разных источников, — и считаться они будут по-разному."
            ),
            affected_rows=facts.empty_strings,
            affected_ratio=facts.empty_strings / total_rows,
            evidence=Evidence(counts={"empty_strings": facts.empty_strings, "nulls": facts.nulls}),
            suggested_action=SuggestedAction(
                kind=ActionKind.REVIEW_MANUALLY,
                columns=(facts.name,),
                hint="Привести к одному способу записи пустого значения.",
            ),
            row_filter=RowFilter(kind=RowFilterKind.IS_EMPTY_STRING, columns=(facts.name,)),
        )
    ]


def _not_numeric(facts: ColumnFacts, total_rows: int) -> list[Finding]:
    #смесь смыслов: колонка текстовая, но почти всё в ней — числа
    if facts.dtype != pl.Utf8 or facts.not_numeric == 0 or facts.filled == 0:
        return []

    minority = facts.not_numeric / facts.filled

    if minority > THRESHOLDS.mixed_types_max_minority_share or facts.not_numeric == facts.filled:
        #либо это обычная текстовая колонка, либо смесь пополам — и то и другое
        #не является опечаткой, о которой стоит говорить
        return []

    return [
        Finding(
            code=CheckCode.MIXED_SEMANTIC_TYPES,
            severity=Severity.WARNING,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"Нечисловые значения среди чисел в «{facts.name}»",
            explanation=(
                f"{say_values(facts.not_numeric)} из {facts.filled} не являются числами, "
                "а остальные являются. Обычно это пометки вроде «н/д» или «-» внутри "
                "числовой колонки: из-за них колонка читается как текст, и арифметика "
                "по ней недоступна."
            ),
            affected_rows=facts.not_numeric,
            affected_ratio=facts.not_numeric / total_rows,
            evidence=Evidence(counts={"not_numeric": facts.not_numeric, "filled": facts.filled}),
            suggested_action=SuggestedAction(
                kind=ActionKind.REVIEW_MANUALLY,
                columns=(facts.name,),
                hint="Заменить пометки на пропуски и привести колонку к числу.",
            ),
            row_filter=RowFilter(
                kind=RowFilterKind.NOT_PARSEABLE_AS_NUMBER, columns=(facts.name,)
            ),
        )
    ]


def _case_variants(
    lazy: pl.LazyFrame, schema: pl.Schema, facts: ColumnFacts, total_rows: int
) -> list[Finding]:
    if facts.dtype != pl.Utf8 or facts.normalised_unique >= facts.unique or facts.unique == 0:
        return []

    #группировка по нормализованному значению нужна только здесь и только для колонок,
    #где расхождение уже обнаружено батареей: считать её для всех подряд дорого
    grouped = (
        lazy.select(
            pl.col(facts.name).cast(pl.Utf8, strict=False).alias("__raw__"),
        )
        .drop_nulls()
        .with_columns(
            pl.col("__raw__").str.strip_chars().str.to_lowercase().alias("__norm__"),
        )
        .group_by("__norm__")
        .agg(
            pl.col("__raw__").n_unique().alias("__variants__"),
            pl.col("__raw__").unique().alias("__examples__"),
        )
        .filter(pl.col("__variants__") > 1)
        .sort("__variants__", descending=True)
        .head(THRESHOLDS.max_filter_values)
        .collect()
    )

    if grouped.height == 0:
        return []

    offending = tuple(grouped["__norm__"].to_list())
    row_filter = RowFilter(
        kind=RowFilterKind.NORMALISED_IN_VALUES, columns=(facts.name,), values=offending
    )
    affected = count_affected(lazy, row_filter, schema)
    examples = [
        _shorten(value)
        for group in grouped["__examples__"].to_list()[:3]
        for value in list(group)[:3]
    ]

    return [
        Finding(
            code=CheckCode.CASE_VARIANT_CATEGORIES,
            severity=Severity.WARNING,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"Значения, различающиеся только регистром или пробелами, в «{facts.name}»",
            explanation=(
                f"Записаны по-разному: {say_values(len(offending))}. «Москва» и «москва» "
                "считаются разными категориями при группировке, объединении и сравнении, "
                "хотя для человека это одно и то же."
            ),
            affected_rows=affected,
            affected_ratio=affected / total_rows,
            evidence=Evidence(
                values=tuple(examples[: THRESHOLDS.max_evidence_values]),
                counts={"groups": len(offending)},
            ),
            suggested_action=SuggestedAction(
                kind=ActionKind.NORMALISE_CASE,
                columns=(facts.name,),
                hint="Привести значения к одному написанию.",
            ),
            row_filter=row_filter,
        )
    ]


def _shape_of_values(
    lazy: pl.LazyFrame,
    schema: pl.Schema,
    facts: ColumnFacts,
    total_rows: int,
    *,
    approximate: bool,
) -> list[Finding]:
    """Постоянство, кардинальность, похожесть на ключ — всё из одной кардинальности."""
    if facts.filled == 0:
        return []

    exactness = Exactness.SAMPLED if approximate else Exactness.EXACT
    sampled_rows = total_rows if approximate else None

    if facts.unique == 1:
        return [
            Finding(
                code=CheckCode.CONSTANT_COLUMN,
                severity=Severity.NOTICE,
                scope=Scope.COLUMN,
                columns=(facts.name,),
                title=f"Колонка «{facts.name}» содержит одно значение",
                explanation=(
                    "Во всех заполненных строках одно и то же значение. Такая колонка "
                    "ничего не различает: она не влияет ни на группировку, ни на модель."
                ),
                affected_rows=None,
                affected_ratio=None,
                evidence=Evidence(counts={"unique": 1}),
                suggested_action=SuggestedAction(
                    kind=ActionKind.DROP_COLUMN,
                    columns=(facts.name,),
                    hint="Удалить колонку, если постоянство не является полезным фактом.",
                ),
                row_filter=None,
            )
        ]

    unique_share = facts.unique / facts.filled
    findings: list[Finding] = []

    if unique_share >= THRESHOLDS.identifier_min_unique_share and facts.nulls == 0:
        findings.append(
            Finding(
                code=CheckCode.POTENTIAL_IDENTIFIER,
                severity=Severity.NOTICE,
                scope=Scope.COLUMN,
                columns=(facts.name,),
                title=f"«{facts.name}» похожа на идентификатор",
                explanation=(
                    f"Почти все значения различны ({facts.unique} на {facts.filled} строк) "
                    "и пропусков нет. Это признак ключа. Само по себе это не дефект — "
                    "но по такой колонке бессмысленно группировать, и в модель её обычно "
                    "не берут."
                ),
                exactness=exactness,
                sampled_rows=sampled_rows,
                affected_rows=None,
                affected_ratio=None,
                evidence=Evidence(counts={"unique": facts.unique, "filled": facts.filled}),
                row_filter=None,
            )
        )

        if facts.unique < facts.filled:
            duplicates = RowFilter(kind=RowFilterKind.DUPLICATED_BY, columns=(facts.name,))
            affected = count_affected(lazy, duplicates, schema)
            findings.append(
                Finding(
                    code=CheckCode.DUPLICATE_KEYS,
                    severity=Severity.WARNING,
                    scope=Scope.COLUMN,
                    columns=(facts.name,),
                    title=f"Повторы в почти уникальной колонке «{facts.name}»",
                    explanation=(
                        f"{say_rows(affected)} повторяют значение, хотя остальные значения "
                        "уникальны. У колонки, похожей на ключ, повторы обычно означают "
                        "ошибку выгрузки или случайное объединение."
                    ),
                    affected_rows=affected,
                    affected_ratio=affected / total_rows,
                    evidence=Evidence(counts={"unique": facts.unique, "filled": facts.filled}),
                    suggested_action=SuggestedAction(
                        kind=ActionKind.REVIEW_MANUALLY,
                        columns=(facts.name,),
                        hint="Проверить, должны ли значения быть уникальными.",
                    ),
                    row_filter=duplicates,
                )
            )

        return findings

    if (
        unique_share >= THRESHOLDS.high_cardinality_share
        and facts.unique >= THRESHOLDS.high_cardinality_min_unique
    ):
        findings.append(
            Finding(
                code=CheckCode.HIGH_CARDINALITY,
                severity=Severity.NOTICE,
                scope=Scope.COLUMN,
                columns=(facts.name,),
                title=f"Много различных значений в «{facts.name}»",
                explanation=(
                    f"{facts.unique} различных значений на {facts.filled} заполненных строк. "
                    "Для адреса или комментария это нормально; для категории — признак того, "
                    "что значения записаны свободным текстом."
                ),
                exactness=exactness,
                sampled_rows=sampled_rows,
                affected_rows=None,
                affected_ratio=None,
                evidence=Evidence(counts={"unique": facts.unique, "filled": facts.filled}),
                row_filter=None,
            )
        )

    findings.extend(_near_constant(lazy, schema, facts, total_rows))

    return findings


def _near_constant(
    lazy: pl.LazyFrame, schema: pl.Schema, facts: ColumnFacts, total_rows: int
) -> list[Finding]:
    #доминирующее значение ищется только у колонок с небольшим числом различных значений:
    #у высококардинальной колонки доминирующего значения быть не может по определению
    if facts.unique < MIN_UNIQUE_FOR_DOMINANT or facts.unique > THRESHOLDS.max_filter_values:
        return []

    dominant = (
        lazy.select(pl.col(facts.name).drop_nulls().mode().first().alias("__mode__"))
        .collect()["__mode__"][0]
    )

    if dominant is None:
        return []

    row_filter = RowFilter(
        kind=RowFilterKind.NOT_EQUALS, columns=(facts.name,), values=(dominant,)
    )
    others = count_affected(lazy, row_filter, schema)
    share = (total_rows - others) / total_rows

    if share < THRESHOLDS.near_constant_share:
        return []

    return [
        Finding(
            code=CheckCode.NEAR_CONSTANT_COLUMN,
            severity=Severity.NOTICE,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"Почти одно значение в «{facts.name}»",
            explanation=(
                f"Значение «{_shorten(dominant)}» встречается в {_percent(share)} строк. "
                f"Остальные {say_rows(others)} — редкие исключения. Такая колонка почти "
                "ничего не различает, но исключения в ней могут быть самым интересным."
            ),
            affected_rows=others,
            affected_ratio=others / total_rows,
            evidence=Evidence(
                values=(_shorten(dominant),),
                counts={"dominant_rows": total_rows - others, "other_rows": others},
            ),
            suggested_action=SuggestedAction(
                kind=ActionKind.REVIEW_MANUALLY,
                columns=(facts.name,),
                hint="Посмотреть исключения: часто именно они и нужны.",
            ),
            #провал ведёт к исключениям, а не к доминирующему большинству
            row_filter=row_filter,
        )
    ]


def _outliers(
    lazy: pl.LazyFrame, schema: pl.Schema, facts: ColumnFacts, total_rows: int
) -> list[Finding]:
    if facts.low is None or facts.high is None or not facts.dtype.is_numeric():
        return []

    spread = float(facts.high) - float(facts.low)

    if spread <= 0:
        #межквартильный размах нулевой: половина значений совпадает, и «выброс»
        #в таком распределении не определён
        return []

    margin = spread * THRESHOLDS.outlier_iqr_multiplier
    row_filter = RowFilter(
        kind=RowFilterKind.OUT_OF_RANGE,
        columns=(facts.name,),
        low=float(facts.low) - margin,
        high=float(facts.high) + margin,
    )
    affected = count_affected(lazy, row_filter, schema)

    if affected == 0:
        return []

    return [
        Finding(
            code=CheckCode.OUTLIER_VALUES,
            severity=Severity.NOTICE,
            scope=Scope.COLUMN,
            columns=(facts.name,),
            title=f"Значения далеко от основной массы в «{facts.name}»",
            explanation=(
                f"{say_values(affected)} выходят за границы "
                f"[{_number(row_filter.low)}; {_number(row_filter.high)}], построенные по "
                "межквартильному размаху. Это не ошибка сама по себе: так выглядят "
                "и настоящие крупные сделки, и опечатка в разряде. Отличить одно "
                "от другого может только человек."
            ),
            affected_rows=affected,
            affected_ratio=affected / total_rows,
            evidence=Evidence(
                counts={
                    "low": row_filter.low,
                    "high": row_filter.high,
                    "outliers": affected,
                }
            ),
            suggested_action=SuggestedAction(
                kind=ActionKind.REVIEW_MANUALLY,
                columns=(facts.name,),
                hint="Посмотреть эти строки и решить, ошибка это или нет.",
            ),
            row_filter=row_filter,
        )
    ]


def _percent(ratio: float) -> str:
    #запятая, а не точка: рядом в интерфейсе «16,7%», и точка выглядит чужеродно
    return f"{ratio * 100:.1f}".replace(".", ",") + "%"


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:.4g}".replace(".", ",")


def _shorten(value: Any) -> Any:
    safe = json_safe_value(value)

    if isinstance(safe, str) and len(safe) > THRESHOLDS.max_evidence_value_length:
        return safe[: THRESHOLDS.max_evidence_value_length] + "…"

    return safe
