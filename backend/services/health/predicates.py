"""Описание затронутых строк превращается в выражение — в одном месте.

Это самая важная функция этапа. Через неё идут **оба** пути: подсчёт затронутых строк
внутри находки и показ этих строк в таблице. Не две одинаковые реализации, а одна:
иначе однажды находка скажет «127 строк», а таблица покажет 119, и доверия к разделу
не останется. Инвариант закреплён тестом по каждой находке.
"""

from __future__ import annotations

import polars as pl

from backend.core.errors import AppError
from backend.domain.health.models import RowFilter, RowFilterKind


class FindingNotApplicableError(AppError):
    #предикат ссылается на колонку, которой в датасете больше нет: показать «те самые»
    #строки уже нельзя, и подставлять вместо них другие нельзя тем более
    status_code = 409
    code = "finding_not_applicable"


def row_filter_expression(spec: RowFilter, schema: pl.Schema) -> pl.Expr:
    """Возвращает выражение, отбирающее ровно те строки, о которых говорит находка."""
    for name in spec.columns:
        if name not in schema:
            raise FindingNotApplicableError(
                f"Колонки «{name}» больше нет в датасете.",
                details={"column": name},
            )

    builder = _BUILDERS.get(spec.kind)

    if builder is None:  # pragma: no cover - перечень закрыт, ветка на случай расширения
        raise FindingNotApplicableError(
            "Этот вид находки не умеет показывать строки.", details={"kind": spec.kind.value}
        )

    return builder(spec, schema)


def _is_null(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    return pl.col(spec.columns[0]).is_null()


def _is_empty_string(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    #пустая строка — это не пропуск: пользователь должен видеть разницу, а не догадываться
    return pl.col(spec.columns[0]).cast(pl.Utf8, strict=False).str.len_chars() == 0


def _is_nan_or_inf(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    column = pl.col(spec.columns[0])
    #fill_null: у пропуска нет ответа на вопрос «это NaN?», а фильтр обязан
    #дать однозначное «нет», иначе строка с пропуском попадёт в выборку случайно
    return (column.is_nan() | column.is_infinite()).fill_null(value=False)


def _not_equals(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    #ne_missing сравнивает и пропуски: без него строки с пропуском выпали бы из выборки,
    #хотя они тоже «не равны доминирующему значению»
    return pl.col(spec.columns[0]).ne_missing(spec.values[0])


def _in_values(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    return pl.col(spec.columns[0]).is_in(list(spec.values))


def _normalised_in_values(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    return _normalised(spec.columns[0]).is_in(list(spec.values))


def _has_surrounding_whitespace(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    column = pl.col(spec.columns[0]).cast(pl.Utf8, strict=False)
    return (column != column.str.strip_chars()).fill_null(value=False)


def _out_of_range(spec: RowFilter, schema: pl.Schema) -> pl.Expr:
    column = pl.col(spec.columns[0])
    outside = ((column < pl.lit(spec.low)) | (column > pl.lit(spec.high))).fill_null(value=False)

    if not schema[spec.columns[0]].is_float():
        return outside

    #NaN и бесконечность исключаются: о них уже говорит отдельная находка, и показывать
    #одни и те же строки дважды под разными заголовками — значит удваивать шум.
    #Проверено измерением: без этого выбросами объявлялись ровно те же строки, что NaN
    return outside & column.is_finite().fill_null(value=False)


def _duplicated_by(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    """Строки, чей набор значений встречается больше одного раза.

    Колонки перечислены явно и берутся из находки, а не из `pl.all()`. Это не мелочь:
    к плану добавляется служебная колонка с порядковым номером строки, и `pl.all()`
    захватил бы её — тогда каждая строка стала бы уникальной, и дубликатов не нашлось бы
    никогда.
    """
    if len(spec.columns) == 1:
        return pl.col(spec.columns[0]).is_duplicated()

    return pl.struct(list(spec.columns)).is_duplicated()


def _not_parseable_as_number(spec: RowFilter, _: pl.Schema) -> pl.Expr:
    column = pl.col(spec.columns[0]).cast(pl.Utf8, strict=False)
    #значение непусто, но числом не становится: именно это и есть «мусор среди чисел»
    return column.is_not_null() & column.str.strip_chars().cast(pl.Float64, strict=False).is_null()


def _normalised(name: str) -> pl.Expr:
    #нормализация для сравнения категорий: регистр и краевые пробелы не считаются
    #различием. «Москва», «москва» и «Москва » — одно и то же значение для человека
    return pl.col(name).cast(pl.Utf8, strict=False).str.strip_chars().str.to_lowercase()


_BUILDERS = {
    RowFilterKind.IS_NULL: _is_null,
    RowFilterKind.IS_EMPTY_STRING: _is_empty_string,
    RowFilterKind.IS_NAN_OR_INF: _is_nan_or_inf,
    RowFilterKind.NOT_EQUALS: _not_equals,
    RowFilterKind.IN_VALUES: _in_values,
    RowFilterKind.NORMALISED_IN_VALUES: _normalised_in_values,
    RowFilterKind.HAS_SURROUNDING_WHITESPACE: _has_surrounding_whitespace,
    RowFilterKind.OUT_OF_RANGE: _out_of_range,
    RowFilterKind.DUPLICATED_BY: _duplicated_by,
    RowFilterKind.NOT_PARSEABLE_AS_NUMBER: _not_parseable_as_number,
}


def count_affected(lazy: pl.LazyFrame, spec: RowFilter, schema: pl.Schema) -> int:
    """Считает затронутые строки тем же выражением, которым их потом покажут."""
    return int(lazy.filter(row_filter_expression(spec, schema)).select(pl.len()).collect().item())
