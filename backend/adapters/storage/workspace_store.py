"""Хранилище workspace на SQLite.

Почему SQLite, а не JSON-файлы: SQL history, сохранённые запросы и шаги pipeline нужно
фильтровать, сортировать по времени и удалять поштучно, а транзакции не дают прерванному
запросу оставить полузаписанное состояние. Всё это лежит в одном файле, который можно
скопировать или удалить целиком.

Почему не БД-сервер: инструмент локальный и однопользовательский. `WorkspaceStore` описан
протоколом, поэтому замена хранилища не потребует переписывать домен — тот же приём,
что в AutoDataAnalysis применён к хранилищу экспериментов.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from backend.core.errors import AppError, DatasetNotFoundError, WorkspaceNotFoundError
from backend.domain.dataset.aliases import alias_from_name, next_free_alias
from backend.domain.dataset.identifiers import (
    ensure_dataset_id,
    ensure_saved_query_id,
    ensure_workspace_id,
)
from backend.domain.dataset.models import (
    ColumnSchema,
    Dataset,
    DatasetSchema,
    DatasetSource,
    DatasetStatus,
    DerivedArtifact,
    LogicalType,
    SemanticType,
    Workspace,
)
from backend.domain.health.models import (
    HealthReport,
    finding_from_storage,
    finding_to_storage,
)
from backend.domain.sql.models import QueryRun, QueryStatus, SavedQuery

#Методы добавляются вместе со своим потребителем. attach_derived, set_status,
#list_workspaces и delete_dataset были написаны «под будущий API» и удалены при разборе:
#интерфейс без вызывающего невозможно проверить, и он устаревает раньше, чем понадобится.

SCHEMA_VERSION = 3
#история одного рабочего пространства не растёт бесконечно: предел выбран так, чтобы
#перекрыть рабочий день и не превратить хранилище в журнал без конца
MAX_HISTORY_PER_WORKSPACE = 500


class IncompatibleStoreError(AppError):
    #база, созданная более новой версией: работать с ней вслепую нельзя
    status_code = 500
    code = "incompatible_workspace_store"


class DuplicateSavedQueryError(AppError):
    #имя сохранённого запроса занято: вызывающий должен предложить переименовать,
    #а не показать «внутреннюю ошибку»
    status_code = 409
    code = "saved_query_name_taken"


class SavedQueryNotFoundError(AppError):
    status_code = 404
    code = "saved_query_not_found"


class DuplicateDatasetError(AppError):
    # типизированная ошибка вместо сырого sqlite3.IntegrityError: вызывающий должен уметь
    # отличить гонку двух одинаковых загрузок от настоящего сбоя и разрешить её в пользу первой
    status_code = 409
    code = "dataset_already_exists"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id        TEXT PRIMARY KEY,
    workspace_id      TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    source_file_name  TEXT NOT NULL,
    source_format     TEXT NOT NULL,
    source_bytes      INTEGER NOT NULL,
    source_sha256     TEXT NOT NULL,
    schema_json       TEXT NOT NULL,
    row_count         INTEGER,
    status            TEXT NOT NULL DEFAULT 'ready',
    status_reason     TEXT,
    -- имя, под которым датасет доступен в SQL. Пользователь не передаёт путь: он пишет
    -- FROM продажи, и связывание с артефактом делает backend
    alias             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

-- производный артефакт хранится отдельной записью, а не полем датасета:
-- у него собственный жизненный цикл — он инвалидируется при смене версии конвертера
-- и пересобирается, тогда как карточка датасета и ссылки на неё остаются прежними
CREATE TABLE IF NOT EXISTS derived_artifacts (
    dataset_id        TEXT PRIMARY KEY REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    path              TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    derived_sha256    TEXT NOT NULL,
    source_format     TEXT NOT NULL,
    derived_format    TEXT NOT NULL,
    converter         TEXT NOT NULL,
    converter_version INTEGER NOT NULL,
    row_count         INTEGER NOT NULL,
    warnings_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- поиск готового артефакта по исходнику: повторная загрузка того же файла не должна
-- запускать конвертацию заново, если версия конвертера прежняя
CREATE INDEX IF NOT EXISTS derived_by_source ON derived_artifacts(source_sha256, converter_version);

CREATE INDEX IF NOT EXISTS datasets_by_workspace ON datasets(workspace_id, created_at);

-- история запросов: текст и исход, но не результат. Результат зависит от данных,
-- а данные меняются: показать вчерашние строки под сегодняшним запросом значит соврать
CREATE TABLE IF NOT EXISTS query_runs (
    run_id       TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    sql          TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    elapsed_ms   INTEGER NOT NULL,
    datasets_json TEXT NOT NULL,
    row_count    INTEGER,
    truncated    INTEGER NOT NULL DEFAULT 0,
    truncated_by TEXT,
    -- устойчивый код, а не текст: сообщение движка может содержать пути
    error_code   TEXT
);

CREATE INDEX IF NOT EXISTS query_runs_by_workspace ON query_runs(workspace_id, started_at DESC);

CREATE TABLE IF NOT EXISTS saved_queries (
    saved_query_id TEXT PRIMARY KEY,
    workspace_id   TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    sql            TEXT NOT NULL,
    description    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- имя сохранённого запроса уникально в рабочем пространстве: два «Выручка по месяцам»
-- невозможно различить в списке. Проверка стоит в индексе, а не в коде, потому что
-- две одновременные попытки сохранить одно имя обошли бы проверку «сначала посмотреть»
CREATE UNIQUE INDEX IF NOT EXISTS saved_queries_by_name ON saved_queries(workspace_id, name);

-- отчёт диагностики. Хранится ровно один на датасет: отчёт, посчитанный по другому
-- артефакту или другой версией проверок, не показывается как актуальный, а заменяется.
-- Строк результата здесь нет — только находки и описания предикатов
CREATE TABLE IF NOT EXISTS dataset_health (
    dataset_id          TEXT PRIMARY KEY REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    artifact_fingerprint TEXT NOT NULL,
    checks_version      INTEGER NOT NULL,
    computed_at         TEXT NOT NULL,
    total_rows          INTEGER NOT NULL,
    findings_json       TEXT NOT NULL
);
-- один и тот же файл, загруженный дважды в один workspace, не должен становиться двумя
-- датасетами: содержимое одинаковое, и вторая карточка только запутает
CREATE UNIQUE INDEX IF NOT EXISTS datasets_by_content ON datasets(workspace_id, source_sha256);
-- псевдоним однозначен внутри рабочего пространства: иначе FROM продажи неоднозначно
CREATE UNIQUE INDEX IF NOT EXISTS datasets_by_alias ON datasets(workspace_id, alias);
"""


class WorkspaceStore:
    # это хранилище отвечает за состояние workspace и никогда не трогает содержимое датасетов
    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite-соединение не переносится между потоками, а FastAPI обслуживает запросы пулом,
        # поэтому доступ сериализуется, а соединение открывается на операцию
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL и таймаут на блокировку: без них параллельные запросы получают
        # «database is locked» вместо ожидания
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")

        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # несколько связанных записей обязаны применяться целиком или не применяться вовсе:
        # датасет без своего артефакта выглядел бы готовым к чтению, не имея рабочего файла
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise

            connection.execute("COMMIT")

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            stored = self._stored_version(connection)

            if stored is not None and stored > SCHEMA_VERSION:
                #база новее кода: молча работать с ней нельзя. Незнакомые столбцы и связи
                #будут потеряны при первой же записи, и потеря обнаружится позже и не здесь
                raise IncompatibleStoreError(
                    f"Рабочее пространство создано более новой версией DataArena "
                    f"(схема {stored}, поддерживается {SCHEMA_VERSION}).",
                    details={"found": stored, "supported": SCHEMA_VERSION},
                )

            if stored is not None and stored < SCHEMA_VERSION:
                #миграция выполняется ДО создания схемы: уникальный индекс по псевдониму
                #не создастся, пока у существующих датасетов псевдонима нет
                self._migrate(connection, stored)

            connection.executescript(_SCHEMA)

            if stored is None:
                connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif stored != SCHEMA_VERSION:
                connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    @staticmethod
    def _stored_version(connection: sqlite3.Connection) -> int | None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()

        if table is None:
            return None

        row = connection.execute("SELECT version FROM schema_version").fetchone()

        return None if row is None else int(row["version"])

    def _migrate(self, connection: sqlite3.Connection, stored: int) -> None:
        #миграции идут по одной и в порядке версий: перескок через версию означал бы,
        #что промежуточное преобразование данных не выполнялось
        if stored < 2:  # noqa: PLR2004 - номер версии схемы
            self._migrate_1_to_2(connection)

        #переход 2 → 3 добавляет только новую таблицу, и её создаёт сама схема:
        #существующие данные не преобразуются, поэтому отдельного шага здесь нет

    @staticmethod
    def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
        """Добавляет псевдоним датасета — имя, под которым он доступен в SQL."""
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(datasets)")}

        if "alias" not in columns:
            connection.execute("ALTER TABLE datasets ADD COLUMN alias TEXT NOT NULL DEFAULT ''")

        #псевдонимы проставляются по одному, с проверкой занятости: у существующих датасетов
        #имена могли совпадать, а индекс требует уникальности внутри рабочего пространства
        taken: dict[str, set[str]] = {}

        for row in connection.execute(
            "SELECT dataset_id, workspace_id, name FROM datasets WHERE alias = '' ORDER BY created_at"
        ).fetchall():
            workspace = row["workspace_id"]
            used = taken.setdefault(workspace, {
                existing["alias"]
                for existing in connection.execute(
                    "SELECT alias FROM datasets WHERE workspace_id = ? AND alias <> ''",
                    (workspace,),
                )
            })
            alias = next_free_alias(alias_from_name(row["name"]), used)
            used.add(alias)
            connection.execute(
                "UPDATE datasets SET alias = ? WHERE dataset_id = ?", (alias, row["dataset_id"])
            )

    # ── workspaces ────────────────────────────────────────────────────────────

    def create_workspace(self, workspace_id: str, name: str) -> Workspace:
        ensure_workspace_id(workspace_id)
        created_at = datetime.now(UTC)

        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO workspaces (workspace_id, name, created_at) VALUES (?, ?, ?)",
                (workspace_id, name, created_at.isoformat()),
            )

        return Workspace(workspace_id=workspace_id, name=name, created_at=created_at)


    def get_workspace(self, workspace_id: str) -> Workspace:
        ensure_workspace_id(workspace_id)

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT workspace_id, name, created_at FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()

            if row is None:
                raise WorkspaceNotFoundError(
                    f"Workspace {workspace_id} не найден.", details={"workspace_id": workspace_id}
                )

            dataset_rows = connection.execute(
                "SELECT * FROM datasets WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
            derived_rows = connection.execute(
                """
                SELECT d.* FROM derived_artifacts d
                JOIN datasets s ON s.dataset_id = d.dataset_id
                WHERE s.workspace_id = ?
                """,
                (workspace_id,),
            ).fetchall()

        workspace = self._workspace_from_row(row)
        workspace.datasets = self._attach_derived_rows(dataset_rows, derived_rows)
        return workspace

    def delete_workspace(self, workspace_id: str) -> None:
        ensure_workspace_id(workspace_id)

        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))

            if cursor.rowcount == 0:
                raise WorkspaceNotFoundError(f"Workspace {workspace_id} не найден.")

    # ── datasets ──────────────────────────────────────────────────────────────

    def add_dataset(self, dataset: Dataset) -> Dataset:
        ensure_dataset_id(dataset.dataset_id)
        ensure_workspace_id(dataset.workspace_id)

        with self._lock, self._transaction() as connection:
            workspace_exists = connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (dataset.workspace_id,)
            ).fetchone()

            if workspace_exists is None:
                raise WorkspaceNotFoundError(f"Workspace {dataset.workspace_id} не найден.")

            #псевдоним подбирается здесь, внутри транзакции, а не у вызывающего:
            #две одновременные загрузки файлов с одинаковым именем иначе выбрали бы
            #один и тот же псевдоним, и вторая упала бы на уникальном индексе
            dataset.alias = self._free_alias(connection, dataset.workspace_id, dataset.name)

            try:
                self._insert_dataset(connection, dataset)
            except sqlite3.IntegrityError as error:
                raise DuplicateDatasetError(
                    "Датасет с таким содержимым уже есть в этом workspace.",
                    details={"workspace_id": dataset.workspace_id, "sha256": dataset.source.sha256},
                ) from error

        return dataset

    @staticmethod
    def _free_alias(connection: sqlite3.Connection, workspace_id: str, name: str) -> str:
        taken = {
            row["alias"]
            for row in connection.execute(
                "SELECT alias FROM datasets WHERE workspace_id = ?", (workspace_id,)
            )
        }

        return next_free_alias(alias_from_name(name), taken)

    @staticmethod
    def _insert_dataset(connection: sqlite3.Connection, dataset: Dataset) -> None:
        connection.execute(
            """
                INSERT INTO datasets (
                    dataset_id, workspace_id, name, source_file_name, source_format,
                    source_bytes, source_sha256, schema_json, row_count,
                    status, status_reason, alias, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                dataset.dataset_id,
                dataset.workspace_id,
                dataset.name,
                dataset.source.file_name,
                dataset.source.format,
                dataset.source.bytes,
                dataset.source.sha256,
                json.dumps(dataset.schema.to_dict(), ensure_ascii=False),
                dataset.row_count,
                dataset.status.value,
                dataset.status_reason,
                dataset.alias,
                dataset.created_at.isoformat(),
            ),
        )

        if dataset.derived is not None:
            WorkspaceStore._insert_derived(connection, dataset.dataset_id, dataset.derived)

    def find_dataset_by_content(self, workspace_id: str, sha256: str) -> Dataset | None:
        # повторная загрузка того же файла обязана вернуть существующий датасет,
        # а не создать вторую карточку с тем же содержимым
        ensure_workspace_id(workspace_id)

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM datasets WHERE workspace_id = ? AND source_sha256 = ?",
                (workspace_id, sha256),
            ).fetchone()

        return self._dataset_from_row(row) if row else None

    def get_dataset(self, dataset_id: str) -> Dataset:
        ensure_dataset_id(dataset_id)

        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
            derived_row = connection.execute(
                "SELECT * FROM derived_artifacts WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()

        if row is None:
            raise DatasetNotFoundError(f"Датасет {dataset_id} не найден.", details={"dataset_id": dataset_id})

        dataset = self._dataset_from_row(row)
        dataset.derived = self._derived_from_row(derived_row) if derived_row else None
        return dataset

    def list_datasets(self, workspace_id: str) -> list[Dataset]:
        ensure_workspace_id(workspace_id)

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM datasets WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
            derived_rows = connection.execute(
                """
                SELECT d.* FROM derived_artifacts d
                JOIN datasets s ON s.dataset_id = d.dataset_id
                WHERE s.workspace_id = ?
                """,
                (workspace_id,),
            ).fetchall()

        #без этого нормализованный датасет выглядел бы в списке как ненормализованный,
        #а карточка того же датасета показывала бы обратное
        return self._attach_derived_rows(rows, derived_rows)

    @classmethod
    def _attach_derived_rows(
        cls, dataset_rows: list[sqlite3.Row], derived_rows: list[sqlite3.Row]
    ) -> list[Dataset]:
        by_dataset = {row["dataset_id"]: cls._derived_from_row(row) for row in derived_rows}
        datasets = []

        for row in dataset_rows:
            dataset = cls._dataset_from_row(row)
            dataset.derived = by_dataset.get(dataset.dataset_id)
            datasets.append(dataset)

        return datasets


    def set_row_count(self, dataset_id: str, row_count: int) -> None:
        # точное число строк выясняется отдельно: для потоковых форматов оно стоит полного прохода,
        # и до него честнее показывать «неизвестно», чем врать оценкой
        ensure_dataset_id(dataset_id)

        with self._lock, self._connect() as connection:
            connection.execute("UPDATE datasets SET row_count = ? WHERE dataset_id = ?", (row_count, dataset_id))

    # ── производные артефакты ─────────────────────────────────────────────────

    @staticmethod
    def _insert_derived(connection: sqlite3.Connection, dataset_id: str, artifact: DerivedArtifact) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO derived_artifacts (
                dataset_id, path, source_sha256, derived_sha256, source_format,
                derived_format, converter, converter_version, row_count,
                warnings_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_id,
                artifact.path,
                artifact.source_sha256,
                artifact.derived_sha256,
                artifact.source_format,
                artifact.derived_format,
                artifact.converter,
                artifact.converter_version,
                artifact.row_count,
                json.dumps(list(artifact.warnings), ensure_ascii=False),
                artifact.created_at.isoformat(),
            ),
        )


    def find_reusable_derived(
        self, source_sha256: str, converter: str, converter_version: int
    ) -> DerivedArtifact | None:
        # повторная загрузка того же файла не должна запускать конвертацию заново
        # версия конвертера входит в условие: при её смене прежний артефакт недействителен
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM derived_artifacts
                WHERE source_sha256 = ? AND converter = ? AND converter_version = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_sha256, converter, converter_version),
            ).fetchone()

        return self._derived_from_row(row) if row else None

    def drop_stale_derived(self, converter: str, converter_version: int) -> list[str]:
        # артефакты, собранные прежней логикой, перечисляются и удаляются из учёта:
        # дальше их пересоберёт обычный путь загрузки
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT dataset_id FROM derived_artifacts
                WHERE converter != ? OR converter_version != ?
                """,
                (converter, converter_version),
            ).fetchall()
            stale = [row["dataset_id"] for row in rows]

            if stale:
                connection.execute(
                    """
                    DELETE FROM derived_artifacts
                    WHERE converter != ? OR converter_version != ?
                    """,
                    (converter, converter_version),
                )

        return stale


    # ── преобразование строк ──────────────────────────────────────────────────

    @staticmethod
    def _workspace_from_row(row: sqlite3.Row) -> Workspace:
        return Workspace(
            workspace_id=row["workspace_id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _dataset_from_row(row: sqlite3.Row) -> Dataset:
        payload = json.loads(row["schema_json"])
        columns = tuple(
            ColumnSchema(
                name=column["name"],
                position=column["position"],
                physical_type=column["physical_type"],
                logical_type=LogicalType(column["logical_type"]),
                semantic_type=SemanticType(column["semantic_type"]),
                nullable=column["nullable"],
            )
            for column in payload["columns"]
        )

        return Dataset(
            dataset_id=row["dataset_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            alias=row["alias"],
            source=DatasetSource(
                file_name=row["source_file_name"],
                format=row["source_format"],
                bytes=row["source_bytes"],
                sha256=row["source_sha256"],
            ),
            schema=DatasetSchema(columns=columns),
            row_count=row["row_count"],
            status=DatasetStatus(row["status"]),
            status_reason=row["status_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _derived_from_row(row: sqlite3.Row) -> DerivedArtifact:
        return DerivedArtifact(
            path=row["path"],
            source_sha256=row["source_sha256"],
            derived_sha256=row["derived_sha256"],
            source_format=row["source_format"],
            derived_format=row["derived_format"],
            converter=row["converter"],
            converter_version=row["converter_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            row_count=row["row_count"],
            warnings=tuple(json.loads(row["warnings_json"])),
        )

    # ── история запросов ──────────────────────────────────────────────────────

    def add_query_run(self, run: QueryRun) -> QueryRun:
        """Записывает исход выполнения. Результат не сохраняется — только то, что произошло."""
        ensure_workspace_id(run.workspace_id)

        with self._lock, self._transaction() as connection:
            workspace_exists = connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (run.workspace_id,)
            ).fetchone()

            if workspace_exists is None:
                raise WorkspaceNotFoundError(f"Workspace {run.workspace_id} не найден.")

            connection.execute(
                """
                INSERT INTO query_runs (
                    run_id, workspace_id, sql, status, started_at, elapsed_ms,
                    datasets_json, row_count, truncated, truncated_by, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.workspace_id,
                    run.sql,
                    run.status.value,
                    run.started_at.isoformat(),
                    run.elapsed_ms,
                    json.dumps(list(run.datasets), ensure_ascii=False),
                    run.row_count,
                    int(run.truncated),
                    run.truncated_by,
                    run.error_code,
                ),
            )
            #история не растёт бесконечно: старые записи вытесняются в той же транзакции,
            #иначе рабочее пространство, которым долго пользуются, копит их без предела
            connection.execute(
                """
                DELETE FROM query_runs
                 WHERE workspace_id = ?
                   AND run_id NOT IN (
                       SELECT run_id FROM query_runs
                        WHERE workspace_id = ?
                        ORDER BY started_at DESC, rowid DESC
                        LIMIT ?
                   )
                """,
                (run.workspace_id, run.workspace_id, MAX_HISTORY_PER_WORKSPACE),
            )

        return run

    def list_query_runs(self, workspace_id: str, limit: int = 50) -> list[QueryRun]:
        ensure_workspace_id(workspace_id)
        bounded = max(1, min(limit, MAX_HISTORY_PER_WORKSPACE))

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM query_runs
                 WHERE workspace_id = ?
                 ORDER BY started_at DESC, rowid DESC
                 LIMIT ?
                """,
                (workspace_id, bounded),
            ).fetchall()

        return [self._run_from_row(row) for row in rows]

    # ── сохранённые запросы ───────────────────────────────────────────────────

    def add_saved_query(self, query: SavedQuery) -> SavedQuery:
        ensure_workspace_id(query.workspace_id)
        ensure_saved_query_id(query.saved_query_id)

        with self._lock, self._transaction() as connection:
            workspace_exists = connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (query.workspace_id,)
            ).fetchone()

            if workspace_exists is None:
                raise WorkspaceNotFoundError(f"Workspace {query.workspace_id} не найден.")

            try:
                connection.execute(
                    """
                    INSERT INTO saved_queries (
                        saved_query_id, workspace_id, name, sql, description,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        query.saved_query_id,
                        query.workspace_id,
                        query.name,
                        query.sql,
                        query.description,
                        query.created_at.isoformat(),
                        query.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                #занятое имя — обычная ситуация, а не сбой: вызывающий предложит другое
                raise DuplicateSavedQueryError(
                    f"Запрос с именем «{query.name}» уже сохранён.",
                    details={"name": query.name},
                ) from error

        return query

    def update_saved_query(self, query: SavedQuery) -> SavedQuery:
        """Перезаписывает сохранённый запрос целиком.

        Принимается готовая запись, а не набор полей: так вызывающий не может обновить
        имя, забыв про updated_at, и не может перепутать порядок одинаковых по типу строк.
        """
        workspace_id = query.workspace_id
        saved_query_id = query.saved_query_id
        ensure_workspace_id(workspace_id)
        ensure_saved_query_id(saved_query_id)

        with self._lock, self._transaction() as connection:
            try:
                cursor = connection.execute(
                    """
                    UPDATE saved_queries
                       SET name = ?, sql = ?, description = ?, updated_at = ?
                     WHERE saved_query_id = ? AND workspace_id = ?
                    """,
                    (
                        query.name,
                        query.sql,
                        query.description,
                        query.updated_at.isoformat(),
                        saved_query_id,
                        workspace_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateSavedQueryError(
                    f"Запрос с именем «{query.name}» уже сохранён.", details={"name": query.name}
                ) from error

            if cursor.rowcount == 0:
                #запись удалена между открытием формы и сохранением: это не ошибка сервера,
                #и создавать её заново молча тоже нельзя — пользователь редактировал другое
                raise SavedQueryNotFoundError(
                    "Сохранённый запрос не найден. Возможно, он был удалён.",
                    details={"saved_query_id": saved_query_id},
                )

            row = connection.execute(
                "SELECT * FROM saved_queries WHERE saved_query_id = ?", (saved_query_id,)
            ).fetchone()

        return self._saved_query_from_row(row)

    def get_saved_query(self, workspace_id: str, saved_query_id: str) -> SavedQuery:
        ensure_workspace_id(workspace_id)
        ensure_saved_query_id(saved_query_id)

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM saved_queries WHERE saved_query_id = ? AND workspace_id = ?",
                (saved_query_id, workspace_id),
            ).fetchone()

        if row is None:
            raise SavedQueryNotFoundError(
                "Сохранённый запрос не найден.", details={"saved_query_id": saved_query_id}
            )

        return self._saved_query_from_row(row)

    def list_saved_queries(self, workspace_id: str) -> list[SavedQuery]:
        ensure_workspace_id(workspace_id)

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM saved_queries WHERE workspace_id = ? ORDER BY name",
                (workspace_id,),
            ).fetchall()

        return [self._saved_query_from_row(row) for row in rows]

    def delete_saved_query(self, workspace_id: str, saved_query_id: str) -> None:
        ensure_workspace_id(workspace_id)
        ensure_saved_query_id(saved_query_id)

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM saved_queries WHERE saved_query_id = ? AND workspace_id = ?",
                (saved_query_id, workspace_id),
            )

        if cursor.rowcount == 0:
            #повторное удаление не должно выглядеть как успешное: интерфейс, показавший
            #устаревший список, обязан узнать, что записи уже нет
            raise SavedQueryNotFoundError(
                "Сохранённый запрос не найден.", details={"saved_query_id": saved_query_id}
            )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> QueryRun:
        return QueryRun(
            run_id=row["run_id"],
            workspace_id=row["workspace_id"],
            sql=row["sql"],
            status=QueryStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            elapsed_ms=row["elapsed_ms"],
            datasets=tuple(json.loads(row["datasets_json"])),
            row_count=row["row_count"],
            truncated=bool(row["truncated"]),
            truncated_by=row["truncated_by"],
            error_code=row["error_code"],
        )

    @staticmethod
    def _saved_query_from_row(row: sqlite3.Row) -> SavedQuery:
        return SavedQuery(
            saved_query_id=row["saved_query_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            sql=row["sql"],
            description=row["description"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ── диагностика датасета ──────────────────────────────────────────────────

    def get_health(self, dataset_id: str) -> HealthReport | None:
        """Сохранённый отчёт или None. Проверку на устаревание делает вызывающий."""
        ensure_dataset_id(dataset_id)

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dataset_health WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()

        if row is None:
            return None

        return HealthReport(
            dataset_id=row["dataset_id"],
            artifact_fingerprint=row["artifact_fingerprint"],
            checks_version=row["checks_version"],
            computed_at=datetime.fromisoformat(row["computed_at"]),
            total_rows=row["total_rows"],
            findings=tuple(
                finding_from_storage(item) for item in json.loads(row["findings_json"])
            ),
        )

    def save_health(self, report: HealthReport) -> HealthReport:
        ensure_dataset_id(report.dataset_id)

        with self._lock, self._transaction() as connection:
            dataset_exists = connection.execute(
                "SELECT 1 FROM datasets WHERE dataset_id = ?", (report.dataset_id,)
            ).fetchone()

            if dataset_exists is None:
                raise DatasetNotFoundError(
                    f"Датасет {report.dataset_id} не найден.",
                    details={"dataset_id": report.dataset_id},
                )

            #замена, а не добавление: отчёт по датасету ровно один, и старый
            #по другому артефакту не должен пережить новый
            connection.execute(
                """
                INSERT INTO dataset_health (
                    dataset_id, artifact_fingerprint, checks_version,
                    computed_at, total_rows, findings_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id) DO UPDATE SET
                    artifact_fingerprint = excluded.artifact_fingerprint,
                    checks_version       = excluded.checks_version,
                    computed_at          = excluded.computed_at,
                    total_rows           = excluded.total_rows,
                    findings_json        = excluded.findings_json
                """,
                (
                    report.dataset_id,
                    report.artifact_fingerprint,
                    report.checks_version,
                    report.computed_at.isoformat(),
                    report.total_rows,
                    json.dumps(
                        [finding_to_storage(finding) for finding in report.findings],
                        ensure_ascii=False,
                    ),
                ),
            )

        return report
