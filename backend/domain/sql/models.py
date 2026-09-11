"""История и сохранённые запросы.

Хранится текст запроса и то, чем он закончился, но **не результат**. Причина не в экономии
места: результат зависит от данных, а данные меняются. Показать вчерашние строки под
сегодняшним запросом значит соврать, а пересчитать их при открытии истории — выполнить
запрос, о котором никто не просил. История отвечает на вопрос «что я запускал и чем это
кончилось», а не «что тогда получилось».

Сообщение об ошибке в историю тоже не попадает: хранится код. Текст ошибки движка может
содержать пути и внутренние подробности, а история переживает перезапуск и уезжает
в экспорт рабочего пространства.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class QueryStatus(StrEnum):
    #только те исходы, которые код действительно производит
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class QueryRun:
    """Одно выполнение запроса."""

    run_id: str
    workspace_id: str
    sql: str
    status: QueryStatus
    started_at: datetime
    elapsed_ms: int
    #псевдонимы датасетов, к которым обращался запрос: по ним видно, что он читал,
    #даже если датасет потом удалили
    datasets: tuple[str, ...] = field(default_factory=tuple)
    row_count: int | None = None
    truncated: bool = False
    truncated_by: str | None = None
    #устойчивый код вместо текста: сообщение движка может содержать пути
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sql": self.sql,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "elapsed_ms": self.elapsed_ms,
            "datasets": list(self.datasets),
            "row_count": self.row_count,
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class SavedQuery:
    """Запрос, сохранённый под именем.

    Ссылок на датасеты здесь нет по той же причине, по какой их нет в тексте: запрос
    обращается к псевдонимам, и связывание происходит в момент выполнения. Датасет,
    удалённый после сохранения, не делает запись битой — он делает запрос невыполнимым,
    и сказать об этом надо при попытке выполнить, а не потерять сохранённое.
    """

    saved_query_id: str
    workspace_id: str
    name: str
    sql: str
    created_at: datetime
    updated_at: datetime
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "saved_query_id": self.saved_query_id,
            "name": self.name,
            "sql": self.sql,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
