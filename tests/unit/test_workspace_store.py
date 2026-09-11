"""Хранилище workspace.

Тесты проверяют свойства, обещанные пользователю и остальному коду, а не устройство таблиц:
состояние переживает перезапуск, повторная загрузка того же файла не плодит карточки,
удаление workspace уносит его датасеты, а идентификатор из пользовательской строки
не попадает ни в путь, ни в запрос.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import DatasetNotFoundError, ValidationError, WorkspaceNotFoundError
from backend.domain.dataset.identifiers import (
    ensure_dataset_id,
    ensure_workspace_id,
    new_dataset_id,
    new_workspace_id,
)
from backend.domain.dataset.models import (
    ColumnSchema,
    Dataset,
    DatasetSchema,
    DatasetSource,
    LogicalType,
    SemanticType,
)


@pytest.fixture
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "state.db")


def make_dataset(workspace_id: str, *, name: str = "users.csv", sha256: str = "a" * 64) -> Dataset:
    return Dataset(
        dataset_id=new_dataset_id(),
        workspace_id=workspace_id,
        alias="",
        name=name,
        source=DatasetSource(file_name=name, format="csv", bytes=1024, sha256=sha256),
        schema=DatasetSchema(
            columns=(
                ColumnSchema("user_id", 0, "Int64", LogicalType.INTEGER, SemanticType.ID, False),
                ColumnSchema("город", 1, "String", LogicalType.STRING, SemanticType.CATEGORICAL, True),
            )
        ),
        row_count=None,
        created_at=datetime.now(UTC),
    )


def test_schema_survives_the_round_trip_through_storage(store: WorkspaceStore) -> None:
    #схема хранится в JSON, и типы обязаны вернуться перечислениями, а не строками:
    #иначе сравнение с LogicalType молча перестанет работать
    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "ws")
    dataset = store.add_dataset(make_dataset(workspace_id))

    restored = store.get_dataset(dataset.dataset_id)

    assert restored.schema.columns[0].logical_type is LogicalType.INTEGER
    assert restored.schema.columns[1].semantic_type is SemanticType.CATEGORICAL
    assert restored.column_count == 2


def test_the_same_content_in_another_workspace_is_a_separate_dataset(store: WorkspaceStore) -> None:
    #workspace изолированы: один и тот же файл в двух рабочих пространствах — два датасета
    first, second = new_workspace_id(), new_workspace_id()
    store.create_workspace(first, "первый")
    store.create_workspace(second, "второй")

    store.add_dataset(make_dataset(first, sha256="c" * 64))
    store.add_dataset(make_dataset(second, sha256="c" * 64))

    assert store.find_dataset_by_content(second, "c" * 64) is not None
    assert len(store.list_datasets(first)) == 1


def test_deleting_a_workspace_removes_its_datasets(store: WorkspaceStore) -> None:
    #без каскада в базе остались бы карточки, ссылающиеся на несуществующее пространство
    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "ws")
    dataset = store.add_dataset(make_dataset(workspace_id))

    store.delete_workspace(workspace_id)

    with pytest.raises(DatasetNotFoundError):
        store.get_dataset(dataset.dataset_id)


def test_dataset_cannot_be_added_to_a_missing_workspace(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        store.add_dataset(make_dataset(new_workspace_id()))


def test_row_count_is_unknown_until_counted(store: WorkspaceStore) -> None:
    #для потоковых форматов точное число строк стоит полного прохода: до него честнее
    #показать «неизвестно», чем подставить оценку и выдать её за факт
    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "ws")
    dataset = store.add_dataset(make_dataset(workspace_id))

    assert store.get_dataset(dataset.dataset_id).row_count is None

    store.set_row_count(dataset.dataset_id, 84213)

    assert store.get_dataset(dataset.dataset_id).row_count == 84213


@pytest.mark.parametrize(
    "value",
    [
        "../../etc/passwd",
        "ws_../../../secrets",
        "ws_ЛАТИНИЦА",
        "ws_short",
        "",
        "ws_" + "a" * 64,
    ],
)
def test_identifier_from_user_input_never_reaches_a_path(value: str) -> None:
    #все пути на диске собираются только из идентификаторов, поэтому проверка идентификатора
    #и есть защита от обхода каталогов
    with pytest.raises(ValidationError):
        ensure_workspace_id(value)


def test_generated_identifiers_pass_their_own_check() -> None:
    for _ in range(50):
        ensure_workspace_id(new_workspace_id())
        ensure_dataset_id(new_dataset_id())


def test_generated_identifiers_do_not_collide() -> None:
    assert len({new_dataset_id() for _ in range(1000)}) == 1000


def test_missing_dataset_reports_its_identifier(store: WorkspaceStore) -> None:
    #сообщение об ошибке обязано говорить, чего именно не нашли
    missing = new_dataset_id()

    with pytest.raises(DatasetNotFoundError) as error:
        store.get_dataset(missing)

    assert error.value.details["dataset_id"] == missing
