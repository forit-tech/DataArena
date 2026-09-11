"""Доменные модели workspace, датасета и производного артефакта.

Слой не знает ни про HTTP, ни про SQLite, ни про Polars: здесь только то, чем оперирует
предметная область. Хранилище и форматы подключаются адаптерами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class LogicalType(StrEnum):
    #те же десять имён, что участвуют в отпечатке контракта: список общий намеренно,
    #иначе схема пакета и схема датасета начали бы расходиться в терминах
    BOOLEAN = "boolean"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    DURATION = "duration"
    TIME = "time"
    STRING = "string"
    BINARY = "binary"


class SemanticType(StrEnum):
    #смысловой тип поверх технического: именно он определяет, как колонку показывать,
    #что о ней считать и что предлагать пользователю
    ID = "id"
    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    TEXT = "text"


class DatasetStatus(StrEnum):
    """Состояние импорта.

    Перечислены только состояния, которые код действительно производит. `NORMALIZING`
    здесь намеренно нет: конвертация выполняется синхронно, и датасет никогда не видим
    пользователю в этом состоянии. Значение появится вместе со слоем фоновых задач —
    статус, который никто не выставляет, врёт интерфейсу.

    Отдельное состояние для неудачной нормализации нужно потому, что исходный файл при
    этом уже загружен и цел: датасет существует, но работать с ним нельзя, и пользователю
    надо сказать почему, а не потерять загрузку молча.
    """

    READY = "ready"
    NORMALIZATION_FAILED = "normalization_failed"


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    #описание одной колонки в том виде, в каком его отдаёт reader и потребляет всё остальное
    name: str
    position: int
    physical_type: str
    logical_type: LogicalType
    semantic_type: SemanticType
    nullable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "position": self.position,
            "physical_type": self.physical_type,
            "logical_type": self.logical_type.value,
            "semantic_type": self.semantic_type.value,
            "nullable": self.nullable,
        }


@dataclass(frozen=True, slots=True)
class DatasetSchema:
    columns: tuple[ColumnSchema, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def column(self, name: str) -> ColumnSchema | None:
        return next((column for column in self.columns if column.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {"columns": [column.to_dict() for column in self.columns]}


@dataclass(frozen=True, slots=True)
class DatasetSource:
    #откуда датасет взялся: имя, как его назвал пользователь, формат и отпечаток содержимого
    #исходный файл неизменяем и никогда не перезаписывается — ни нормализацией, ни правками
    file_name: str
    format: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DerivedArtifact:
    """Нормализованный Parquet — внутренний рабочий артефакт, а не новый источник.

    Существует только для форматов, которые не читаются лениво (JSON, XLSX). Для CSV,
    TSV, Parquet, JSONL, Feather и Arrow нормализация не выполняется: она решала бы
    несуществующую проблему и удваивала бы диск без причины.

    Провенанс хранится целиком, чтобы артефакт можно было признать устаревшим
    и пересобрать: при смене версии конвертера прежний Parquet мог быть получен
    другой логикой, и молча продолжать им пользоваться нельзя.
    """

    path: str
    source_sha256: str
    derived_sha256: str
    source_format: str
    derived_format: str
    #версия конвертера: её изменение делает существующий артефакт недействительным
    converter: str
    converter_version: int
    created_at: datetime
    row_count: int
    #что могло потеряться при конвертации — берётся из объявленных возможностей формата
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_format": self.source_format,
            "derived_format": self.derived_format,
            "converter": self.converter,
            "converter_version": self.converter_version,
            "created_at": self.created_at.isoformat(),
            "row_count": self.row_count,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class Dataset:
    #датасет в workspace: источник, схема, счётчики, состояние и, возможно, производный артефакт
    #row_count может быть неизвестен до первого полного прохода: для потоковых форматов
    #его выяснение стоит денег, и врать точным числом нельзя
    dataset_id: str
    workspace_id: str
    name: str
    #имя, под которым датасет доступен в SQL: пользователь пишет FROM продажи,
    #а связывание с артефактом делает backend
    alias: str
    source: DatasetSource
    schema: DatasetSchema
    row_count: int | None
    created_at: datetime
    status: DatasetStatus = DatasetStatus.READY
    status_reason: str | None = None
    derived: DerivedArtifact | None = None

    @property
    def column_count(self) -> int:
        return len(self.schema.columns)

    @property
    def artifact_fingerprint(self) -> str:
        """Отпечаток того артефакта, из которого данные читаются на самом деле.

        Тот же выбор, что и в `working_format`, и по той же причине: выше по стеку
        никто не должен знать, читаем мы исходный файл или нормализованный. Повторить
        это правило в другом месте — значит однажды получить два разных ответа.
        """
        return self.derived.derived_sha256 if self.derived else self.source.sha256

    @property
    def working_format(self) -> str:
        #формат, из которого backend фактически читает данные
        #frontend этим не управляет и не обязан знать, откуда идёт чтение — он только показывает
        return self.derived.derived_format if self.derived else self.source.format

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "alias": self.alias,
            "source": {
                "file_name": self.source.file_name,
                "format": self.source.format,
                "bytes": self.source.bytes,
                "sha256": self.source.sha256,
            },
            "schema": self.schema.to_dict(),
            "row_count": self.row_count,
            "column_count": self.column_count,
            "status": self.status.value,
            "status_reason": self.status_reason,
            "original_format": self.source.format,
            "working_format": self.working_format,
            "normalized": self.derived is not None,
            "conversion": self.derived.to_dict() if self.derived else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class Workspace:
    workspace_id: str
    name: str
    created_at: datetime
    datasets: list[Dataset] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "dataset_count": len(self.datasets),
        }
