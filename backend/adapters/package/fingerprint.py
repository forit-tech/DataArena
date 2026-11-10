"""dataarena-logical-sha256-v1 — эталонная реализация logical dataset fingerprint.

Отпечаток описывает ЛОГИЧЕСКОЕ содержимое датасета и не зависит от версии библиотек,
Parquet writer, сжатия, dictionary encoding, разбиения на row group и метаданных файла.
Поэтому он одинаков для одних и тех же данных в Parquet, Arrow IPC и CSV с теми же типами.

Спецификация: docs/contracts/dataset-package-v1.md, §D.
"""

from __future__ import annotations

import hashlib
import unicodedata
from decimal import Decimal

import numpy as np
import polars as pl

ALGORITHM = "dataarena-logical-sha256-v1"
_FIELD_SEPARATOR = b"\x1e"
_RECORD_SEPARATOR = b"\x1d"
_CANONICAL_NAN = np.uint64(0x7FF8000000000000)
_NEGATIVE_ZERO = np.uint64(0x8000000000000000)

_INTEGER_TYPES = (
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
)


class UnsupportedColumnTypeError(ValueError):
    """Тип колонки не имеет канонического представления в этом алгоритме.

    Неописанный тип обязан быть явной ошибкой сборки пакета, а не тихо стать строкой:
    иначе Categorical, List и Struct хешировались бы через Python-репрезентацию,
    которая не гарантирована между версиями библиотеки, и две добросовестные
    реализации разошлись бы молча.
    """


def logical_type(dtype: pl.DataType) -> str:
    #эта функция сводит физический тип к одному из десяти логических имён контракта
    #ширина целого в отпечаток не входит: Int32 и Int64 с теми же значениями — один и тот же датасет,
    #иначе безубыточная конвертация CSV -> Parquet меняла бы идентичность данных
    if dtype == pl.Boolean:
        return "boolean"
    if dtype in _INTEGER_TYPES:
        return "integer"
    if dtype in (pl.Float32, pl.Float64):
        return "float"
    if isinstance(dtype, pl.Decimal):
        return "decimal"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "datetime"
    if isinstance(dtype, pl.Duration):
        return "duration"
    if dtype == pl.Time:
        return "time"
    if dtype == pl.Binary:
        return "binary"
    if dtype == pl.String:
        return "string"

    raise UnsupportedColumnTypeError(
        f"Тип {dtype} не имеет канонического представления в {ALGORITHM}. "
        "Приведите колонку к поддерживаемому типу перед сборкой пакета."
    )


def _length_prefixed(blobs: list[bytes]) -> bytes:
    #эта функция кодирует последовательность байтовых значений так, чтобы её нельзя было прочитать двояко
    #блок длин идёт до полезной нагрузки, поэтому ["ab", "c"] и ["a", "bc"] дают разные байты
    lengths = np.fromiter((len(blob) for blob in blobs), dtype="<u4", count=len(blobs))
    return lengths.tobytes() + b"".join(blobs)


def _value_bytes(series: pl.Series, logical: str) -> bytes:
    #эта функция кодирует значения колонки в фиксированное little-endian представление
    #null заменяется нейтральным значением: сам факт пропуска несёт отдельно хешируемая маска,
    #поэтому пустая строка и null различаются, хотя их полезная нагрузка одинаково пуста
    if logical == "integer":
        return series.fill_null(0).cast(pl.Int64).to_numpy().astype("<i8", copy=False).tobytes()

    if logical == "float":
        values = series.fill_null(0.0).cast(pl.Float64).to_numpy().astype("<f8", copy=False).copy()
        bits = values.view(np.uint64)
        #все варианты NaN сводятся к одному представлению: иначе отпечаток зависел бы от способа получения NaN
        bits[np.isnan(values)] = _CANONICAL_NAN
        #по IEEE-754 -0.0 == 0.0, значит и отпечатки обязаны совпадать
        bits[bits == _NEGATIVE_ZERO] = np.uint64(0)
        return bits.astype("<u8", copy=False).tobytes()

    if logical == "decimal":
        #масштаб нормализуется: 1.50 и 1.5 — одно и то же число, значит и один отпечаток
        #представление десятичное строковое, без float: точность не теряется
        blobs = [
            b"" if value is None else format(Decimal(str(value)).normalize(), "f").encode("ascii")
            for value in series.to_list()
        ]
        return _length_prefixed(blobs)

    if logical == "boolean":
        return series.fill_null(False).cast(pl.UInt8).to_numpy().astype("<u1", copy=False).tobytes()

    if logical == "date":
        return series.cast(pl.Int32).fill_null(0).to_numpy().astype("<i4", copy=False).tobytes()

    if logical == "datetime":
        #время приводится к UTC и микросекундам: единица и зона не должны влиять на отпечаток
        normalized = series.cast(pl.Datetime(time_unit="us", time_zone="UTC"), strict=False)
        return normalized.cast(pl.Int64).fill_null(0).to_numpy().astype("<i8", copy=False).tobytes()

    if logical in ("duration", "time"):
        return series.cast(pl.Int64).fill_null(0).to_numpy().astype("<i8", copy=False).tobytes()

    if logical == "string":
        #NFC-нормализация обязательна: «é» одним кодовым пунктом и «e» + комбинирующий акцент
        #выглядят одинаково и обязаны давать один отпечаток
        normalized = [
            "" if value is None else unicodedata.normalize("NFC", value)
            for value in series.cast(pl.String).to_list()
        ]
        #длины считаются векторно по нормализованным строкам, а кодировка выполняется один раз
        #для всего блока: миллион отдельных вызовов encode() стоит втрое дороже
        lengths = pl.Series(normalized).str.len_bytes().to_numpy().astype("<u4", copy=False)
        return lengths.tobytes() + "".join(normalized).encode("utf-8")

    return _length_prefixed([b"" if value is None else bytes(value) for value in series.to_list()])


def content_fingerprint(frame: pl.DataFrame) -> str:
    #эта функция считает logical fingerprint таблицы
    #в хеш входят: идентификатор алгоритма, число строк, число колонок, затем для каждой колонки
    #по порядку — имя, логический тип, маска пропусков и значения
    digest = hashlib.sha256()
    digest.update(ALGORITHM.encode("ascii") + b"\n")
    digest.update(str(frame.height).encode("ascii") + _FIELD_SEPARATOR)
    digest.update(str(frame.width).encode("ascii") + _RECORD_SEPARATOR)

    for name, dtype in frame.schema.items():
        logical = logical_type(dtype)
        series = frame[name]
        name_bytes = unicodedata.normalize("NFC", name).encode("utf-8")

        digest.update(len(name_bytes).to_bytes(4, "little"))
        digest.update(name_bytes)
        digest.update(logical.encode("ascii") + _FIELD_SEPARATOR)

        null_mask = np.packbits(series.is_null().to_numpy(), bitorder="little").tobytes()
        digest.update(hashlib.sha256(null_mask).digest())
        digest.update(hashlib.sha256(_value_bytes(series, logical)).digest())
        digest.update(_RECORD_SEPARATOR)

    return digest.hexdigest()
