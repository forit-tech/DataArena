"""Возможности формата объявляются, а не подразумеваются.

Export Dialog строит предупреждения из этой таблицы, а не из зашитых в интерфейс строк.
Каждое значение `preserves_schema` подтверждено round-trip-тестом: формат не попадает
в реестр, пока такой тест не написан и не прошёл.

Основание — измерения из AUDIT.md, A-3: JSON и NDJSON превращают Date и Datetime в строки,
Parquet, Arrow IPC, Avro, CSV и XLSX сохраняют типы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FormatCapabilities:
    #одно объявление на формат: всё, что о нём нужно знать читателю, писателю и интерфейсу
    key: str
    label: str
    extensions: tuple[str, ...]
    can_read: bool
    can_write: bool
    #доступно ли ленивое чтение: от этого зависит, придётся ли нормализовать файл в Parquet
    supports_lazy_scan: bool
    #переживают ли типы round-trip; значение подтверждается тестом, а не намерением автора
    preserves_schema: bool
    preserves_nulls: bool
    #предел строк формата: у XLSX он жёсткий и приводит к молчаливой потере данных
    row_limit: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "extensions": list(self.extensions),
            "can_read": self.can_read,
            "can_write": self.can_write,
            "supports_lazy_scan": self.supports_lazy_scan,
            "preserves_schema": self.preserves_schema,
            "preserves_nulls": self.preserves_nulls,
            "row_limit": self.row_limit,
            "warnings": list(self.warnings),
        }


CSV = FormatCapabilities(
    key="csv",
    label="CSV",
    extensions=(".csv",),
    can_read=True,
    can_write=True,
    supports_lazy_scan=True,
    preserves_schema=False,
    preserves_nulls=False,
    warnings=(
        "CSV не хранит типы: при обратном чтении они определяются заново по значениям.",
        "CSV не различает пустую строку и отсутствующее значение.",
    ),
)

TSV = FormatCapabilities(
    key="tsv",
    label="TSV",
    extensions=(".tsv",),
    can_read=True,
    can_write=True,
    supports_lazy_scan=True,
    preserves_schema=False,
    preserves_nulls=False,
    warnings=(
        "TSV не хранит типы: при обратном чтении они определяются заново по значениям.",
        "Значения с символом табуляции внутри требуют экранирования.",
    ),
)

PARQUET = FormatCapabilities(
    key="parquet",
    label="Parquet",
    extensions=(".parquet",),
    can_read=True,
    can_write=True,
    supports_lazy_scan=True,
    preserves_schema=True,
    preserves_nulls=True,
)

JSON = FormatCapabilities(
    key="json",
    label="JSON",
    extensions=(".json",),
    can_read=True,
    can_write=True,
    supports_lazy_scan=False,
    preserves_schema=False,
    preserves_nulls=True,
    warnings=(
        "JSON превращает даты и время в строки: при обратном чтении они перестают быть датами.",
        "JSON заметно больше по размеру, чем колоночные форматы.",
        "JSON читается целиком: ленивый доступ к части файла невозможен.",
    ),
)

JSONL = FormatCapabilities(
    key="jsonl",
    label="JSON Lines",
    extensions=(".jsonl", ".ndjson"),
    can_read=True,
    can_write=True,
    supports_lazy_scan=True,
    preserves_schema=False,
    preserves_nulls=True,
    warnings=("JSONL превращает даты и время в строки: при обратном чтении они перестают быть датами.",),
)

XLSX = FormatCapabilities(
    key="xlsx",
    label="Excel",
    extensions=(".xlsx",),
    can_read=True,
    can_write=True,
    supports_lazy_scan=False,
    preserves_schema=True,
    preserves_nulls=True,
    row_limit=1_048_576,
    warnings=(
        "Excel вмещает не более 1 048 576 строк: остальное будет потеряно.",
        "Excel читается целиком: ленивый доступ к части файла невозможен.",
    ),
)

FEATHER = FormatCapabilities(
    key="feather",
    label="Feather",
    extensions=(".feather",),
    can_read=True,
    can_write=True,
    supports_lazy_scan=True,
    preserves_schema=True,
    preserves_nulls=True,
)

ARROW = FormatCapabilities(
    key="arrow",
    label="Arrow IPC",
    extensions=(".arrow", ".ipc"),
    can_read=True,
    can_write=True,
    supports_lazy_scan=True,
    preserves_schema=True,
    preserves_nulls=True,
)

ALL_FORMATS: tuple[FormatCapabilities, ...] = (
    CSV,
    TSV,
    PARQUET,
    JSON,
    JSONL,
    XLSX,
    FEATHER,
    ARROW,
)
