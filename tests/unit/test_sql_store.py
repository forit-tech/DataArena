"""История запросов, сохранённые запросы и миграция схемы.

Миграция проверяется на настоящей базе прежней версии, собранной здесь же по старой
схеме. Проверять миграцию на базе, созданной текущим кодом, бессмысленно: она уже новая,
и преобразование данных при этом не выполняется.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.adapters.storage.workspace_store import (
    MAX_HISTORY_PER_WORKSPACE,
    DuplicateSavedQueryError,
    IncompatibleStoreError,
    SavedQueryNotFoundError,
    WorkspaceStore,
)
from backend.core.errors import WorkspaceNotFoundError
from backend.domain.dataset.identifiers import (
    new_query_run_id,
    new_saved_query_id,
    new_workspace_id,
)
from backend.domain.sql.models import QueryRun, QueryStatus, SavedQuery
from tests.unit.test_workspace_store import make_dataset

#схема первой версии, дословно: миграцию нельзя проверить на базе, созданной новым кодом
SCHEMA_V1 = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE datasets (
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
    created_at        TEXT NOT NULL
);
"""


@pytest.fixture
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "workspace.sqlite")


@pytest.fixture
def workspace_id(store: WorkspaceStore) -> str:
    return store.create_workspace(new_workspace_id(), "Проверка").workspace_id


def make_run(workspace_id: str, **overrides: object) -> QueryRun:
    defaults: dict = {
        "run_id": new_query_run_id(),
        "workspace_id": workspace_id,
        "sql": "SELECT 1",
        "status": QueryStatus.SUCCEEDED,
        "started_at": datetime.now(UTC),
        "elapsed_ms": 12,
        "datasets": ("продажи",),
        "row_count": 1,
    }
    return QueryRun(**{**defaults, **overrides})


def make_saved(workspace_id: str, name: str = "Выручка", sql: str = "SELECT 1") -> SavedQuery:
    now = datetime.now(UTC)
    return SavedQuery(
        saved_query_id=new_saved_query_id(),
        workspace_id=workspace_id,
        name=name,
        sql=sql,
        created_at=now,
        updated_at=now,
    )


# ── миграция ──────────────────────────────────────────────────────────────────


def _make_v1_database(path: Path, rows: list[tuple[str, str, str]]) -> None:
    connection = sqlite3.connect(path)

    try:
        connection.executescript(SCHEMA_V1)
        connection.execute("INSERT INTO schema_version (version) VALUES (1)")
        connection.execute(
            "INSERT INTO workspaces VALUES ('ws_00000000000000aa', 'Старое', '2024-01-01T00:00:00+00:00')"
        )

        for index, (dataset_id, name, created_at) in enumerate(rows):
            connection.execute(
                "INSERT INTO datasets VALUES (?, 'ws_00000000000000aa', ?, ?, 'csv', 10, ?, "
                '\'{"columns": []}\', NULL, \'ready\', NULL, ?)',
                (dataset_id, name, name, f"hash{index}", created_at),
            )

        connection.commit()
    finally:
        connection.close()


def test_a_version_one_database_gains_aliases_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    _make_v1_database(
        path,
        [
            ("ds_0000000000000001", "продажи.csv", "2024-01-01T00:00:00+00:00"),
            ("ds_0000000000000002", "остатки.csv", "2024-01-02T00:00:00+00:00"),
        ],
    )

    store = WorkspaceStore(path)
    datasets = store.list_datasets("ws_00000000000000aa")

    assert [dataset.name for dataset in datasets] == ["продажи.csv", "остатки.csv"]
    assert [dataset.alias for dataset in datasets] == ["продажи", "остатки"]


def test_migration_resolves_colliding_names_into_distinct_aliases(tmp_path: Path) -> None:
    """Старые датасеты могли называться одинаково — уникальный индекс этого не допустит.

    Наивная миграция «взять имя как псевдоним» упала бы здесь на создании индекса,
    и рабочее пространство перестало бы открываться после обновления.
    """
    path = tmp_path / "old.sqlite"
    _make_v1_database(
        path,
        [
            ("ds_0000000000000001", "отчёт.csv", "2024-01-01T00:00:00+00:00"),
            ("ds_0000000000000002", "отчёт.csv", "2024-01-02T00:00:00+00:00"),
            ("ds_0000000000000003", "отчёт.csv", "2024-01-03T00:00:00+00:00"),
        ],
    )

    aliases = [dataset.alias for dataset in WorkspaceStore(path).list_datasets("ws_00000000000000aa")]

    assert aliases == ["отчёт", "отчёт_2", "отчёт_3"]
    assert len(set(aliases)) == 3


def test_migration_runs_once_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    _make_v1_database(path, [("ds_0000000000000001", "продажи.csv", "2024-01-01T00:00:00+00:00")])

    first = WorkspaceStore(path).list_datasets("ws_00000000000000aa")[0].alias
    second = WorkspaceStore(path).list_datasets("ws_00000000000000aa")[0].alias

    assert first == second == "продажи"


def test_a_database_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    """База новее кода не открывается молча.

    Незнакомые столбцы и связи были бы потеряны при первой же записи, и потеря
    обнаружилась бы позже и не здесь.
    """
    path = tmp_path / "future.sqlite"
    WorkspaceStore(path)

    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_version SET version = 999")
    connection.commit()
    connection.close()

    with pytest.raises(IncompatibleStoreError):
        WorkspaceStore(path)


# ── история ───────────────────────────────────────────────────────────────────


def test_a_failed_run_keeps_the_code_and_not_the_engine_message(
    store: WorkspaceStore, workspace_id: str
) -> None:
    #сообщение движка может содержать пути, а история переживает перезапуск и уходит в экспорт
    store.add_query_run(
        make_run(workspace_id, status=QueryStatus.FAILED, row_count=None, error_code="sql_invalid")
    )
    entry = store.list_query_runs(workspace_id)[0]

    assert entry.status is QueryStatus.FAILED
    assert entry.error_code == "sql_invalid"
    assert entry.row_count is None


def test_history_is_newest_first(store: WorkspaceStore, workspace_id: str) -> None:
    base = datetime.now(UTC)

    for index in range(3):
        store.add_query_run(
            make_run(workspace_id, sql=f"SELECT {index}", started_at=base + timedelta(seconds=index))
        )

    assert [entry.sql for entry in store.list_query_runs(workspace_id)] == [
        "SELECT 2",
        "SELECT 1",
        "SELECT 0",
    ]


def test_history_does_not_grow_without_limit(store: WorkspaceStore, workspace_id: str) -> None:
    #рабочее пространство, которым долго пользуются, иначе копит журнал без конца
    base = datetime.now(UTC)

    for index in range(MAX_HISTORY_PER_WORKSPACE + 25):
        store.add_query_run(
            make_run(workspace_id, sql=f"SELECT {index}", started_at=base + timedelta(seconds=index))
        )

    history = store.list_query_runs(workspace_id, limit=MAX_HISTORY_PER_WORKSPACE)

    assert len(history) == MAX_HISTORY_PER_WORKSPACE
    #вытесняются самые старые, а не самые новые
    assert history[0].sql == f"SELECT {MAX_HISTORY_PER_WORKSPACE + 24}"


def test_history_of_another_workspace_is_not_visible(store: WorkspaceStore, workspace_id: str) -> None:
    other = store.create_workspace(new_workspace_id(), "Другое").workspace_id
    store.add_query_run(make_run(workspace_id, sql="SELECT 'моё'"))
    store.add_query_run(make_run(other, sql="SELECT 'чужое'"))

    assert [entry.sql for entry in store.list_query_runs(workspace_id)] == ["SELECT 'моё'"]


def test_history_of_a_missing_workspace_is_refused(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        store.add_query_run(make_run(new_workspace_id()))


def test_deleting_a_workspace_removes_its_history(store: WorkspaceStore, workspace_id: str) -> None:
    store.add_query_run(make_run(workspace_id))
    store.delete_workspace(workspace_id)

    assert store.list_query_runs(workspace_id) == []


# ── сохранённые запросы ───────────────────────────────────────────────────────


def test_a_saved_query_round_trips(store: WorkspaceStore, workspace_id: str) -> None:
    saved = store.add_saved_query(make_saved(workspace_id, sql="SELECT * FROM продажи"))
    read = store.get_saved_query(workspace_id, saved.saved_query_id)

    assert read.name == "Выручка"
    assert read.sql == "SELECT * FROM продажи"


def test_the_same_name_in_another_workspace_is_allowed(
    store: WorkspaceStore, workspace_id: str
) -> None:
    other = store.create_workspace(new_workspace_id(), "Другое").workspace_id
    store.add_saved_query(make_saved(workspace_id, name="Выручка"))
    store.add_saved_query(make_saved(other, name="Выручка"))

    assert len(store.list_saved_queries(workspace_id)) == 1


def test_renaming_onto_a_taken_name_is_refused(store: WorkspaceStore, workspace_id: str) -> None:
    store.add_saved_query(make_saved(workspace_id, name="Выручка"))
    second = store.add_saved_query(make_saved(workspace_id, name="Остатки"))

    with pytest.raises(DuplicateSavedQueryError):
        store.update_saved_query(
            SavedQuery(
                saved_query_id=second.saved_query_id,
                workspace_id=workspace_id,
                name="Выручка",
                sql=second.sql,
                description=None,
                created_at=second.created_at,
                updated_at=datetime.now(UTC),
            )
        )


def test_a_saved_query_of_another_workspace_is_invisible(
    store: WorkspaceStore, workspace_id: str
) -> None:
    #чужая запись обязана выглядеть как несуществующая, иначе перебор идентификаторов
    #рассказывал бы о содержимом соседнего рабочего пространства
    other = store.create_workspace(new_workspace_id(), "Другое").workspace_id
    saved = store.add_saved_query(make_saved(other))

    with pytest.raises(SavedQueryNotFoundError):
        store.get_saved_query(workspace_id, saved.saved_query_id)

    with pytest.raises(SavedQueryNotFoundError):
        store.delete_saved_query(workspace_id, saved.saved_query_id)


def test_long_and_unicode_text_survives_storage(store: WorkspaceStore, workspace_id: str) -> None:
    #запрос на десять тысяч символов с эмодзи и переносами обязан вернуться байт в байт
    sql = "SELECT '🙂 привет', -- комментарий\n" + "  1 +\n" * 2000 + "  1"
    saved = store.add_saved_query(make_saved(workspace_id, name="Длинный 🙂", sql=sql))

    assert store.get_saved_query(workspace_id, saved.saved_query_id).sql == sql


def test_deleting_a_workspace_removes_its_saved_queries(
    store: WorkspaceStore, workspace_id: str
) -> None:
    store.add_saved_query(make_saved(workspace_id))
    store.delete_workspace(workspace_id)

    assert store.list_saved_queries(workspace_id) == []


# ── псевдонимы датасетов ──────────────────────────────────────────────────────


def test_datasets_with_the_same_name_get_distinct_aliases(
    store: WorkspaceStore, workspace_id: str
) -> None:
    first = store.add_dataset(make_dataset(workspace_id, name="отчёт.csv", sha256="a" * 64))
    second = store.add_dataset(make_dataset(workspace_id, name="отчёт.csv", sha256="b" * 64))

    assert first.alias == "отчёт"
    assert second.alias == "отчёт_2"


def test_alias_is_assigned_by_the_store_not_by_the_caller(
    store: WorkspaceStore, workspace_id: str
) -> None:
    #вызывающий не знает, какие имена заняты, и не защищён от гонки: подбор принадлежит хранилищу
    dataset = store.add_dataset(make_dataset(workspace_id, name="продажи.csv", sha256="c" * 64))

    assert dataset.alias == "продажи"
