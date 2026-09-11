"""Окно SQL-запросов: связывание датасетов, выполнение, история, сохранённые запросы.

Пользователь не передаёт путь к файлу. Он пишет `FROM продажи`, backend разбирает запрос,
находит в рабочем пространстве датасет с таким псевдонимом и подключает **его артефакт** —
тот самый, из которого читает таблица. Никакого другого способа попасть в запрос у файла нет.

Порядок здесь существенный и повторяет порядок в движке:

1. запрос разбирается без доступа к данным — так становится известно, что он читает;
2. имена сопоставляются с датасетами рабочего пространства; неизвестное имя даёт отказ
   со списком доступных, а не ошибку движка «table not found»;
3. открывается соединение ровно с этими артефактами, и после регистрации оно запирается;
4. запрос выполняется с ограничениями и попадает в историю — независимо от исхода.

Запись в историю выполняется и при ошибке. История, в которой видны только удачные
запросы, отвечает не на тот вопрос: чаще всего к ней возвращаются именно после неудачи.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.adapters.engine.duckdb_session import (
    MAX_QUERY_LENGTH,
    ParsedQuery,
    QueryHandle,
    QueryLimits,
    QueryResult,
    SqlCancelledError,
    SqlError,
    SqlRejectedError,
    SqlTimeoutError,
    execute_query,
    hardened_session,
    parse_query,
)
from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import AppError
from backend.core.logging import get_logger
from backend.domain.dataset.identifiers import (
    ensure_workspace_id,
    new_query_run_id,
    new_saved_query_id,
)
from backend.domain.dataset.models import Dataset, DatasetStatus
from backend.domain.sql.models import QueryRun, QueryStatus, SavedQuery
from backend.services.workspace_access import reading_path

#имя сохранённого запроса показывается в списке: слишком длинное его ломает,
#пустое делает запись неотличимой от соседних
MAX_SAVED_NAME_LENGTH = 120
MAX_SAVED_DESCRIPTION_LENGTH = 1_000


class UnknownDatasetError(AppError):
    #неизвестное имя таблицы — ошибка пользователя, а не сбой: в ответе перечисляется,
    #что доступно, иначе остаётся гадать, как называется датасет в SQL
    status_code = 400
    code = "unknown_dataset"


class DatasetNotQueryableError(AppError):
    status_code = 409
    code = "dataset_not_queryable"


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """Результат вместе с записью в историю: интерфейс показывает и то, и другое."""

    result: QueryResult
    run: QueryRun


@dataclass(slots=True)
class _Attempt:
    """Состояние одной попытки выполнения: что запускали, когда и с чем связали.

    Собрано в один объект, потому что запись в историю нужна из нескольких мест — при
    успехе и при каждой ошибке, — и передавать четыре значения по отдельности значит
    однажды перепутать местами два одинаковых по типу.
    """

    parsed: ParsedQuery
    started_at: datetime
    started: float
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SqlWorkspace:
    """Окно запросов одного рабочего пространства.

    Хранилище, корень файлов и идентификатор нужны каждой операции вместе; передавать их
    по отдельности значит однажды передать чужой идентификатор к своему хранилищу.
    """

    store: WorkspaceStore
    workspace_root: Path
    workspace_id: str

    def __post_init__(self) -> None:
        ensure_workspace_id(self.workspace_id)

    # ── выполнение ────────────────────────────────────────────────────────────

    def run(
        self,
        sql: str,
        limits: QueryLimits | None = None,
        handle: QueryHandle | None = None,
    ) -> QueryOutcome:
        """Выполняет запрос над датасетами рабочего пространства.

        `handle` создаётся вызывающим **до** запуска и уже зарегистрирован там, где его
        найдёт отмена. Соединение оно получает здесь, когда сессия открыта. Не передан —
        значит отменять некому, и кнопку отмены в таком случае показывать нельзя.
        """
        #существование рабочего пространства проверяется до разбора: иначе на чужой
        #идентификатор отвечали бы разбором запроса, а не «не найдено»
        self.store.get_workspace(self.workspace_id)

        #разбор входит в измеряемую и записываемую часть: запрос, отвергнутый на разборе,
        #обязан попасть в историю — именно его там ищут, когда не понимают, почему отказ
        attempt = _Attempt(
            parsed=ParsedQuery(sql=_shortened(sql), tables=()),
            started_at=datetime.now(UTC),
            started=time.perf_counter(),
        )

        try:
            attempt.parsed = parse_query(sql)
            bindings, attempt.aliases = self._resolve_bindings(attempt.parsed)

            with hardened_session(bindings, limits) as connection:
                if handle is not None:
                    handle.attach(connection)

                result = execute_query(
                    connection, attempt.parsed.sql, limits=limits, handle=handle
                )
        except AppError as error:
            self._record(attempt, error=error)
            raise

        return QueryOutcome(result=result, run=self._record(attempt, result=result))

    def _resolve_bindings(
        self, parsed: ParsedQuery
    ) -> tuple[dict[str, Path], tuple[str, ...]]:
        """Сопоставляет имена из запроса с артефактами. Путь берётся здесь и только здесь.

        Возвращает связывание (ключ — имя ровно так, как написано в запросе, иначе движок
        его не найдёт) и канонические псевдонимы найденных датасетов: в историю уходят
        именно они, а не написание из конкретного запроса.
        """
        if not parsed.tables:
            #запрос без единой таблицы читать нечего: «SELECT 1» не является работой
            #с данными, а без связанных датасетов проверка плана всё равно не найдёт
            #разрешённого источника и ответила бы куда менее понятно
            raise SqlRejectedError(
                "Запрос не обращается ни к одному датасету.",
                details={"available": self._available_aliases()},
            )

        #сопоставление без учёта регистра: «FROM Продажи» и «FROM продажи» — одно и то же.
        #Сам DuckDB так поступает только с латиницей, поэтому приводим имена сами
        datasets = {
            dataset.alias.casefold(): dataset
            for dataset in self.store.list_datasets(self.workspace_id)
        }
        bindings: dict[str, Path] = {}
        aliases: list[str] = []

        for name in parsed.tables:
            dataset = datasets.get(name.casefold())

            if dataset is None:
                raise UnknownDatasetError(
                    f"В рабочем пространстве нет датасета «{name}».",
                    details={
                        "requested": name,
                        "available": sorted(item.alias for item in datasets.values()),
                    },
                )

            #ключ — написание из запроса: под ним движок и будет искать таблицу
            bindings[name] = self._queryable_path(dataset)
            aliases.append(dataset.alias)

        return bindings, tuple(aliases)

    def _queryable_path(self, dataset: Dataset) -> Path:
        if dataset.status is not DatasetStatus.READY:
            raise DatasetNotQueryableError(
                f"Датасет «{dataset.alias}» недоступен для запросов: "
                f"{dataset.status_reason or dataset.status.value}.",
                details={"alias": dataset.alias, "status": dataset.status.value},
            )

        path = reading_path(self.workspace_root, dataset)

        if not path.exists():
            #датасет удалён между чтением списка и открытием соединения: файл исчез,
            #и запрос выполнить нельзя. Для вызывающего это то же самое, что
            #несуществующий датасет, а не внутренняя ошибка сервера
            raise UnknownDatasetError(
                f"Файл датасета «{dataset.alias}» недоступен. Возможно, он был удалён.",
                details={"requested": dataset.alias},
            )

        return path

    def _available_aliases(self) -> list[str]:
        return sorted(dataset.alias for dataset in self.store.list_datasets(self.workspace_id))

    def _record(
        self,
        attempt: _Attempt,
        result: QueryResult | None = None,
        error: AppError | None = None,
    ) -> QueryRun:
        run = QueryRun(
            run_id=new_query_run_id(),
            workspace_id=self.workspace_id,
            sql=attempt.parsed.sql,
            status=_status_of(result, error),
            started_at=attempt.started_at,
            elapsed_ms=int((time.perf_counter() - attempt.started) * 1000),
            datasets=attempt.aliases,
            row_count=result.row_count if result else None,
            truncated=bool(result and result.truncated),
            truncated_by=result.truncated_by if result else None,
            #в историю уходит код, а не сообщение: текст движка может содержать пути
            error_code=error.code if error else None,
        )
        self.store.add_query_run(run)

        if error is not None:
            get_logger().info(
                "Запрос завершился ошибкой",
                extra={
                    "workspace_id": self.workspace_id,
                    "error_code": error.code,
                    "run_id": run.run_id,
                },
            )

        return run

    # ── сохранённые запросы ───────────────────────────────────────────────────

    def save(self, name: str, sql: str, description: str | None = None) -> SavedQuery:
        """Сохраняет запрос под именем.

        Текст проверяется разбором до сохранения: запись, которую заведомо нельзя
        выполнить, не должна попадать в список. Датасеты при этом не проверяются — они
        могут появиться позже, и требовать их наличия значило бы запрещать заготовки.
        """
        parsed = parse_query(sql)
        now = datetime.now(UTC)

        return self.store.add_saved_query(
            SavedQuery(
                saved_query_id=new_saved_query_id(),
                workspace_id=self.workspace_id,
                name=_clean_name(name),
                sql=parsed.sql,
                description=_clean_description(description),
                created_at=now,
                updated_at=now,
            )
        )

    def update(
        self, saved_query_id: str, name: str, sql: str, description: str | None = None
    ) -> SavedQuery:
        existing = self.store.get_saved_query(self.workspace_id, saved_query_id)
        parsed = parse_query(sql)

        return self.store.update_saved_query(
            SavedQuery(
                saved_query_id=existing.saved_query_id,
                workspace_id=self.workspace_id,
                name=_clean_name(name),
                sql=parsed.sql,
                description=_clean_description(description),
                #дата создания принадлежит записи, а не форме: правка её не меняет
                created_at=existing.created_at,
                updated_at=datetime.now(UTC),
            )
        )


def limits_for(row_limit: int | None, timeout_seconds: int | None) -> QueryLimits | None:
    """Собирает ограничения из того, что попросил клиент.

    Живёт в сервисном слое, а не в роутере: иначе транспорт знал бы про движок напрямую.
    Умолчания берутся у самих ограничений, а не дублируются здесь — два места, знающие
    один и тот же предел, однажды разойдутся.
    """
    if row_limit is None and timeout_seconds is None:
        return None

    defaults = QueryLimits()

    return QueryLimits(
        row_limit=row_limit or defaults.row_limit,
        timeout_seconds=timeout_seconds or defaults.timeout_seconds,
    )


def _shortened(sql: str) -> str:
    """Обрезает текст для истории.

    В историю пишется и запрос, отвергнутый по длине. Хранить его целиком значило бы
    обойти тот самый предел, из-за которого он и был отвергнут.
    """
    if len(sql) <= MAX_QUERY_LENGTH:
        return sql

    return sql[:MAX_QUERY_LENGTH] + "…"


def _status_of(result: QueryResult | None, error: AppError | None) -> QueryStatus:
    if isinstance(error, SqlTimeoutError):
        return QueryStatus.TIMED_OUT

    if isinstance(error, SqlCancelledError):
        return QueryStatus.CANCELLED

    if error is not None or result is None:
        return QueryStatus.FAILED

    return QueryStatus.SUCCEEDED


def _clean_name(name: str) -> str:
    #управляющие символы ломают вывод списка, а пустое имя делает запись неотличимой
    cleaned = "".join(character for character in name if character.isprintable()).strip()

    if not cleaned:
        raise SqlRejectedError("Имя сохранённого запроса не может быть пустым.")

    if len(cleaned) > MAX_SAVED_NAME_LENGTH:
        raise SqlRejectedError(
            f"Имя длиннее {MAX_SAVED_NAME_LENGTH} символов.",
            details={"limit": MAX_SAVED_NAME_LENGTH},
        )

    return cleaned


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None

    cleaned = description.strip()

    if not cleaned:
        return None

    if len(cleaned) > MAX_SAVED_DESCRIPTION_LENGTH:
        raise SqlRejectedError(
            f"Описание длиннее {MAX_SAVED_DESCRIPTION_LENGTH} символов.",
            details={"limit": MAX_SAVED_DESCRIPTION_LENGTH},
        )

    return cleaned


__all__ = [
    "DatasetNotQueryableError",
    "QueryOutcome",
    "SqlError",
    "SqlWorkspace",
    "UnknownDatasetError",
    "limits_for",
]
