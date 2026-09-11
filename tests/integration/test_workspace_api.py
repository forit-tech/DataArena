"""Сквозной путь: загрузка → карточка → таблица → сортировка, фильтр, страница.

Тесты идут через HTTP, а не через сервисы напрямую: проверяется тот путь, которым
пользуется интерфейс, вместе с разбором параметров, схемами ответа и обработкой ошибок.
"""

from __future__ import annotations

import datetime as dt
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
    #каждый тест получает собственный каталог workspace: без сброса кеша второй тест
    #работал бы с хранилищем первого
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


@pytest.fixture
def workspace_id(client: TestClient) -> str:
    response = client.post("/api/workspaces", json={"name": "Проверка"})
    assert response.status_code == 201
    return response.json()["workspace_id"]


def sample_frame() -> pl.DataFrame:
    #в таблице намеренно есть пропуски, отрицательные значения, кириллица и даты:
    #на них ломаются сортировка, фильтры и сериализация
    return pl.DataFrame(
        {
            "id": list(range(1, 21)),
            "город": ["Москва", "Казань", None, "Омск", "Тверь"] * 4,
            "сумма": [100.5, -20.0, None, 3000.0, 15.25] * 4,
            "дата": [dt.date(2024, 1, day) for day in range(1, 21)],
            "активен": [True, False, None, True, False] * 4,
        }
    )


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> dict:
    buffer = io.BytesIO()
    suffix = Path(name).suffix

    if suffix == ".csv":
        buffer.write(frame.write_csv().encode("utf-8"))
    elif suffix == ".parquet":
        frame.write_parquet(buffer)
    elif suffix == ".json":
        buffer.write(frame.write_json().encode("utf-8"))
    else:
        raise AssertionError(f"тест не умеет писать {suffix}")

    buffer.seek(0)
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── сквозной сценарий ─────────────────────────────────────────────────────────


def test_upload_then_read_the_table(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "cities.csv")

    assert dataset["column_count"] == 5
    assert dataset["status"] == "ready"
    assert dataset["original_format"] == "csv"
    assert dataset["working_format"] == "csv", "CSV читается лениво и не нормализуется"
    assert dataset["normalized"] is False

    rows = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?limit=5"
    ).json()

    assert len(rows["rows"]) == 5
    assert rows["total_rows"] == 20
    assert rows["total_is_exact"] is True
    assert rows["columns"] == ["id", "город", "сумма", "дата", "активен"]


def test_json_is_normalised_but_source_format_is_still_reported(
    client: TestClient, workspace_id: str
) -> None:
    #пользователь загрузил JSON и должен видеть JSON как исходный формат,
    #даже если backend внутри читает Parquet
    dataset = upload(client, workspace_id, sample_frame(), "cities.json")

    assert dataset["original_format"] == "json"
    assert dataset["working_format"] == "parquet"
    assert dataset["normalized"] is True
    assert dataset["conversion"]["converter_version"] >= 1
    assert dataset["conversion"]["warnings"], "JSON теряет даты — об этом обязано быть сказано"


def test_uploading_the_same_file_twice_returns_the_same_dataset(
    client: TestClient, workspace_id: str
) -> None:
    frame = sample_frame()
    first = upload(client, workspace_id, frame, "same.csv")
    second = upload(client, workspace_id, frame, "same.csv")

    assert first["dataset_id"] == second["dataset_id"]
    assert len(client.get(f"/api/workspaces/{workspace_id}/datasets").json()["datasets"]) == 1


def test_dataset_survives_a_restart_of_the_application(
    client: TestClient, workspace_id: str
) -> None:
    #состояние живёт на сервере, а не в браузере: это главное отличие от предшественника
    dataset = upload(client, workspace_id, sample_frame(), "persist.csv")
    context.reset_context()

    restarted = TestClient(create_app())
    response = restarted.get(f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}")

    assert response.status_code == 200
    assert response.json()["dataset_id"] == dataset["dataset_id"]

    rows = restarted.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?limit=3"
    )
    assert rows.status_code == 200
    assert len(rows.json()["rows"]) == 3


# ── пагинация, сортировка, фильтры ────────────────────────────────────────────


def test_pagination_returns_disjoint_pages(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "pages.csv")
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"

    first = client.get(f"{base}?offset=0&limit=5&sort=id:asc").json()["rows"]
    second = client.get(f"{base}?offset=5&limit=5&sort=id:asc").json()["rows"]

    assert [row["id"] for row in first] == [1, 2, 3, 4, 5]
    assert [row["id"] for row in second] == [6, 7, 8, 9, 10]


def test_page_size_is_capped_regardless_of_the_request(
    client: TestClient, workspace_id: str
) -> None:
    #миллион строк в браузер не отправляется ни при каком значении параметра
    dataset = upload(client, workspace_id, sample_frame(), "cap.csv")
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?limit=999999"
    )

    assert response.status_code == 200
    assert response.json()["limit"] <= 1000


def test_sorting_puts_nulls_last_in_both_directions(
    client: TestClient, workspace_id: str
) -> None:
    #иначе при смене направления пропуски прыгают с одного конца на другой,
    #и пользователь считает, что данные изменились
    dataset = upload(client, workspace_id, sample_frame(), "nulls.csv")
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"

    ascending = client.get(f"{base}?sort=сумма:asc&limit=20").json()["rows"]
    descending = client.get(f"{base}?sort=сумма:desc&limit=20").json()["rows"]

    assert ascending[-1]["сумма"] is None
    assert descending[-1]["сумма"] is None
    assert ascending[0]["сумма"] == -20.0
    assert descending[0]["сумма"] == 3000.0


def test_filter_by_number_compares_as_number_not_as_text(
    client: TestClient, workspace_id: str
) -> None:
    #без приведения к типу колонки сравнение «сумма > 100» отработало бы по строкам
    dataset = upload(client, workspace_id, sample_frame(), "filter.csv")
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"
        "?filter=сумма:gt:100&limit=50"
    ).json()

    values = [row["сумма"] for row in response["rows"]]
    assert values
    assert all(value > 100 for value in values)


def test_null_filters_work_in_both_directions(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "nullfilter.csv")
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"

    empty = client.get(f"{base}?filter=город:is_null&limit=50").json()
    filled = client.get(f"{base}?filter=город:is_not_null&limit=50").json()

    assert empty["total_rows"] == 4
    assert filled["total_rows"] == 16
    assert all(row["город"] is None for row in empty["rows"])


def test_search_finds_values_in_any_column(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "search.csv")
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?search=Казань"
    ).json()

    assert response["total_rows"] == 4
    assert all(row["город"] == "Казань" for row in response["rows"])


def test_search_treats_the_needle_literally(client: TestClient, workspace_id: str) -> None:
    """Строка поиска не является регулярным выражением.

    Проверяется шаблоном, который КАК РЕГУЛЯРНОЕ ВЫРАЖЕНИЕ нашёл бы строки, а буквально —
    нет. Прежняя версия теста искала «(a+)+b» и ожидала ноль совпадений, но такой шаблон
    не находит ничего и в режиме регулярного выражения: тест оставался зелёным при
    отключённом literal. Вскрыто мутационной проверкой.
    """
    dataset = upload(client, workspace_id, sample_frame(), "regex.csv")
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"

    #«М.сква» как регулярное выражение совпадает с «Москва», буквально — нет
    as_regex = client.get(base, params={"search": "М.сква"})
    literally = client.get(base, params={"search": "Москва"})

    assert as_regex.status_code == 200
    assert as_regex.json()["total_rows"] == 0, "строка поиска истолкована как регулярное выражение"
    assert literally.json()["total_rows"] == 4, "буквальное совпадение обязано находиться"

    #и отдельно — шаблон, дорогой для перебора: он не должен ни находиться, ни исполняться
    pathological = client.get(base, params={"search": "(a+)+b"})
    assert pathological.status_code == 200
    assert pathological.json()["total_rows"] == 0


def test_empty_result_is_a_valid_page_not_an_error(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "empty.csv")
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?search=нетзначения"
    )

    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["total_rows"] == 0
    assert response.json()["columns"], "колонки известны даже без строк"


def test_offset_past_the_end_returns_an_empty_page(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "past.csv")
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?offset=100000"
    )

    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["total_rows"] == 20


# ── статистика по колонке ─────────────────────────────────────────────────────


def test_numeric_column_statistics(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "stats.csv")
    stats = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/columns/сумма"
    ).json()

    assert stats["null_count"] == 4
    assert stats["numeric"]["min"] == -20.0
    assert stats["numeric"]["max"] == 3000.0
    assert stats["histogram"] is not None
    assert stats["top_values"] is None, "для числовой колонки показывается распределение"


def test_categorical_column_statistics(client: TestClient, workspace_id: str) -> None:
    dataset = upload(client, workspace_id, sample_frame(), "cat.csv")
    stats = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/columns/город"
    ).json()

    assert stats["unique_count"] == 5
    assert stats["top_values"]
    assert sum(item["count"] for item in stats["top_values"]) == 16
