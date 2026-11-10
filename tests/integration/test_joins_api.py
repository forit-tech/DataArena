"""Сборка датасета: анализ ключей, прогноз роста строк, соединение.

Главное, что здесь проверяется, — прогноз. «Сколько строк получится» обязано быть
посчитанным до выполнения и совпасть с фактом: иначе предупреждение о росте строк
не стоит ничего.
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

CLIENTS = pl.DataFrame(
    {
        "client_id": [1, 2, 3, 4],
        "город": ["Москва", "Казань", "Омск", "Уфа"],
    }
)
#на каждого клиента несколько заказов: связь 1:N, и строк станет больше, чем клиентов
ORDERS = pl.DataFrame(
    {
        "order_id": [10, 11, 12, 13, 14, 15],
        "client_id": [1, 1, 2, 2, 2, 99],
        "сумма": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0],
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


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    buffer.seek(0)

    return client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    ).json()["dataset_id"]


@pytest.fixture
def pair(client: TestClient) -> tuple[str, str, str]:
    workspace_id = client.post("/api/workspaces", json={"name": "Сборка"}).json()["workspace_id"]

    return (
        workspace_id,
        upload(client, workspace_id, CLIENTS, "clients.parquet"),
        upload(client, workspace_id, ORDERS, "orders.parquet"),
    )


def request_body(left: str, right: str, **extra) -> dict:
    return {
        "left_dataset_id": left,
        "right_dataset_id": right,
        "keys": [{"left": "client_id", "right": "client_id"}],
        **extra,
    }


def test_the_predicted_row_count_matches_the_real_one(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    """Прогноз обязан совпасть с фактом — иначе предупреждать им бессмысленно."""
    workspace_id, left, right = pair
    analysis = client.post(
        f"/api/workspaces/{workspace_id}/joins/analyse", json=request_body(left, right)
    ).json()

    #у клиента 1 два заказа, у клиента 2 — три; клиенты 3 и 4 без заказов, заказ 15 — ничей
    assert analysis["cardinality"] == "1:N"
    assert analysis["estimated_rows"] == 5
    assert analysis["unmatched_left_rows"] == 2
    assert analysis["unmatched_right_rows"] == 1

    created = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json=request_body(left, right, name="Клиенты и заказы"),
    )
    assert created.status_code == 201, created.text

    dataset_id = created.json()["dataset_id"]
    page = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/rows?limit=100"
    ).json()

    assert page["total_rows"] == analysis["estimated_rows"], "прогноз разошёлся с фактом"


def test_a_left_join_keeps_rows_without_a_pair(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    workspace_id, left, right = pair
    analysis = client.post(
        f"/api/workspaces/{workspace_id}/joins/analyse",
        json=request_body(left, right, kind="left"),
    ).json()

    #пять пар плюс два клиента без заказов
    assert analysis["estimated_rows"] == 7

    created = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json=request_body(left, right, kind="left", name="Все клиенты"),
    ).json()
    page = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{created['dataset_id']}/rows?limit=100"
    ).json()

    assert page["total_rows"] == 7


def test_row_explosion_is_announced_before_the_join_runs(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    """N:M умножает строки, и это должно быть видно до выполнения."""
    workspace_id, _, _ = pair
    left = upload(
        client,
        workspace_id,
        pl.DataFrame({"ключ": ["a"] * 30, "слева": list(range(30))}),
        "left.parquet",
    )
    right = upload(
        client,
        workspace_id,
        pl.DataFrame({"ключ": ["a"] * 40, "справа": list(range(40))}),
        "right.parquet",
    )

    analysis = client.post(
        f"/api/workspaces/{workspace_id}/joins/analyse",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "ключ", "right": "ключ"}],
        },
    ).json()

    assert analysis["cardinality"] == "N:M"
    assert analysis["estimated_rows"] == 30 * 40
    assert analysis["row_explosion"] is True
    assert any("умножает строки" in text for text in analysis["warnings"]), analysis["warnings"]


def test_an_empty_result_is_refused_instead_of_creating_an_empty_dataset(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    #пустой результат почти всегда означает неверный ключ. Создать пустой датасет молча —
    #значит дать пользователю унести его в следующий этап
    workspace_id, left, right = pair
    response = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "город", "right": "сумма"}],
            "name": "Пустое",
        },
    )

    assert response.status_code == 422
    assert "Ни одна пара не найдена" in response.json()["error"]["message"]


def test_colliding_column_names_are_separated_by_the_chosen_suffix(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    workspace_id, _, _ = pair
    left = upload(
        client,
        workspace_id,
        pl.DataFrame({"id": [1, 2], "имя": ["а", "б"]}),
        "l.parquet",
    )
    right = upload(
        client,
        workspace_id,
        pl.DataFrame({"id": [1, 2], "имя": ["в", "г"]}),
        "r.parquet",
    )

    preview = client.post(
        f"/api/workspaces/{workspace_id}/joins/preview",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "right_suffix": "_из_справочника",
        },
    ).json()

    assert preview["columns"] == ["id", "имя", "имя_из_справочника"]


def test_suggestions_explain_themselves(client: TestClient, pair: tuple[str, str, str]) -> None:
    """Подсказка без объяснения бесполезна: по ней нельзя принять решение."""
    workspace_id, left, right = pair
    body = client.post(
        f"/api/workspaces/{workspace_id}/joins/suggest",
        params={"left_dataset_id": left, "right_dataset_id": right},
    ).json()

    best = body["suggestions"][0]

    assert best["left"] == "client_id"
    assert best["right"] == "client_id"
    assert best["expected_relation"] == "1:N"
    assert best["strength"] in {"высокая", "средняя", "слабая"}
    assert any("Общих значений" in reason for reason in best["reasons"])
    assert any("уникально" in reason for reason in best["reasons"])


def test_the_join_sees_the_result_of_the_pipeline_not_the_raw_file(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    """Пользователь, почистивший данные, соединяет почищенное.

    Если бы соединение читало сырой файл, шаги конвейера молча игнорировались бы —
    и результат не совпадал бы с тем, что человек видит на экране.
    """
    workspace_id, left, right = pair
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{left}/pipeline",
        json={
            "steps": [
                {
                    "step_id": "s1",
                    "kind": "filter_rows",
                    "params": {"column": "client_id", "operator": "eq", "value": "1"},
                }
            ]
        },
    )

    analysis = client.post(
        f"/api/workspaces/{workspace_id}/joins/analyse", json=request_body(left, right)
    ).json()

    #остался один клиент с двумя заказами
    assert analysis["left_rows"] == 1
    assert analysis["estimated_rows"] == 2


def test_incompatible_key_types_are_refused_instead_of_crashing(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    """Анализ предупреждал о разных типах, а выполнение падало необработанной ошибкой.

    Предупредить и уронить — хуже, чем просто отказать: пользователь видит предупреждение,
    нажимает «Создать датасет» и получает пятисотку.
    """
    workspace_id, _, _ = pair
    left = upload(client, workspace_id, pl.DataFrame({"k": [1, 2], "l": ["a", "b"]}), "num.parquet")
    right = upload(
        client, workspace_id, pl.DataFrame({"k": ["1", "2"], "r": ["x", "y"]}), "txt.parquet"
    )
    body = {
        "left_dataset_id": left,
        "right_dataset_id": right,
        "keys": [{"left": "k", "right": "k"}],
        "name": "разные типы",
    }

    response = client.post(f"/api/workspaces/{workspace_id}/joins", json=body)

    assert response.status_code == 422
    assert "Типы ключа не совпадают" in response.json()["error"]["message"]


def test_each_key_reports_how_many_values_both_sides_share(
    client: TestClient, pair: tuple[str, str, str]
) -> None:
    """Регрессия: число общих значений ключа было жёстко равно нулю при реальных совпадениях."""
    workspace_id, left, right = pair
    analysis = client.post(
        f"/api/workspaces/{workspace_id}/joins/analyse", json=request_body(left, right)
    ).json()

    key = analysis["keys"][0]
    #общие идентификаторы клиентов — 1 и 2
    assert key["overlap_values"] == 2
    assert analysis["matched_left_rows"] > 0
