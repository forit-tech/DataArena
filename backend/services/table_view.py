"""Серверная выдача страниц таблицы.

Ни одна операция не материализует датасет целиком: сортировка, фильтрация и поиск
выражаются в плане `LazyFrame`, а `collect()` вызывается один раз — на срез страницы.
Клиент получает не более `MAX_PAGE_SIZE` строк независимо от того, о чём просил.

Значения приводятся к JSON-безопасному виду здесь, а не в роутере: `NaN`, `Inf`,
`Decimal` и временные типы не сериализуются стандартным JSON, и попытка отдать их
как есть даёт либо невалидный JSON, либо исключение в середине ответа.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import polars as pl

from backend.core.errors import ValidationError
from backend.domain.dataset.json_values import json_safe_value

DEFAULT_PAGE_SIZE = 100
#предел страницы: миллион строк в браузер не отправляется ни при каком значении параметра
MAX_PAGE_SIZE = 1000
#длина строки поиска и значения фильтра: длинный ввод не должен превращаться
#в дорогое сканирование по всем колонкам
MAX_FILTER_VALUE_LENGTH = 500
#подсчёт строк после фильтра дороже самой страницы, поэтому он ограничен сверху:
#точное число за этим пределом пользователю не нужно, а стоит полного прохода
MAX_EXACT_COUNT_ROWS = 5_000_000


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class FilterOperator(StrEnum):
    #только те операции, которые выражаются в плане Polars без исполнения чужого кода
    EQUALS = "eq"
    NOT_EQUALS = "ne"
    CONTAINS = "contains"
    GREATER = "gt"
    GREATER_OR_EQUAL = "gte"
    LESS = "lt"
    LESS_OR_EQUAL = "lte"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


@dataclass(frozen=True, slots=True)
class SortSpec:
    column: str
    direction: SortDirection = SortDirection.ASC


@dataclass(frozen=True, slots=True)
class FilterSpec:
    column: str
    operator: FilterOperator
    value: str | None = None


@dataclass(frozen=True, slots=True)
class PageRequest:
    offset: int = 0
    limit: int = DEFAULT_PAGE_SIZE
    sort: tuple[SortSpec, ...] = ()
    filters: tuple[FilterSpec, ...] = ()
    search: str | None = None


@dataclass(frozen=True, slots=True)
class PageResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    #номер строки в файле, по одному на строку страницы. Не номер на странице:
    #после сортировки «третья сверху» — каждый раз другая строка, а номер в файле
    #остаётся тем же. Измерено, что порядок чтения артефакта детерминирован
    row_ordinals: list[int]
    offset: int
    limit: int
    #число строк после фильтров; None означает «больше предела точного подсчёта»,
    #и интерфейс обязан показать это как «более N», а не как отсутствие данных
    total_rows: int | None
    total_is_exact: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_ordinals": self.row_ordinals,
            "offset": self.offset,
            "limit": self.limit,
            "total_rows": self.total_rows,
            "total_is_exact": self.total_is_exact,
        }


def parse_page_request(  # noqa: PLR0913, PLR0917 - параметры повторяют параметры HTTP-запроса
    schema_names: tuple[str, ...],
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    sort: list[str] | None = None,
    filters: list[str] | None = None,
    search: str | None = None,
) -> PageRequest:
    #эта функция превращает параметры запроса в проверенный план
    #проверка идёт до чтения данных: некорректный запрос обязан завершаться сразу
    if offset < 0:
        raise ValidationError("Смещение не может быть отрицательным.", details={"offset": offset})

    if limit < 1:
        raise ValidationError("Размер страницы должен быть положительным.", details={"limit": limit})

    #запрос страницы в миллион строк не отвергается, а урезается: это не ошибка пользователя,
    #а попытка получить больше, чем имеет смысл отправлять в браузер
    effective_limit = min(limit, MAX_PAGE_SIZE)

    return PageRequest(
        offset=offset,
        limit=effective_limit,
        sort=tuple(_parse_sort(item, schema_names) for item in (sort or [])),
        filters=tuple(_parse_filter(item, schema_names) for item in (filters or [])),
        search=_validated_search(search),
    )


def _validated_search(search: str | None) -> str | None:
    if search is None:
        return None

    trimmed = search.strip()

    if not trimmed:
        return None

    if len(trimmed) > MAX_FILTER_VALUE_LENGTH:
        raise ValidationError(
            f"Строка поиска длиннее {MAX_FILTER_VALUE_LENGTH} символов.",
            details={"length": len(trimmed)},
        )

    return trimmed


def _ensure_known_column(name: str, schema_names: tuple[str, ...]) -> str:
    #имя колонки сверяется со схемой датасета: только так произвольная строка из запроса
    #не доходит до выражения Polars
    if name not in schema_names:
        raise ValidationError(
            f"Колонка «{name[:100]}» отсутствует в датасете.",
            details={"column": name[:100]},
        )

    return name


def _parse_sort(raw: str, schema_names: tuple[str, ...]) -> SortSpec:
    column, _, direction = raw.rpartition(":")

    if not column:
        column, direction = raw, SortDirection.ASC.value

    _ensure_known_column(column, schema_names)

    if direction not in tuple(SortDirection):
        raise ValidationError(
            f"Неизвестное направление сортировки «{direction[:20]}». Допустимо: asc, desc.",
        )

    return SortSpec(column=column, direction=SortDirection(direction))


def _parse_filter(raw: str, schema_names: tuple[str, ...]) -> FilterSpec:
    column, _, rest = raw.partition(":")
    operator, _, value = rest.partition(":")

    _ensure_known_column(column, schema_names)

    if operator not in tuple(FilterOperator):
        raise ValidationError(
            f"Неизвестная операция фильтра «{operator[:20]}».",
            details={"allowed": [item.value for item in FilterOperator]},
        )

    parsed_operator = FilterOperator(operator)

    if parsed_operator in (FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL):
        return FilterSpec(column=column, operator=parsed_operator, value=None)

    if len(value) > MAX_FILTER_VALUE_LENGTH:
        raise ValidationError(
            f"Значение фильтра длиннее {MAX_FILTER_VALUE_LENGTH} символов.",
            details={"length": len(value)},
        )

    return FilterSpec(column=column, operator=parsed_operator, value=value)


def build_page(
    lazy: pl.LazyFrame,
    request: PageRequest,
    schema: pl.Schema,
    prefilter: pl.Expr | None = None,
) -> PageResult:
    """Строит план и материализует только одну страницу.

    `prefilter` — отбор строк находки диагностики. Он применяется **до** пользовательских
    фильтров и поиска, поэтому при их отсутствии в таблице оказывается ровно то множество
    строк, о котором говорит находка, а при их наличии — сужение внутри него. Второй
    таблицы для диагностики не существует: это та же выдача, с тем же постраничным
    просмотром и той же сортировкой.
    """
    #имя служебной колонки подбирается свободным: датасет с колонкой «__ordinal__»
    #иначе сломал бы и выдачу, и провал в строки
    ordinal = _free_column_name(schema)
    plan = lazy.with_row_index(ordinal)

    if prefilter is not None:
        plan = plan.filter(prefilter)

    for spec in request.filters:
        plan = plan.filter(_filter_expression(spec, schema))

    if request.search:
        plan = plan.filter(_search_expression(request.search, schema))

    if request.sort:
        #nulls_last=True одинаково для обоих направлений: иначе при смене направления
        #пропуски прыгают с одного конца таблицы на другой, и пользователь считает,
        #что данные изменились
        plan = plan.sort(
            by=[spec.column for spec in request.sort],
            descending=[spec.direction is SortDirection.DESC for spec in request.sort],
            nulls_last=True,
            maintain_order=True,
        )

    total_rows, total_is_exact = _count_rows(plan)
    page = plan.slice(request.offset, request.limit).collect()
    ordinals = [int(value) for value in page[ordinal].to_list()]
    page = page.drop(ordinal)

    return PageResult(
        columns=page.columns,
        rows=json_safe_rows(page),
        row_ordinals=ordinals,
        offset=request.offset,
        limit=request.limit,
        total_rows=total_rows,
        total_is_exact=total_is_exact,
    )


def _free_column_name(schema: pl.Schema) -> str:
    """Имя служебной колонки, которого нет в датасете.

    Фиксированное имя вроде «__ordinal__» однажды встретилось бы в данных пользователя,
    и тогда номер строки затёр бы его колонку — а полные дубликаты перестали бы
    находиться, потому что в набор колонок попал бы уникальный номер.
    """
    candidate = "__row__"
    suffix = 0

    while candidate in schema:
        suffix += 1
        candidate = f"__row_{suffix}__"

    return candidate


def _count_rows(plan: pl.LazyFrame) -> tuple[int | None, bool]:
    #подсчёт после фильтра требует прохода по данным, поэтому он ограничен сверху:
    #точное число за пределом стоит дорого и никому не нужно
    counted = plan.select(pl.len()).collect().item()

    if counted > MAX_EXACT_COUNT_ROWS:
        return MAX_EXACT_COUNT_ROWS, False

    return int(counted), True


def _filter_expression(spec: FilterSpec, schema: pl.Schema) -> pl.Expr:  # noqa: PLR0911 - диспетчер операций
    column = pl.col(spec.column)

    if spec.operator is FilterOperator.IS_NULL:
        return column.is_null()

    if spec.operator is FilterOperator.IS_NOT_NULL:
        return column.is_not_null()

    if spec.operator is FilterOperator.CONTAINS:
        #literal=True отключает трактовку ввода как регулярного выражения: иначе строка
        #вида «(a+)+b» превращается в катастрофический перебор на стороне сервера
        return (
            column.cast(pl.Utf8, strict=False)
            .str.contains(spec.value or "", literal=True)
            .fill_null(False)
        )

    typed_value = _coerce_value(spec.value, schema[spec.column])

    if spec.operator is FilterOperator.EQUALS:
        return column.eq(typed_value).fill_null(False)
    if spec.operator is FilterOperator.NOT_EQUALS:
        #строки с пропуском при «не равно» остаются: null не равен ничему, включая значение фильтра
        return column.ne(typed_value).fill_null(True)
    if spec.operator is FilterOperator.GREATER:
        return column.gt(typed_value).fill_null(False)
    if spec.operator is FilterOperator.GREATER_OR_EQUAL:
        return column.ge(typed_value).fill_null(False)
    if spec.operator is FilterOperator.LESS:
        return column.lt(typed_value).fill_null(False)

    return column.le(typed_value).fill_null(False)


def _coerce_value(raw: str | None, dtype: pl.DataType) -> Any:  # noqa: PLR0911 - диспетчер типов
    #значение фильтра приходит строкой: без приведения к типу колонки сравнение
    #числа со строкой либо падает, либо молча ничего не находит
    if raw is None:
        return None

    try:
        if dtype.is_integer():
            return int(raw)
        if dtype.is_float():
            return float(raw)
        if dtype == pl.Boolean:
            return raw.strip().lower() in {"true", "1", "yes", "да"}
        if dtype == pl.Date:
            return date.fromisoformat(raw)
        if isinstance(dtype, pl.Datetime):
            return datetime.fromisoformat(raw)
        if isinstance(dtype, pl.Decimal):
            return Decimal(raw)
    except (ValueError, ArithmeticError) as error:
        raise ValidationError(
            f"Значение «{raw[:50]}» не подходит для колонки типа {dtype}.",
            details={"value": raw[:50], "dtype": str(dtype)},
        ) from error

    return raw


def _search_expression(needle: str, schema: pl.Schema) -> pl.Expr:
    #поиск идёт по всем колонкам, приведённым к тексту; literal=True здесь тоже обязателен
    lowered = needle.lower()
    expression: pl.Expr | None = None

    for name in schema:
        match = (
            pl.col(name)
            .cast(pl.Utf8, strict=False)
            .str.to_lowercase()
            .str.contains(lowered, literal=True)
            .fill_null(False)
        )
        expression = match if expression is None else (expression | match)

    #датасет без колонок не должен давать «найдено всё»: пустой поиск не находит ничего
    return expression if expression is not None else pl.lit(value=False)


def json_safe_rows(frame: pl.DataFrame) -> list[dict[str, Any]]:
    return [
        {name: json_safe_value(value) for name, value in row.items()} for row in frame.to_dicts()
    ]
