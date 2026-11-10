"""Выгрузка результата в файл.

Выгружается **результат конвейера**, а не исходный файл: пользователь, почистивший
данные, ожидает получить почищенное. Если шагов нет, результат совпадает с исходником,
и это верно само собой.

Два решения стоит объяснить.

**Потоковая запись.** Там, где формат её допускает, результат пишется потоково
(`sink_*`): датасет на миллион строк не обязан целиком оказаться в памяти ради того,
чтобы стать файлом. XLSX и JSON так не умеют — у них формат требует знать всё сразу,
и для них запись идёт через материализацию с честным пределом по числу строк.

**Формулы в таблицах.** Значение, начинающееся с `=`, `+`, `-` или `@`, в Excel и
LibreOffice становится формулой при открытии файла. Это известный способ выполнить
чужой код на машине того, кто открыл выгрузку, и защищаться от него обязан тот, кто
файл создаёт. Такие значения помечаются апострофом — Excel показывает текст как текст.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from backend.adapters.formats.registry import (
    capabilities_for_key,
    writable_formats,
    write_dataset,
)
from backend.core.errors import AppError

#форматы, куда умеем писать потоково: результат не собирается в памяти целиком
STREAMING_FORMATS = frozenset({"csv", "parquet", "jsonl"})

#предел строк для форматов, требующих материализации. XLSX и так ограничен самим форматом,
#а JSON на миллион строк — это гигабайт текста, который никто не откроет
MATERIALIZED_ROW_LIMIT = 1_000_000

#значение, которое табличный редактор выполнит как формулу: начинается с «=», «@»,
#табуляции или перевода строки — или с «+»/«-», если дальше есть что-то кроме цифр,
#пробелов и знаков записи числа или телефона. Прежде помечалось всё, что начинается
#с «+» или «-»: «-5» выгружалось как «'-5», а телефон «+7 999» — как «'+7 999», то есть
#выгрузка меняла данные. Формула, способная что-то выполнить, требует букв или знаков
#вроде «|» и «!», и она по-прежнему помечается
FORMULA_PATTERN = r"^([=@\t\r]|[+-].*[^0-9\s.,()/+\-])"

#форматы, где значение может стать формулой. XLSX сюда не входит: строки записываются
#в него строками, и апостроф там не защищал, а появлялся в ячейке как часть значения
SPREADSHEET_FORMATS = frozenset({"csv", "tsv"})

_UNSAFE_NAME = re.compile(r"[^\w\s.\-()]+", re.UNICODE)


class ExportError(AppError):
    status_code = 422
    code = "export_failed"


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    file_name: str
    media_type: str
    rows: int


MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
    "parquet": "application/vnd.apache.parquet",
    "json": "application/json; charset=utf-8",
    "jsonl": "application/x-ndjson; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "feather": "application/vnd.apache.arrow.file",
    #и feather, и arrow пишутся как IPC-файл (write_ipc), а не как поток: объявлять
    #потоковый тип значило бы соврать потребителю о формате содержимого
    "arrow": "application/vnd.apache.arrow.file",
}


def formats() -> list[dict[str, Any]]:
    """Форматы записи вместе с ограничениями — для интерфейса.

    Маршрут берёт их отсюда, а не из адаптера напрямую: направление зависимостей
    `api → services → adapters` проверяется тестом, и обход его ломает.
    """
    return [capabilities.to_dict() for capabilities in writable_formats()]


def safe_file_name(name: str, extension: str) -> str:
    """Имя файла, которое не уведёт запись за пределы каталога и не сломает заголовок.

    Имя приходит от пользователя и попадает в `Content-Disposition`. Разделители пути,
    управляющие символы и кавычки здесь недопустимы — не из осторожности, а потому что
    ими и уводят запись в чужой каталог.
    """
    normalised = unicodedata.normalize("NFC", name).strip()
    #точки в начале и разделители пути убираются до фильтра: «../» не должно превратиться
    #в «..» и остаться
    normalised = normalised.replace("/", " ").replace("\\", " ").lstrip(". ")
    cleaned = _UNSAFE_NAME.sub("", normalised).strip() or "dataset"
    stem = cleaned.rsplit(".", 1)[0] if "." in cleaned else cleaned

    return f"{stem[:80]}.{extension}"


def neutralise_formulas(lazy: pl.LazyFrame, schema: pl.Schema) -> pl.LazyFrame:
    """Обезвреживает значения, которые табличный редактор примет за формулу."""
    textual = [
        name
        for name, dtype in schema.items()
        if dtype == pl.Utf8 or isinstance(dtype, pl.Categorical | pl.Enum)
    ]

    if not textual:
        return lazy

    pattern = FORMULA_PATTERN

    return lazy.with_columns(
        [
            pl.when(pl.col(name).cast(pl.Utf8, strict=False).str.contains(pattern))
            .then(pl.lit("'") + pl.col(name).cast(pl.Utf8, strict=False))
            .otherwise(pl.col(name).cast(pl.Utf8, strict=False))
            .alias(name)
            for name in textual
        ]
    )


def drop_time_zone(lazy: pl.LazyFrame, schema: pl.Schema) -> pl.LazyFrame:
    """Приводит время с поясом к UTC — иначе Excel отказывается его записывать.

    Измерено: выгрузка колонки `Datetime(time_zone="UTC")` в XLSX даёт необработанный
    `TypeError` из xlsxwriter, то есть пятисотку на обычном датасете с временем.
    Excel не хранит часовой пояс вовсе, поэтому выбор такой: отказать целиком или
    записать момент в UTC и **сказать об этом заранее**. Сказано: предупреждение
    формата видно в интерфейсе до нажатия «Выгрузить».
    """
    zoned = [
        name
        for name, dtype in schema.items()
        if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None
    ]

    if not zoned:
        return lazy

    return lazy.with_columns(
        [pl.col(name).dt.convert_time_zone("UTC").dt.replace_time_zone(None) for name in zoned]
    )


def export(
    lazy: pl.LazyFrame,
    schema: pl.Schema,
    fmt: str,
    directory: Path,
    name: str,
) -> ExportResult:
    """Пишет результат в файл выбранного формата и возвращает путь к нему."""
    capabilities = capabilities_for_key(fmt)

    if not capabilities.can_write:
        raise ExportError(
            f"В формат {capabilities.label} записывать нельзя.", details={"format": fmt}
        )

    file_name = safe_file_name(name, capabilities.extensions[0].lstrip("."))
    path = directory / file_name
    prepared = neutralise_formulas(lazy, schema) if fmt in SPREADSHEET_FORMATS else lazy

    if fmt == "xlsx":
        prepared = drop_time_zone(prepared, prepared.collect_schema())

    if fmt in STREAMING_FORMATS:
        rows = _sink(prepared, path, fmt)
    else:
        rows = _materialize(prepared, path, fmt, capabilities.row_limit)

    return ExportResult(
        path=path,
        file_name=file_name,
        media_type=MEDIA_TYPES.get(fmt, "application/octet-stream"),
        rows=rows,
    )


def _sink(lazy: pl.LazyFrame, path: Path, fmt: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        lazy.sink_csv(path)
    elif fmt == "parquet":
        lazy.sink_parquet(path)
    else:
        lazy.sink_ndjson(path)

    #число строк берётся из плана, а не из записанного файла: считать заново значило бы
    #прочитать всё, что только что записали
    return int(lazy.select(pl.len()).collect().item())


def _materialize(lazy: pl.LazyFrame, path: Path, fmt: str, row_limit: int | None) -> int:
    rows = int(lazy.select(pl.len()).collect().item())
    limit = min(row_limit or MATERIALIZED_ROW_LIMIT, MATERIALIZED_ROW_LIMIT)

    if rows > limit:
        #обрезать молча нельзя: пользователь унесёт файл, считая его полным
        raise ExportError(
            f"В этот формат помещается {limit} строк, а в датасете {rows}. "
            "Выберите CSV или Parquet — они пишутся потоково и без предела.",
            details={"rows": rows, "limit": limit, "format": fmt},
        )

    write_dataset(lazy.collect(), path, fmt)

    return rows
