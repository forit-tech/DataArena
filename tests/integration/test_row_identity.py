"""Устойчивый номер строки.

Номер относится к файлу, а не к странице. Разница видна сразу после первой сортировки:
«третья сверху» — каждый раз другая строка, а номер в файле остаётся тем же.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


@pytest.fixture
def workspace_id(client: TestClient) -> str:
    return client.post("/api/workspaces", json={"name": "Строки"}).json()["workspace_id"]


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    buffer.seek(0)
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    )
    assert response.status_code == 201, response.text
    return response.json()["dataset_id"]


@pytest.fixture
def dataset_id(client: TestClient, workspace_id: str) -> str:
    frame = pl.DataFrame(
        {
            "id": list(range(100)),
            "сумма": [float((index * 37) % 100) for index in range(100)],
        }
    )
    return upload(client, workspace_id, frame, "строки.parquet")


def rows(client: TestClient, workspace_id: str, dataset_id: str, **params: object) -> dict:
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/rows", params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_ordinal_matches_the_position_in_the_file(
    client: TestClient, workspace_id: str, dataset_id: str
) -> None:
    page = rows(client, workspace_id, dataset_id, limit=5)

    assert page["row_ordinals"] == [0, 1, 2, 3, 4]
    assert [row["id"] for row in page["rows"]] == [0, 1, 2, 3, 4]


def test_the_ordinal_is_the_same_between_requests(
    client: TestClient, workspace_id: str, dataset_id: str
) -> None:
    #порядок чтения артефакта детерминирован: без этого номер не был бы идентичностью
    first = rows(client, workspace_id, dataset_id, limit=10)
    second = rows(client, workspace_id, dataset_id, limit=10)

    assert first["row_ordinals"] == second["row_ordinals"]


def test_the_ordinal_follows_the_row_through_sorting(
    client: TestClient, workspace_id: str, dataset_id: str
) -> None:
    """Главное свойство: строка узнаётся после сортировки.

    Номер на странице после сортировки означал бы другую строку — именно так «строка 143»
    из находки перестала бы указывать на то, о чём говорила находка.
    """
    plain = rows(client, workspace_id, dataset_id, limit=100)
    by_id = dict(zip(plain["row_ordinals"], [row["id"] for row in plain["rows"]], strict=True))

    sorted_page = rows(client, workspace_id, dataset_id, sort="сумма:desc", limit=5)

    for ordinal, row in zip(sorted_page["row_ordinals"], sorted_page["rows"], strict=True):
        assert by_id[ordinal] == row["id"], "номер указывает на другую строку"

    #и сама сортировка действительно переставила строки
    assert sorted_page["row_ordinals"] != [0, 1, 2, 3, 4]


def test_the_ordinal_survives_paging(
    client: TestClient, workspace_id: str, dataset_id: str
) -> None:
    second_page = rows(client, workspace_id, dataset_id, offset=10, limit=5)

    assert second_page["row_ordinals"] == [10, 11, 12, 13, 14]


def test_the_ordinal_is_of_the_file_not_of_the_filtered_result(
    client: TestClient, workspace_id: str, dataset_id: str
) -> None:
    #внутри отфильтрованной выдачи номера остаются номерами файла, иначе они
    #перестали бы отвечать на вопрос «какая это строка датасета»
    page = rows(client, workspace_id, dataset_id, filter="id:gt:50", limit=3)

    assert page["row_ordinals"] == [51, 52, 53]


def test_a_column_named_like_the_internal_one_does_not_break_anything(
    client: TestClient, workspace_id: str
) -> None:
    """Датасет с колонкой «__row__» обязан работать.

    Фиксированное имя служебной колонки однажды встретилось бы в данных: тогда номер
    затёр бы колонку пользователя, а полные дубликаты перестали бы находиться, потому
    что в набор колонок попал бы уникальный номер.
    """
    frame = pl.DataFrame(
        {
            "__row__": ["свои данные"] * 40,
            "__row_1__": list(range(40)),
            "значение": [index % 4 for index in range(40)],
        }
    )
    dataset_id = upload(client, workspace_id, frame, "коллизия.parquet")

    page = rows(client, workspace_id, dataset_id, limit=3)

    assert page["columns"] == ["__row__", "__row_1__", "значение"]
    assert page["rows"][0]["__row__"] == "свои данные"
    assert page["row_ordinals"] == [0, 1, 2]

    #и диагностика на таком датасете тоже работает
    health = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/health")

    assert health.status_code == 200
    assert health.json()["total_rows"] == 40


def test_duplicates_are_still_found_when_the_ordinal_is_present(
    client: TestClient, workspace_id: str
) -> None:
    """Служебная колонка не должна делать каждую строку уникальной.

    Проверено измерением: при подсчёте дубликатов через «все колонки плана» номер
    попадал в набор, и дубликатов не находилось никогда.
    """
    frame = pl.DataFrame({"a": [1, 1, 2, 3] * 10, "b": ["x", "x", "y", "z"] * 10})
    dataset_id = upload(client, workspace_id, frame, "дубликаты.parquet")

    report = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/health").json()
    duplicates = next(item for item in report["findings"] if item["code"] == "duplicate_rows")

    assert duplicates["affected_rows"] == 40

    page = rows(client, workspace_id, dataset_id, finding=duplicates["finding_id"], limit=5)

    assert page["total_rows"] == 40
    #номера настоящие, а не выдуманные заново для подмножества
    assert page["row_ordinals"] == [0, 1, 2, 3, 4]
