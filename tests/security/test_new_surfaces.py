"""Безопасность разделов, появившихся в первом релизе.

Проверяются не «все возможные атаки», а те классы, которые здесь действительно есть:
чужое рабочее пространство, имя файла в заголовке ответа, произвольный код в параметрах
шага и запись за пределы рабочей области.
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

FRAME = pl.DataFrame({"id": [1, 2, 3], "значение": ["а", "б", "в"]})


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


def upload(client: TestClient, workspace_id: str, name: str = "d.parquet") -> str:
    buffer = io.BytesIO()
    FRAME.write_parquet(buffer)
    buffer.seek(0)

    return client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    ).json()["dataset_id"]


def test_a_dataset_of_another_workspace_looks_missing_everywhere(client: TestClient) -> None:
    """Чужой датасет обязан выглядеть несуществующим, а не «запрещённым».

    Иначе перебор идентификаторов сообщал бы о чужих данных самим фактом отказа.
    """
    mine = client.post("/api/workspaces", json={"name": "моё"}).json()["workspace_id"]
    other = client.post("/api/workspaces", json={"name": "чужое"}).json()["workspace_id"]
    stranger = upload(client, other)

    for path in (
        f"/api/workspaces/{mine}/datasets/{stranger}/pipeline",
        f"/api/workspaces/{mine}/datasets/{stranger}/pipeline/rows",
        f"/api/workspaces/{mine}/datasets/{stranger}/export?format=csv",
        f"/api/workspaces/{mine}/datasets/{stranger}/package",
        f"/api/workspaces/{mine}/datasets/{stranger}/health",
    ):
        response = client.get(path)

        assert response.status_code == 404, f"{path}: {response.status_code}"
        assert response.json()["error"]["code"] == "dataset_not_found"


def test_a_join_cannot_reach_across_workspaces(client: TestClient) -> None:
    mine = client.post("/api/workspaces", json={"name": "моё"}).json()["workspace_id"]
    other = client.post("/api/workspaces", json={"name": "чужое"}).json()["workspace_id"]

    response = client.post(
        f"/api/workspaces/{mine}/joins/analyse",
        json={
            "left_dataset_id": upload(client, mine),
            "right_dataset_id": upload(client, other, "чужой.parquet"),
            "keys": [{"left": "id", "right": "id"}],
        },
    )

    assert response.status_code == 404


def test_a_dataset_name_cannot_escape_into_the_response_header(client: TestClient) -> None:
    #имя файла приходит от пользователя и попадает в Content-Disposition
    workspace_id = client.post("/api/workspaces", json={"name": "имена"}).json()["workspace_id"]
    dataset_id = upload(client, workspace_id, "..\\..\\секрет.parquet")

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/export?format=csv"
    )

    disposition = response.headers.get("content-disposition", "")

    assert response.status_code == 200
    assert ".." not in disposition
    assert "\\" not in disposition
    assert "/" not in disposition.split("filename")[-1]


def test_step_parameters_are_data_and_never_code(client: TestClient) -> None:
    """Шаг — операция из закрытого перечня, а не выражение пользователя.

    Параметр с попыткой подставить выражение обязан остаться значением: он либо
    не найдёт колонку, либо будет сравниваться как текст.
    """
    workspace_id = client.post("/api/workspaces", json={"name": "шаги"}).json()["workspace_id"]
    dataset_id = upload(client, workspace_id)

    hostile = client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {
                    "step_id": "s1",
                    "kind": "filter_rows",
                    "params": {
                        "column": "значение",
                        "operator": "contains",
                        "value": "__import__('os').system('echo x')",
                    },
                }
            ]
        },
    )

    assert hostile.status_code == 200, hostile.text

    page = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/rows"
    ).json()

    #значение искалось буквально и ничего не нашло: выполнять его никто не пытался
    assert page["total_rows"] == 0


def test_a_column_name_from_the_user_cannot_become_an_expression(client: TestClient) -> None:
    workspace_id = client.post("/api/workspaces", json={"name": "колонки"}).json()["workspace_id"]
    dataset_id = upload(client, workspace_id)

    response = client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {
                    "step_id": "s1",
                    "kind": "drop_columns",
                    "params": {"columns": ["id; DROP TABLE datasets"]},
                }
            ]
        },
    )

    assert response.status_code == 422
    assert "Нет колонок" in response.json()["error"]["message"]


def test_the_package_stays_inside_the_temporary_directory(client: TestClient) -> None:
    #имя датасета участвует в имени файла пакета: оно не должно уводить запись
    workspace_id = client.post("/api/workspaces", json={"name": "пакет"}).json()["workspace_id"]
    dataset_id = upload(client, workspace_id, "../../побег.parquet")

    response = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/package")

    assert response.status_code == 200
    assert ".." not in response.headers.get("content-disposition", "")
