"""Значение предиката переживает хранение без потери типа — или находка теряет провал.

Предикат находки хранится в JSON рядом с датасетом, а применяется потом к колонке
настоящего типа. Между этими двумя моментами значение обязано остаться **тем же
самым**: не похожим, не приведённым «примерно», а тем же.

Почему это отдельный модуль, а не `json_safe_value`. Тот переводит значение в вид,
пригодный **для показа**, и переводит с потерями сознательно: `bytes` становятся
`«<2 байт>»`, `timedelta` — числом секунд. Для подписи под находкой этого достаточно.
Для предиката — нет: измерено, что `timedelta` через секунды и `bytes` через описание
не падают при восстановлении, а **молча отбирают другие строки** (3 вместо 1). Тихая
подмена выборки хуже пятисотки: пятисотку видно.

Поэтому здесь на каждый тип своя стратегия, проверенная измерением:

| тип | хранится как | восстанавливается |
|---|---|---|
| текст, категория, перечисление | строка | литерал |
| целые, дробные, булевы | число, булево | приведение к типу колонки |
| Decimal | строка | приведение к типу колонки |
| Date, Datetime, Time | ISO-строка | `str.to_date` / `to_datetime` / `to_time` |
| Duration | целое в единицах колонки | приведение к `Duration(единицы)` |
| Binary | base64 | литерал `bytes` |

Всё остальное — списки, структуры, объекты — **отказ**: значения такой колонки нельзя
уложить в предикат без домыслов, и вместо домысла находка остаётся без провала в строки.
Отказ закрытый: не «показать хоть что-нибудь», а «не показывать ничего».
"""

from __future__ import annotations

import base64
import datetime as dt
import math
from decimal import Decimal
from typing import Any

import polars as pl

from backend.domain.health.thresholds import THRESHOLDS

#микросекунда в единицах времени Polars: timedelta в Python хранит микросекунды,
#поэтому в них же и считаем, а к единицам колонки переводим целочисленно
_MICROSECONDS_PER_UNIT = {"ns": 0.001, "us": 1, "ms": 1000}


class ValueNotStorableError(Exception):
    """Значение нельзя сохранить в предикате, не потеряв его тождественность."""


def encode_filter_value(value: Any, dtype: pl.DataType) -> Any:  # noqa: PLR0911, PLR0912 - диспетчер типов
    """Переводит значение колонки в вид, который переживёт JSON без потерь.

    Возбуждает `ValueNotStorableError`, если тип не поддаётся точному восстановлению.
    Вызывающий обязан обработать отказ, а не подставлять приблизительное значение.
    """
    if value is None:
        raise ValueNotStorableError("У пропуска нет значения для предиката.")

    if dtype in (pl.Utf8, pl.Categorical, pl.Enum) or isinstance(dtype, pl.Categorical | pl.Enum):
        text = str(value)

        if len(text) > THRESHOLDS.max_filter_value_length:
            #такое значение раздувает сохранённый отчёт, оставаясь невидимым в ответе API.
            #Обрезать его нельзя: обрезанное значение отобрало бы другие строки
            raise ValueNotStorableError(
                "Значение слишком длинное, чтобы хранить его в предикате."
            )

        return text

    if dtype == pl.Boolean:
        return bool(value)

    if dtype.is_integer():
        return int(value)

    if dtype.is_float():
        number = float(value)
        #NaN и бесконечность нельзя сравнить на равенство: NaN не равен даже себе,
        #и предикат «не равно NaN» отобрал бы вообще все строки. О самих таких
        #значениях говорит отдельная находка
        if math.isnan(number) or math.isinf(number):
            raise ValueNotStorableError("Неконечное число не может быть значением предиката.")
        return number

    if isinstance(dtype, pl.Decimal):
        #строка, а не float: перевод в float теряет ту самую точность, ради которой
        #Decimal и берут, и восстановленное значение перестанет совпадать с исходным
        return str(Decimal(value))

    if dtype == pl.Date:
        return _require(value, dt.date).isoformat()

    if isinstance(dtype, pl.Datetime):
        return _require(value, dt.datetime).isoformat()

    if dtype == pl.Time:
        return _require(value, dt.time).isoformat()

    if isinstance(dtype, pl.Duration):
        delta = _require(value, dt.timedelta)
        microseconds = delta // dt.timedelta(microseconds=1)
        return int(microseconds / _MICROSECONDS_PER_UNIT[dtype.time_unit])

    if dtype == pl.Binary:
        return base64.b64encode(_require(value, bytes)).decode("ascii")

    raise ValueNotStorableError(f"Тип {dtype} не может быть значением предиката.")


def decode_filter_value(encoded: Any, dtype: pl.DataType) -> pl.Expr:  # noqa: PLR0911 - диспетчер типов
    """Восстанавливает значение предиката как литерал того же типа, что и колонка.

    Зеркало `encode_filter_value`: пара проверяется измерением на каждом поддержанном
    типе — отобранные строки обязаны совпасть со строками, отобранными исходным
    значением, до единой.
    """
    if dtype in (pl.Utf8, pl.Categorical, pl.Enum) or isinstance(dtype, pl.Categorical | pl.Enum):
        return pl.lit(str(encoded))

    if dtype == pl.Boolean:
        return pl.lit(bool(encoded))

    if dtype.is_integer() or dtype.is_float() or isinstance(dtype, pl.Decimal):
        return pl.lit(encoded).cast(dtype, strict=True)

    if dtype == pl.Date:
        return pl.lit(str(encoded)).str.to_date()

    if isinstance(dtype, pl.Datetime):
        return pl.lit(str(encoded)).str.to_datetime(
            time_unit=dtype.time_unit, time_zone=dtype.time_zone
        )

    if dtype == pl.Time:
        return pl.lit(str(encoded)).str.to_time()

    if isinstance(dtype, pl.Duration):
        return pl.lit(int(encoded)).cast(pl.Duration(dtype.time_unit), strict=True)

    if dtype == pl.Binary:
        return pl.lit(base64.b64decode(str(encoded)))

    #сюда можно попасть только с предикатом, сохранённым прежней версией проверок для
    #типа, который больше не поддерживается: подставлять вместо него что-нибудь нельзя
    raise ValueNotStorableError(f"Тип {dtype} не может быть значением предиката.")


def _require(value: Any, expected: type) -> Any:
    if not isinstance(value, expected):
        raise ValueNotStorableError(
            f"Ожидалось {expected.__name__}, получено {type(value).__name__}."
        )

    return value
