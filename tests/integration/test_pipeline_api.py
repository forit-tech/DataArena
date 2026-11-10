"""Конвейер преобразований: путь пользователя целиком, через настоящий API.

Проверяется не «эндпоинт отвечает 200», а обещания раздела: исходный файл не меняется,
шаги воспроизводимы, отмена возвращает прежнее состояние, а перестановка, ломающая
зависимость, отклоняется с объяснением — и всё это переживает перезапуск приложения.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context

MESSY = pl.DataFrame(
    {
        "город": ["Москва ", "москва", "Казань", "н/д", "Москва "],
        "сумма": ["1 200,50", "300", "-", "40", "1 200,50"],
        "статус": ["активен", "активен", "закрыт", "активен", "активен"],
    }
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


@pytest.fixture
def dataset(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/workspaces", json={"name": "Конвейер"}).json()["workspace_id"]
    buffer = io.BytesIO()
    MESSY.write_csv(buffer)
    buffer.seek(0)
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("messy.csv", buffer)}
    )

    return workspace_id, response.json()["dataset_id"]


def put(client: TestClient, workspace_id: str, dataset_id: str, steps: list[dict]):
    return client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline", json={"steps": steps}
    )


def rows(client: TestClient, workspace_id: str, dataset_id: str) -> dict:
    return client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/rows?limit=50"
    ).json()


def test_steps_transform_the_result_without_touching_the_source(
    client: TestClient, dataset: tuple[str, str], tmp_path: Path
) -> None:
    """Главное обещание раздела: исходный файл не меняется ни одним шагом."""
    workspace_id, dataset_id = dataset
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "workspaces").rglob("*")
        if path.is_file() and path.suffix in {".csv", ".parquet"}
    }
    assert before, "в рабочей области нет файлов данных — проверять нечего"

    response = put(
        client,
        workspace_id,
        dataset_id,
        [
            {"step_id": "s1", "kind": "trim_whitespace", "params": {"columns": ["город"]}},
            {
                "step_id": "s2",
                "kind": "normalize_nulls",
                "params": {"columns": ["город", "сумма"]},
            },
            {"step_id": "s3", "kind": "parse_number", "params": {"column": "сумма"}},
            {"step_id": "s4", "kind": "deduplicate", "params": {}},
        ],
    )

    assert response.status_code == 200, response.text
    page = rows(client, workspace_id, dataset_id)

    #строка-дубликат ушла, пометка «н/д» стала пропуском, число разобрано
    assert page["total_rows"] == 4
    assert page["rows"][0]["сумма"] == 1200.5
    assert [row["город"] for row in page["rows"]][:2] == ["Москва", "москва"]
    assert any(row["город"] is None for row in page["rows"])

    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "workspaces").rglob("*")
        if path.is_file() and path.suffix in {".csv", ".parquet"}
    }
    assert after == before, "шаги конвейера изменили файлы данных"


def test_a_reorder_that_breaks_a_dependency_is_refused_with_a_reason(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    """Перестановка, уносящая колонку из-под шага, обязана отклоняться понятно.

    Молчаливое сохранение сломанного конвейера — худший исход: он выглядит сохранённым
    и падает позже, в другом месте.
    """
    workspace_id, dataset_id = dataset
    good = [
        {
            "step_id": "a",
            "kind": "duplicate_column",
            "params": {"column": "город", "to": "копия"},
        },
        {"step_id": "b", "kind": "change_case", "params": {"columns": ["копия"], "case": "upper"}},
    ]

    assert put(client, workspace_id, dataset_id, good).status_code == 200

    swapped = put(client, workspace_id, dataset_id, [good[1], good[0]])

    assert swapped.status_code == 422
    body = swapped.json()["error"]
    assert body["code"] == "pipeline_step_invalid"
    assert "копия" in body["message"], body
    #отклонённая перестановка не должна сохраниться: конвейер остаётся рабочим
    view = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline"
    ).json()
    assert [step["step_id"] for step in view["steps"]] == ["a", "b"]


def test_undo_and_redo_move_along_the_pipeline_history(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    workspace_id, dataset_id = dataset
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline"

    put(client, workspace_id, dataset_id, [])
    put(
        client,
        workspace_id,
        dataset_id,
        [{"step_id": "s1", "kind": "drop_columns", "params": {"columns": ["статус"]}}],
    )

    assert "статус" not in client.get(base).json()["result_columns"]

    undone = client.post(f"{base}/undo")
    assert undone.status_code == 200
    assert "статус" in undone.json()["result_columns"], "отмена не вернула колонку"
    assert undone.json()["can_redo"] is True

    redone = client.post(f"{base}/redo")
    assert "статус" not in redone.json()["result_columns"], "повтор не применил шаг заново"

    #отменять больше нечего — это отказ с объяснением, а не пустой успех
    client.post(f"{base}/undo")
    client.post(f"{base}/undo")
    exhausted = client.post(f"{base}/undo")
    assert exhausted.status_code == 422
    assert exhausted.json()["error"]["message"] == "Отменять нечего."


def test_the_pipeline_and_its_history_survive_a_restart(
    client: TestClient, dataset: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #конвейер живёт в хранилище, а не в браузере: после перезагрузки страницы
    #и после перезапуска backend отмена обязана работать так же
    workspace_id, dataset_id = dataset
    put(
        client,
        workspace_id,
        dataset_id,
        [{"step_id": "s1", "kind": "drop_columns", "params": {"columns": ["статус"]}}],
    )

    config.get_settings.cache_clear()
    context.reset_context()
    restarted = TestClient(create_app())
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline"

    view = restarted.get(base).json()
    assert [step["step_id"] for step in view["steps"]] == ["s1"]
    assert view["can_undo"] is True

    undone = restarted.post(f"{base}/undo").json()
    assert "статус" in undone["result_columns"]


def test_a_destructive_step_reports_its_cost_before_it_is_applied(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    """«Удалит 1 240 строк» обязано быть подсчётом, а не догадкой."""
    workspace_id, dataset_id = dataset
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/impact",
        json={
            "step": {"step_id": "x", "kind": "deduplicate", "params": {}},
            "before": [],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rows_before"] == 5
    assert body["rows_after"] == 4
    assert body["rows_removed"] == 1


def test_a_cast_shows_how_many_values_it_will_turn_into_gaps(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    #приведение типа молча превращает неподходящие значения в пропуски. Цена должна
    #быть видна до применения, иначе пользователь узнает о ней по пустым ячейкам
    workspace_id, dataset_id = dataset
    body = client.post(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/impact",
        json={
            "step": {
                "step_id": "x",
                "kind": "cast_column",
                "params": {"column": "сумма", "to": "float"},
            },
            "before": [],
        },
    ).json()

    #«1 200,50», «-» и «1 200,50» числом не становятся
    assert body["values_lost"] == 3, body
    assert body["rows_removed"] == 0


def test_an_unknown_step_kind_is_refused_at_the_boundary(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    workspace_id, dataset_id = dataset
    response = put(
        client, workspace_id, dataset_id, [{"step_id": "s1", "kind": "rm -rf", "params": {}}]
    )

    assert response.status_code == 422
    assert "rm -rf" in response.json()["error"]["message"]


def test_a_partial_reorder_is_refused_instead_of_dropping_the_rest(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    """Перестановка не должна удалять колонки, которые в ней не перечислены.

    Измерено: `select` частичного списка в Polars молча оставляет только его —
    то есть «поменять местами две колонки» удалило бы все остальные, и заметил бы это
    пользователь уже после выгрузки.
    """
    workspace_id, dataset_id = dataset
    response = put(
        client,
        workspace_id,
        dataset_id,
        [{"step_id": "s1", "kind": "reorder_columns", "params": {"order": ["сумма", "город"]}}],
    )

    assert response.status_code == 422
    assert "все колонки" in response.json()["error"]["message"]

    full = put(
        client,
        workspace_id,
        dataset_id,
        [
            {
                "step_id": "s1",
                "kind": "reorder_columns",
                "params": {"order": ["сумма", "город", "статус"]},
            }
        ],
    )

    assert full.status_code == 200
    assert full.json()["result_columns"] == ["сумма", "город", "статус"]


def test_a_corrupted_pipeline_document_is_a_domain_error_not_a_crash(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    """Повреждённая запись конвейера не должна давать пятисотку со стеком.

    Оборванная запись на диске, ручная правка, сбой носителя — всё это даёт нечитаемый
    документ. Вернуть пустой конвейер молча нельзя: шаги пользователя исчезли бы без
    слова, и он собрал бы их заново поверх испорченной записи.
    """
    from backend.services import context

    workspace_id, dataset_id = dataset
    put(client, workspace_id, dataset_id, [{"step_id": "s1", "kind": "deduplicate", "params": {}}])

    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute(
            "UPDATE dataset_pipelines SET document = ? WHERE dataset_id = ?",
            ("не json", dataset_id),
        )
        connection.commit()

    response = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline")

    assert response.status_code == 422
    assert "повреждён" in response.json()["error"]["message"]
