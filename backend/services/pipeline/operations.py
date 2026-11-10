"""Операции конвейера: проверка, применение, описание, оценка последствий.

Каждая операция — это четыре ответа на один шаг:

* **проверить** — можно ли выполнить её на этой схеме, и если нет, то почему понятным
  человеку текстом;
* **применить** — превратить один ленивый план в другой, не трогая исходный файл;
* **описать** — сказать словами, что шаг делает, чтобы список шагов читался, а не
  расшифровывался;
* **оценить последствия** — сколько строк или колонок исчезнет, если шаг разрушающий.

Перечень операций закрыт. Шаг — не произвольное выражение пользователя, а операция из
списка: иначе конвейер перестаёт быть воспроизводимым описанием и превращается
в исполнение чужого кода прямо в процессе сервера.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import polars as pl

from backend.domain.dataset.models import LogicalType
from backend.domain.lineage.effects import (
    ColumnCreate,
    ColumnDrop,
    ColumnEffect,
    ColumnMutation,
    ColumnRename,
    DatasetOnlyEffect,
)
from backend.domain.pipeline.models import PipelineError, Step, StepKind
from backend.services.pipeline.formula import compile_formula

#во что разрешено приводить колонку. Список намеренно короче, чем всё, что умеет Polars:
#приведение к редкому типу без понятного пользователю смысла — источник пропусков,
#а не польза
CAST_TARGETS: dict[str, pl.DataType] = {
    LogicalType.INTEGER.value: pl.Int64(),
    LogicalType.FLOAT.value: pl.Float64(),
    LogicalType.STRING.value: pl.Utf8(),
    LogicalType.BOOLEAN.value: pl.Boolean(),
    LogicalType.DATE.value: pl.Date(),
    LogicalType.DATETIME.value: pl.Datetime("us"),
}

#значения, которые в выгрузках означают «нет значения», но записаны как текст.
#Список — умолчание, пользователь задаёт свой
DEFAULT_NULL_TOKENS = ("", "-", "—", "н/д", "нд", "n/a", "na", "null", "none", "нет данных")

CASE_MODES = ("lower", "upper", "title")

#записи дат, которые разбираются без явного формата. Прежде формат угадывал движок
#по первому значению: если угадать не удавалось, падал весь запрос, а значения в другой
#записи молча становились пропусками. Список проверяется по порядку для каждого значения
#отдельно, так что смешанные записи в одной колонке разбираются. День идёт раньше месяца:
#«01.02.2024» — это первое февраля
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y.%m.%d")
DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%.f",
    "%Y-%m-%d %H:%M:%S%.f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)

#слова, которые при приведении к логическому типу означают «да» и «нет»
TRUE_TOKENS = frozenset({"1", "true", "t", "yes", "y", "да", "истина"})
FALSE_TOKENS = frozenset({"0", "false", "f", "no", "n", "нет", "ложь"})

#сравнения, для которых важен порядок: для них не-число (NaN) не проходит условие
ORDERING = frozenset({"gt", "gte", "lt", "lte"})

COMPARISONS: dict[str, Callable[[pl.Expr, Any], pl.Expr]] = {
    "eq": lambda column, value: column.eq_missing(value),
    "ne": lambda column, value: column.ne_missing(value),
    "gt": lambda column, value: column > value,
    "gte": lambda column, value: column >= value,
    "lt": lambda column, value: column < value,
    "lte": lambda column, value: column <= value,
}


@dataclass(frozen=True, slots=True)
class Operation:
    kind: StepKind
    title: str
    validate: Callable[[dict[str, Any], pl.Schema], None]
    apply: Callable[[pl.LazyFrame, dict[str, Any], pl.Schema], pl.LazyFrame]
    describe: Callable[[dict[str, Any]], str]
    #колонки, которые шаг читает: по ним проверяется, не сломает ли перестановка шагов
    reads: Callable[[dict[str, Any]], tuple[str, ...]]
    #что шаг делает с колонками для их происхождения. Из `reads` это не выводится:
    #сортировка читает колонку, но не меняет в ней ни одного значения
    effect: Callable[[dict[str, Any]], ColumnEffect]


# ── вспомогательное ───────────────────────────────────────────────────────────


def _text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)

    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"Не задан параметр «{key}».", details={"parameter": key})

    return value


def _columns(params: dict[str, Any], key: str = "columns") -> tuple[str, ...]:
    value = params.get(key)

    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise PipelineError(f"Не заданы колонки в «{key}».", details={"parameter": key})

    return tuple(value)


def _require_present(columns: tuple[str, ...], schema: pl.Schema) -> None:
    missing = [name for name in columns if name not in schema]

    if missing:
        #ровно та ошибка, которая возникает при перестановке шагов: шаг ссылается
        #на колонку, которой на этом месте ещё (или уже) нет
        raise PipelineError(
            f"Нет колонок: {', '.join(missing)}.",
            details={"columns": missing},
        )


def _require_absent(name: str, schema: pl.Schema) -> None:
    if name in schema:
        raise PipelineError(
            f"Колонка «{name}» уже есть — имя должно быть свободным.",
            details={"column": name},
        )


def _parse_literal(value: str, dtype: pl.DataType) -> Any:  # noqa: PLR0912 - диспетчер типов
    """Значение из формы в значение типа колонки — или понятный отказ.

    Прежде значение, которое не разбиралось, молча становилось null: «сумма > abc»
    превращалось в «сумма > null» и удаляло все строки, «= abc» находило строки
    с пропуском, а «заполнить пропуски значением abc» не делало ничего. Теперь такое
    значение — ошибка шага с объяснением, до сохранения.
    """
    raw = value.strip()

    def refuse() -> PipelineError:
        return PipelineError(
            f"Значение «{value}» не подходит для колонки типа {dtype}.",
            details={"value": value, "dtype": str(dtype)},
        )

    if dtype == pl.Utf8 or isinstance(dtype, pl.Categorical | pl.Enum):
        return value

    if dtype == pl.Boolean:
        lowered = raw.lower()

        if lowered in TRUE_TOKENS:
            return True

        if lowered in FALSE_TOKENS:
            return False

        raise refuse()

    text = pl.Series([raw], dtype=pl.Utf8)

    try:
        if dtype.is_numeric():
            #десятичная запятая — обычная запись в выгрузках; пробел — разделитель разрядов
            number = raw.replace(" ", "").replace("\u00a0", "")
            number = number.replace(",", ".") if "," in number and "." not in number else number
            return pl.Series([number], dtype=pl.Utf8).cast(dtype, strict=True).item()

        #даты из формы разбираются теми же записями, что и колонки в шагах разбора
        if dtype == pl.Date:
            parsed = text.to_frame("v").select(_parsed_date(pl.col("v"))).to_series()
        elif isinstance(dtype, pl.Datetime):
            parsed = text.to_frame("v").select(
                _parsed_datetime(pl.col("v"), time_unit=dtype.time_unit)
            ).to_series()

            if dtype.time_zone:
                #значение из формы понимается во времени самой колонки, а не в UTC
                parsed = parsed.dt.replace_time_zone(dtype.time_zone)
        else:
            parsed = None

        if parsed is not None:
            if parsed.null_count():
                raise refuse()

            return parsed.item()

        if dtype == pl.Time:
            return text.str.to_time(strict=True).item()
    except PipelineError:
        raise
    except Exception as error:  # любой отказ разбора — это неподходящее значение
        raise refuse() from error

    raise PipelineError(
        f"Сравнивать значения колонки типа {dtype} с текстом из формы нельзя.",
        details={"dtype": str(dtype)},
    )


def _typed_literal(value: str, dtype: pl.DataType) -> pl.Expr:
    """Значение из формы — литерал типа колонки. Неразбираемое значение — отказ, а не null."""
    parsed = _parse_literal(value, dtype)

    if isinstance(dtype, pl.Categorical | pl.Enum):
        #категории сравниваются как текст: порядок категорий — внутренний, а не алфавитный
        return pl.lit(parsed, dtype=pl.Utf8)

    return pl.lit(parsed, dtype=dtype)


def _scalar(dtype: pl.DataType) -> bool:
    #вложенные и двоичные значения не сравниваются со строкой из формы
    return not isinstance(dtype, pl.List | pl.Array | pl.Struct | pl.Object | pl.Binary | pl.Null)


def _predicate(params: dict[str, Any], schema: pl.Schema) -> pl.Expr:
    """Условие отбора строк — общее для «оставить» и «удалить».

    Условие возвращает null там, где его нельзя проверить (пропуск в колонке); что делать
    с такими строками, решает вызывающий: «оставить» их не оставляет, «удалить» — не удаляет.
    """
    name = _text(params, "column")
    _require_present((name,), schema)
    operator = str(params.get("operator") or "eq")
    dtype = schema[name]
    column = pl.col(name)

    if operator == "is_null":
        return column.is_null()

    if operator == "is_not_null":
        return column.is_not_null()

    if not _scalar(dtype):
        raise PipelineError(
            f"Колонка «{name}» типа {dtype} не сравнивается со значением.",
            details={"column": name, "dtype": str(dtype)},
        )

    if operator == "contains":
        return column.cast(pl.Utf8, strict=False).str.contains(_text(params, "value"), literal=True)

    comparison = COMPARISONS.get(operator)

    if comparison is None:
        raise PipelineError(
            f"Неизвестное условие: {operator}.", details={"operator": operator}
        )

    if isinstance(dtype, pl.Categorical | pl.Enum):
        column = column.cast(pl.Utf8)

    condition = comparison(column, _typed_literal(_text(params, "value"), dtype))

    if operator in ORDERING and dtype.is_float():
        #NaN в Polars больше любого числа: «сумма > 100» отбирало бы и NaN. Не-число
        #не больше и не меньше ничего — условие для него ложно
        condition = condition & column.is_not_nan()

    return condition


def _text_columns(columns: tuple[str, ...], schema: pl.Schema) -> list[str]:
    #сначала наличие: без этой проверки отказ приходил бы из Polars и не говорил,
    #какой именно колонки не хватает — а это ровно тот случай, когда шаг переставили
    #выше того, который эту колонку создаёт
    _require_present(columns, schema)

    #операции очистки текста применимы к текстовым колонкам; для числовой они означали бы
    #приведение к строке, то есть смену типа исподтишка. Enum не входит: очищенное значение
    #может оказаться вне перечня, и оно стало бы пропуском
    wrong = [name for name in columns if not _textual(schema[name])]

    if wrong:
        raise PipelineError(
            f"Это не текстовые колонки: {', '.join(wrong)}.",
            details={"columns": wrong},
        )

    return list(columns)


def _textual(dtype: pl.DataType) -> bool:
    return dtype == pl.Utf8 or isinstance(dtype, pl.Categorical)


def _as_before(expression: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    """Результат текстовой операции — в прежнем типе колонки.

    Категориальная колонка после обрезки пробелов оставалась бы текстовой: тип менялся
    бы исподтишка, хотя операция обещает не трогать тип.
    """
    return expression if dtype == pl.Utf8 else expression.cast(dtype)


# ── колонки ───────────────────────────────────────────────────────────────────


def _drop_columns(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    columns = _columns(params)

    if len(columns) >= len(schema):
        raise PipelineError(
            "Нельзя удалить все колонки: датасет без колонок не датасет.",
            details={"columns": list(columns)},
        )

    return lazy.drop(columns)


def _validate_drop_columns(params: dict[str, Any], schema: pl.Schema) -> None:
    columns = _columns(params)
    _require_present(columns, schema)

    if len(columns) >= len(schema):
        raise PipelineError(
            "Нельзя удалить все колонки: датасет без колонок не датасет.",
            details={"columns": list(columns)},
        )


def _validate_rename(params: dict[str, Any], schema: pl.Schema) -> None:
    _require_present((_text(params, "column"),), schema)
    _require_absent(_text(params, "to"), schema)


def _validate_reorder(params: dict[str, Any], schema: pl.Schema) -> None:
    order = _columns(params, "order")
    _require_present(order, schema)

    if len(set(order)) != len(order):
        #повтор в порядке означал бы две колонки с одним именем; без этой проверки
        #отказ приходил бы из Polars и не объяснял, что именно не так
        repeated = sorted({name for name in order if order.count(name) > 1})
        raise PipelineError(
            f"Колонка указана в порядке дважды: {', '.join(repeated)}.",
            details={"columns": repeated},
        )

    if set(order) != set(schema.names()):
        #частичный порядок означал бы, что часть колонок молча уедет в конец
        raise PipelineError(
            "Порядок должен перечислять все колонки датасета.",
            details={"missing": sorted(set(schema.names()) - set(order))},
        )


def _validate_duplicate(params: dict[str, Any], schema: pl.Schema) -> None:
    _require_present((_text(params, "column"),), schema)
    _require_absent(_text(params, "to"), schema)


def _validate_add_column(params: dict[str, Any], schema: pl.Schema) -> None:
    name = _text(params, "name")
    _require_absent(name, schema)
    expression, _ = compile_formula(str(params.get("expression") or ""), schema)
    #типы проверяются до сохранения: «текст умножить на дату» отказывается здесь,
    #а не пятисоткой при показе результата
    #одна строка из пропусков нужных типов: схема пустого кадра пропускала «текст × дата»,
    #а вычисление на строке отказывает так же, как отказало бы на данных
    probe = pl.DataFrame({column: pl.Series([None], dtype=dtype) for column, dtype in schema.items()})
    try:
        probe.lazy().with_columns(expression.alias(name)).collect()
    except pl.exceptions.PolarsError as error:
        raise PipelineError(
            f"Формула не вычисляется для этих колонок: {str(error).splitlines()[0][:200]}",
            details={"name": name},
        ) from error


def _add_column(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    expression, _ = compile_formula(str(params.get("expression") or ""), schema)
    return lazy.with_columns(expression.alias(str(params["name"])))


def _formula_reads(params: dict[str, Any]) -> tuple[str, ...]:
    #колонки, на которые ссылается формула, без схемы: имена в скобках и голые имена.
    #Точный список даёт компиляция; здесь — для проверки перестановки шагов
    import ast
    import re

    text = re.sub(r"\[([^\[\]]+)\]", lambda m: f"col({m.group(1).strip()!r})", str(params.get("expression") or ""))
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return ()
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in ("True", "False", "None") and not _is_function(tree, node):
            names.append(node.id)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "col"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            names.append(str(node.args[0].value))
    return tuple(dict.fromkeys(names))


def _is_function(tree: Any, name_node: Any) -> bool:
    import ast

    return any(isinstance(node, ast.Call) and node.func is name_node for node in ast.walk(tree))


def _validate_cast(params: dict[str, Any], schema: pl.Schema) -> None:
    name = _text(params, "column")
    _require_present((name,), schema)
    target = _text(params, "to")

    if target not in CAST_TARGETS:
        raise PipelineError(
            f"Нельзя привести к типу «{target}».",
            details={"allowed": sorted(CAST_TARGETS)},
        )

    source = schema[name]
    temporal = source.is_temporal()
    numeric_target = target in {LogicalType.INTEGER.value, LogicalType.FLOAT.value}
    unsupported = (
        not _scalar(source)
        #дата в число — это число дней от 1970 года, а не ответ на вопрос пользователя
        or (temporal and (numeric_target or target == LogicalType.BOOLEAN.value))
        or (
            (source.is_numeric() or source == pl.Boolean)
            and target in {LogicalType.DATE.value, LogicalType.DATETIME.value}
        )
    )

    if unsupported:
        raise PipelineError(
            f"Колонку «{name}» типа {source} нельзя привести к «{target}»: "
            "значения такого приведения не имели бы понятного смысла.",
            details={"column": name, "from": str(source), "to": target},
        )


def _cast(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    name = _text(params, "column")
    target = CAST_TARGETS[_text(params, "to")]
    source = schema[name]
    column = pl.col(name)

    if isinstance(source, pl.Categorical | pl.Enum):
        #категории приводятся через их текст: напрямую Polars не умеет их в числа и даты
        column = column.cast(pl.Utf8)
        source = pl.Utf8()

    if source == pl.Utf8 and target == pl.Boolean:
        #текст в логическое — по словам, а не приведением: Polars такого приведения
        #не знает и отказывал на первой же строке данных, то есть пятисоткой
        lowered = column.str.strip_chars().str.to_lowercase()
        expression = (
            pl.when(lowered.is_in(list(TRUE_TOKENS)))
            .then(pl.lit(value=True))
            .when(lowered.is_in(list(FALSE_TOKENS)))
            .then(pl.lit(value=False))
            .otherwise(pl.lit(None, dtype=pl.Boolean))
        )
    elif source == pl.Utf8 and isinstance(target, pl.Date | pl.Datetime):
        #текст в дату приводится разбором, а не приведением типа: cast здесь отказывается
        expression = (
            _parsed_date(column)
            if isinstance(target, pl.Date)
            else _parsed_datetime(column, time_unit=target.time_unit)
        )
    else:
        #strict=False: значение, которое не приводится, становится пропуском. Это видно
        #в оценке последствий до применения, и это честнее отказа на всём датасете
        expression = column.cast(target, strict=False)

    return lazy.with_columns(expression.alias(name))


# ── строки ────────────────────────────────────────────────────────────────────


def _validate_predicate(params: dict[str, Any], schema: pl.Schema) -> None:
    _predicate(params, schema)


def _validate_sort(params: dict[str, Any], schema: pl.Schema) -> None:
    _require_present((_text(params, "column"),), schema)

    #строка «false» истинна для bool(): сортировка шла бы по убыванию вопреки параметру
    if not isinstance(params.get("descending", False), bool):
        raise PipelineError(
            "Параметр «descending» должен быть логическим.", details={"parameter": "descending"}
        )


def _sort(lazy: pl.LazyFrame, params: dict[str, Any], _: pl.Schema) -> pl.LazyFrame:
    return lazy.sort(
        _text(params, "column"),
        descending=bool(params.get("descending")),
        nulls_last=True,
        maintain_order=True,
    )


# ── значения ──────────────────────────────────────────────────────────────────


def _replace(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    name = _text(params, "column")
    dtype = schema[name]
    source = str(params.get("from", ""))
    target = str(params.get("to", ""))
    mode = str(params.get("mode") or "exact")

    if dtype.is_numeric():
        #число сравнивается с числом: через текст «1» не находило 1.0
        replaced = (
            pl.when(pl.col(name) == _typed_literal(source, dtype))
            .then(_typed_literal(target, dtype))
            .otherwise(pl.col(name))
        )

        return lazy.with_columns(replaced.alias(name))

    column = pl.col(name).cast(pl.Utf8)

    if mode == "substring":
        replaced = column.str.replace_all(source, target, literal=True)
    else:
        replaced = pl.when(column == source).then(pl.lit(target)).otherwise(column)

    return lazy.with_columns(_as_before(replaced, dtype).alias(name))


def _validate_replace(params: dict[str, Any], schema: pl.Schema) -> None:
    name = _text(params, "column")
    _require_present((name,), schema)
    dtype = schema[name]
    mode = str(params.get("mode") or "exact")

    if mode not in {"exact", "substring"}:
        raise PipelineError(f"Неизвестный режим замены: {mode}.", details={"mode": mode})

    #замена работает с текстом и числами. Прежде значения любых типов проходили через
    #текст: логическая колонка давала пятисотку, а дата с часовым поясом — пропуски
    #во всей колонке
    if not (_textual(dtype) or dtype.is_numeric()):
        raise PipelineError(
            f"Замена значений в колонке «{name}» типа {dtype} не поддерживается. "
            "Приведите её к тексту, если нужно заменить запись значения.",
            details={"column": name, "dtype": str(dtype)},
        )

    if mode == "substring" and not _textual(dtype):
        raise PipelineError(
            "Замена части значения возможна только в текстовой колонке.",
            details={"column": name},
        )

    if mode == "substring" and not str(params.get("from", "")):
        #пустая подстрока встречается между любыми двумя символами: «abc» стало бы «XaXbXcX»
        raise PipelineError("Не задано, что заменять.", details={"parameter": "from"})

    if dtype.is_numeric():
        _parse_literal(str(params.get("from", "")), dtype)
        _parse_literal(str(params.get("to", "")), dtype)


def _fill_missing(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    name = _text(params, "column")
    dtype = schema[name]

    if isinstance(dtype, pl.Categorical | pl.Enum):
        filled = pl.col(name).cast(pl.Utf8).fill_null(pl.lit(_text(params, "value"))).cast(dtype)
    else:
        filled = pl.col(name).fill_null(_typed_literal(_text(params, "value"), dtype))

    return lazy.with_columns(filled.alias(name))


def _validate_fill(params: dict[str, Any], schema: pl.Schema) -> None:
    name = _text(params, "column")
    _require_present((name,), schema)
    dtype = schema[name]

    if not _scalar(dtype):
        raise PipelineError(
            f"Пропуски в колонке «{name}» типа {dtype} заполнить значением нельзя.",
            details={"column": name, "dtype": str(dtype)},
        )

    value = _parse_literal(_text(params, "value"), dtype)

    if isinstance(dtype, pl.Enum) and value not in dtype.categories.to_list():
        raise PipelineError(
            f"Значения «{value}» нет среди допустимых значений колонки «{name}».",
            details={"column": name},
        )


def _normalize_nulls(
    lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema
) -> pl.LazyFrame:
    columns = _text_columns(_columns(params), schema)
    tokens = params.get("tokens") or list(DEFAULT_NULL_TOKENS)
    lowered = [str(token).strip().lower() for token in tokens]

    return lazy.with_columns(
        [
            pl.when(
                pl.col(name).cast(pl.Utf8, strict=False).str.strip_chars().str.to_lowercase()
                .is_in(lowered)
            )
            .then(None)
            .otherwise(pl.col(name))
            .alias(name)
            for name in columns
        ]
    )


# ── очистка ───────────────────────────────────────────────────────────────────


def _deduplicate(lazy: pl.LazyFrame, params: dict[str, Any], _: pl.Schema) -> pl.LazyFrame:
    subset = params.get("columns") or None
    #maintain_order=True: без него порядок строк после шага зависит от плана, и результат
    #перестаёт быть воспроизводимым — а воспроизводимость здесь и есть смысл конвейера
    return lazy.unique(subset=list(subset) if subset else None, keep="first", maintain_order=True)


def _validate_deduplicate(params: dict[str, Any], schema: pl.Schema) -> None:
    columns = params.get("columns")

    if columns:
        _require_present(tuple(columns), schema)


def _trim(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    columns = _text_columns(_columns(params), schema)

    return lazy.with_columns(
        [
            _as_before(pl.col(name).cast(pl.Utf8).str.strip_chars(), schema[name]).alias(name)
            for name in columns
        ]
    )


def _change_case(lazy: pl.LazyFrame, params: dict[str, Any], schema: pl.Schema) -> pl.LazyFrame:
    columns = _text_columns(_columns(params), schema)
    mode = str(params.get("case") or "lower")
    text = {
        "lower": lambda column: column.str.to_lowercase(),
        "upper": lambda column: column.str.to_uppercase(),
        "title": lambda column: column.str.to_titlecase(),
    }[mode]

    return lazy.with_columns(
        [_as_before(text(pl.col(name).cast(pl.Utf8)), schema[name]).alias(name) for name in columns]
    )


def _validate_case(params: dict[str, Any], schema: pl.Schema) -> None:
    _text_columns(_columns(params), schema)
    mode = str(params.get("case") or "lower")

    if mode not in CASE_MODES:
        raise PipelineError(f"Неизвестный регистр: {mode}.", details={"allowed": list(CASE_MODES)})


def _parse_number(lazy: pl.LazyFrame, params: dict[str, Any], _: pl.Schema) -> pl.LazyFrame:
    name = _text(params, "column")
    #пробелы-разделители разрядов и запятая как десятичный знак — обычная запись в выгрузках.
    #Если в записи есть и точка, и запятая, десятичный знак — тот, что стоит последним,
    #а другой разделяет разряды: «1.234,56» и «1,234.56» — одно число, а не пропуск.
    #Одна запятая без точки по-прежнему десятичная: «1,5» — это полтора
    text = (
        pl.col(name)
        .cast(pl.Utf8, strict=False)
        .str.strip_chars()
        .str.replace_all(r"[\s\u00a0]", "")
    )
    dot_groups = text.str.contains(r"^[+-]?\d{1,3}(\.\d{3})+,\d+$") | text.str.contains(
        r"^[+-]?\d{1,3}(\.\d{3}){2,}$"
    )
    comma_groups = text.str.contains(r"^[+-]?\d{1,3}(,\d{3})+\.\d+$") | text.str.contains(
        r"^[+-]?\d{1,3}(,\d{3}){2,}$"
    )
    cleaned = (
        pl.when(dot_groups)
        .then(text.str.replace_all(".", "", literal=True).str.replace_all(",", ".", literal=True))
        .when(comma_groups)
        .then(text.str.replace_all(",", "", literal=True))
        .otherwise(text.str.replace_all(",", ".", literal=True))
    )

    return lazy.with_columns(cleaned.cast(pl.Float64, strict=False).alias(name))


def _validate_parse_number(params: dict[str, Any], schema: pl.Schema) -> None:
    name = _text(params, "column")
    _require_present((name,), schema)

    #разбирать как число можно запись числа: текст или уже число. Дата, прочитанная
    #как текст, дала бы пропуски во всей колонке без единого слова об этом
    if not (_textual(schema[name]) or schema[name].is_numeric()):
        raise PipelineError(
            f"Колонку «{name}» типа {schema[name]} нельзя разобрать как число.",
            details={"column": name, "dtype": str(schema[name])},
        )


def _parse_datetime(lazy: pl.LazyFrame, params: dict[str, Any], _: pl.Schema) -> pl.LazyFrame:
    name = _text(params, "column")
    pattern = params.get("format")
    text = pl.col(name).cast(pl.Utf8, strict=False).str.strip_chars()
    parsed = (
        text.str.to_datetime(format=str(pattern), strict=False)
        if pattern
        else _parsed_datetime(text, time_unit="us")
    )

    return lazy.with_columns(parsed.alias(name))


def _parsed_date(text: pl.Expr) -> pl.Expr:
    """Текст в дату: первая подошедшая запись из списка, для каждого значения своя."""
    return pl.coalesce(
        [text.str.strip_chars().str.to_date(format=pattern, strict=False) for pattern in DATE_FORMATS]
    )


def _parsed_datetime(text: pl.Expr, *, time_unit: Any) -> pl.Expr:
    """Текст в дату и время; запись без времени понимается как полночь."""
    stripped = text.str.strip_chars()

    return pl.coalesce(
        [
            *(
                stripped.str.to_datetime(format=pattern, strict=False, time_unit=time_unit)
                for pattern in DATETIME_FORMATS
            ),
            *(
                stripped.str.to_date(format=pattern, strict=False).cast(pl.Datetime(time_unit))
                for pattern in DATE_FORMATS
            ),
        ]
    )


def _validate_parse_datetime(params: dict[str, Any], schema: pl.Schema) -> None:
    name = _text(params, "column")
    _require_present((name,), schema)

    #разбирается запись даты, то есть текст. Число или уже дата здесь давали пятисотку
    if not _textual(schema[name]):
        raise PipelineError(
            f"Разобрать как дату можно только текстовую колонку, а «{name}» — {schema[name]}.",
            details={"column": name, "dtype": str(schema[name])},
        )


def _validate_text_columns(params: dict[str, Any], schema: pl.Schema) -> None:
    _text_columns(_columns(params), schema)


# ── происхождение ─────────────────────────────────────────────────────────────


def _listed(params: dict[str, Any], key: str) -> tuple[str, ...]:
    #параметры к этому моменту уже проверены: эффект описывает шаг, а не валидирует его
    return tuple(str(name) for name in params.get(key) or [])


def _mutates_column(params: dict[str, Any]) -> ColumnEffect:
    return ColumnMutation((str(params.get("column", "")),))


def _mutates_columns(params: dict[str, Any]) -> ColumnEffect:
    return ColumnMutation(_listed(params, "columns"))


def _rows_only(_: dict[str, Any]) -> ColumnEffect:
    return DatasetOnlyEffect()


# ── реестр ────────────────────────────────────────────────────────────────────


OPERATIONS: dict[StepKind, Operation] = {
    StepKind.DROP_COLUMNS: Operation(
        StepKind.DROP_COLUMNS,
        "Удалить колонки",
        _validate_drop_columns,
        _drop_columns,
        lambda params: f"Удалить колонки: {', '.join(params.get('columns') or [])}",
        lambda params: tuple(params.get("columns") or []),
        lambda params: ColumnDrop(_listed(params, "columns")),
    ),
    StepKind.RENAME_COLUMN: Operation(
        StepKind.RENAME_COLUMN,
        "Переименовать колонку",
        _validate_rename,
        lambda lazy, params, _: lazy.rename({params["column"]: params["to"]}),
        lambda params: f"Переименовать «{params.get('column')}» в «{params.get('to')}»",
        lambda params: (str(params.get("column", "")),),
        lambda params: ColumnRename(str(params.get("column", "")), str(params.get("to", ""))),
    ),
    StepKind.REORDER_COLUMNS: Operation(
        StepKind.REORDER_COLUMNS,
        "Переставить колонки",
        _validate_reorder,
        lambda lazy, params, _: lazy.select(list(params["order"])),
        lambda params: f"Переставить колонки: {', '.join(params.get('order') or [])}",
        lambda params: tuple(params.get("order") or []),
        _rows_only,
    ),
    StepKind.DUPLICATE_COLUMN: Operation(
        StepKind.DUPLICATE_COLUMN,
        "Скопировать колонку",
        _validate_duplicate,
        lambda lazy, params, _: lazy.with_columns(
            pl.col(params["column"]).alias(params["to"])
        ),
        lambda params: f"Скопировать «{params.get('column')}» в «{params.get('to')}»",
        lambda params: (str(params.get("column", "")),),
        lambda params: ColumnCreate((str(params.get("column", "")),), str(params.get("to", ""))),
    ),
    StepKind.ADD_COLUMN: Operation(
        StepKind.ADD_COLUMN,
        "Новая колонка по формуле",
        _validate_add_column,
        _add_column,
        lambda params: f"Новая колонка «{params.get('name')}» = {params.get('expression')}",
        _formula_reads,
        lambda params: ColumnCreate(_formula_reads(params), str(params.get("name", ""))),
    ),
    StepKind.CAST_COLUMN: Operation(
        StepKind.CAST_COLUMN,
        "Сменить тип колонки",
        _validate_cast,
        _cast,
        lambda params: f"Привести «{params.get('column')}» к типу «{params.get('to')}»",
        lambda params: (str(params.get("column", "")),),
        _mutates_column,
    ),
    StepKind.FILTER_ROWS: Operation(
        StepKind.FILTER_ROWS,
        "Оставить строки",
        _validate_predicate,
        #строки, где условие не проверить (пропуск), не оставляются
        lambda lazy, params, schema: lazy.filter(_predicate(params, schema).fill_null(value=False)),
        lambda params: (
            f"Оставить строки, где «{params.get('column')}» "
            f"{params.get('operator')} {params.get('value', '')}".strip()
        ),
        lambda params: (str(params.get("column", "")),),
        _rows_only,
    ),
    StepKind.DROP_ROWS: Operation(
        StepKind.DROP_ROWS,
        "Удалить строки",
        _validate_predicate,
        #удаляются только строки, где условие истинно. Прежде отрицание null давало null,
        #и «удалить, где сумма > 100» удаляло заодно все строки с пропуском в сумме
        lambda lazy, params, schema: lazy.filter(~_predicate(params, schema).fill_null(value=False)),
        lambda params: (
            f"Удалить строки, где «{params.get('column')}» "
            f"{params.get('operator')} {params.get('value', '')}".strip()
        ),
        lambda params: (str(params.get("column", "")),),
        _rows_only,
    ),
    StepKind.SORT_ROWS: Operation(
        StepKind.SORT_ROWS,
        "Отсортировать строки",
        _validate_sort,
        _sort,
        lambda params: (
            f"Отсортировать по «{params.get('column')}»"
            f"{' по убыванию' if params.get('descending') else ''}"
        ),
        lambda params: (str(params.get("column", "")),),
        _rows_only,
    ),
    StepKind.REPLACE_VALUES: Operation(
        StepKind.REPLACE_VALUES,
        "Заменить значения",
        _validate_replace,
        _replace,
        lambda params: (
            f"В «{params.get('column')}» заменить «{params.get('from', '')}» "
            f"на «{params.get('to', '')}»"
        ),
        lambda params: (str(params.get("column", "")),),
        _mutates_column,
    ),
    StepKind.FILL_MISSING: Operation(
        StepKind.FILL_MISSING,
        "Заполнить пропуски",
        _validate_fill,
        _fill_missing,
        lambda params: f"Заполнить пропуски в «{params.get('column')}» значением «{params.get('value', '')}»",
        lambda params: (str(params.get("column", "")),),
        _mutates_column,
    ),
    StepKind.NORMALIZE_NULLS: Operation(
        StepKind.NORMALIZE_NULLS,
        "Привести пометки к пропускам",
        _validate_text_columns,
        _normalize_nulls,
        lambda params: (
            f"Считать пропуском пометки вроде «н/д» в: {', '.join(params.get('columns') or [])}"
        ),
        lambda params: tuple(params.get("columns") or []),
        _mutates_columns,
    ),
    StepKind.DEDUPLICATE: Operation(
        StepKind.DEDUPLICATE,
        "Убрать дубликаты",
        _validate_deduplicate,
        _deduplicate,
        lambda params: (
            "Оставить по одной строке из повторов"
            + (f" по колонкам: {', '.join(params['columns'])}" if params.get("columns") else "")
        ),
        lambda params: tuple(params.get("columns") or []),
        _rows_only,
    ),
    StepKind.TRIM_WHITESPACE: Operation(
        StepKind.TRIM_WHITESPACE,
        "Убрать краевые пробелы",
        _validate_text_columns,
        _trim,
        lambda params: f"Убрать пробелы по краям в: {', '.join(params.get('columns') or [])}",
        lambda params: tuple(params.get("columns") or []),
        _mutates_columns,
    ),
    StepKind.CHANGE_CASE: Operation(
        StepKind.CHANGE_CASE,
        "Привести регистр",
        _validate_case,
        _change_case,
        lambda params: (
            f"Привести регистр ({params.get('case', 'lower')}) в: "
            f"{', '.join(params.get('columns') or [])}"
        ),
        lambda params: tuple(params.get("columns") or []),
        _mutates_columns,
    ),
    StepKind.PARSE_NUMBER: Operation(
        StepKind.PARSE_NUMBER,
        "Разобрать как число",
        _validate_parse_number,
        _parse_number,
        lambda params: f"Разобрать «{params.get('column')}» как число",
        lambda params: (str(params.get("column", "")),),
        _mutates_column,
    ),
    StepKind.PARSE_DATETIME: Operation(
        StepKind.PARSE_DATETIME,
        "Разобрать как дату",
        _validate_parse_datetime,
        _parse_datetime,
        lambda params: f"Разобрать «{params.get('column')}» как дату",
        lambda params: (str(params.get("column", "")),),
        _mutates_column,
    ),
}


def operation_for(step: Step) -> Operation:
    operation = OPERATIONS.get(step.kind)

    if operation is None:  # pragma: no cover - перечень закрыт и совпадает с StepKind
        raise PipelineError(f"Операция не реализована: {step.kind}.", details={"kind": step.kind})

    return operation
