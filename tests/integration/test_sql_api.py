"""Окно SQL через HTTP: выполнение, история, сохранённые запросы, отмена, гонки.

Тесты идут тем же путём, что и интерфейс, вместе с разбором тела запроса, схемами
ответа и обработкой ошибок. Проверять сервис напрямую здесь недостаточно: половина
интересного находится именно в переводе доменных ошибок в коды ответа.
"""

from __future__ import annotations

import io
import threading
import time
import uuid
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
    response = client.post("/api/workspaces", json={"name": "SQL"})
    assert response.status_code == 201
    return response.json()["workspace_id"]


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> dict:
    buffer = io.BytesIO()

    if name.endswith(".csv"):
        buffer.write(frame.write_csv().encode("utf-8"))
    else:
        frame.write_parquet(buffer)

    buffer.seek(0)
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def datasets(client: TestClient, workspace_id: str) -> dict[str, dict]:
    orders = pl.DataFrame(
        {
            "id": list(range(1, 21)),
            "город": ["Москва", "Казань", "Омск", "Тверь"] * 5,
            "сумма": [float(100 * index) for index in range(1, 21)],
        }
    )
    people = pl.DataFrame({"id": list(range(1, 21)), "имя": [f"клиент {n}" for n in range(1, 21)]})

    return {
        "продажи": upload(client, workspace_id, orders, "продажи.csv"),
        "клиенты": upload(client, workspace_id, people, "клиенты.parquet"),
    }


def sql(client: TestClient, workspace_id: str, text: str, **extra: object) -> tuple[int, dict]:
    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries", json={"sql": text, **extra}
    )
    return response.status_code, response.json()


# ── связывание датасетов ──────────────────────────────────────────────────────


def test_available_aliases_are_listed_for_the_editor(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #имя датасета в SQL не должно быть загадкой: список показывается рядом с редактором
    response = client.get(f"/api/workspaces/{workspace_id}/sql/datasets").json()
    aliases = {item["alias"]: item for item in response["datasets"]}

    assert set(aliases) == {"продажи", "клиенты"}
    assert aliases["продажи"]["columns"] == ["id", "город", "сумма"]
    assert aliases["продажи"]["queryable"] is True


def test_a_query_reads_the_dataset_by_its_alias(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    status, body = sql(client, workspace_id, "SELECT count(*) AS n FROM продажи")

    assert status == 200, body
    assert body["result"]["rows"][0]["n"] == 20
    assert body["run"]["datasets"] == ["продажи"]
    assert body["run"]["status"] == "succeeded"


def test_alias_matching_ignores_case(client: TestClient, workspace_id: str, datasets: dict) -> None:
    #DuckDB нечувствителен к регистру идентификаторов, и связывание обязано вести себя так же
    status, body = sql(client, workspace_id, "SELECT count(*) AS n FROM ПРОДАЖИ")

    assert status == 200, body
    assert body["result"]["rows"][0]["n"] == 20


def test_a_join_of_two_datasets_works(client: TestClient, workspace_id: str, datasets: dict) -> None:
    status, body = sql(
        client,
        workspace_id,
        "SELECT p.город, count(*) AS n FROM продажи p JOIN клиенты k ON p.id = k.id "
        "GROUP BY p.город ORDER BY p.город",
    )

    assert status == 200, body
    assert [row["город"] for row in body["result"]["rows"]] == ["Казань", "Москва", "Омск", "Тверь"]
    assert sorted(body["run"]["datasets"]) == ["клиенты", "продажи"]


def test_an_unknown_alias_lists_what_is_available(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    """Неизвестное имя — ошибка пользователя, и ответ должен помогать её исправить.

    Сырое «table with name X does not exist» оставляет гадать, как датасет называется
    в SQL, хотя backend это знает.
    """
    status, body = sql(client, workspace_id, "SELECT * FROM несуществующий")

    assert status == 400
    assert body["error"]["code"] == "unknown_dataset"
    assert set(body["error"]["details"]["available"]) == {"продажи", "клиенты"}


def test_a_dataset_of_another_workspace_is_not_reachable(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #псевдонимы принадлежат рабочему пространству: чужой датасет обязан выглядеть отсутствующим
    other = client.post("/api/workspaces", json={"name": "Чужое"}).json()["workspace_id"]
    status, body = sql(client, other, "SELECT * FROM продажи")

    assert status == 400
    assert body["error"]["code"] == "unknown_dataset"
    assert body["error"]["details"]["available"] == []


def test_a_query_without_any_dataset_is_refused(client: TestClient, workspace_id: str) -> None:
    #окно предназначено для работы с данными: «SELECT 1» ею не является
    status, body = sql(client, workspace_id, "SELECT 1")

    assert status == 403
    assert body["error"]["code"] == "sql_rejected"


# ── защита сохраняется на уровне HTTP ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        #по одному представителю на слой: тип оператора, белый список функций,
        #проверка плана. Полный враждебный корпус живёт в тестах песочницы
        #и в живой сквозной проверке; здесь доказывается одно — защита
        #не теряется по дороге до HTTP
        "ATTACH 'x.db' AS x",
        "SELECT current_setting('temp_directory')",
        "PRAGMA database_list",
    ],
)
def test_hostile_queries_are_refused_over_http_too(
    client: TestClient, workspace_id: str, datasets: dict, text: str
) -> None:
    status, body = sql(client, workspace_id, text)

    assert status in {400, 403, 422}, body
    assert body["error"]["code"] in {"sql_rejected", "sql_invalid", "unknown_dataset"}


# ── ограничения ───────────────────────────────────────────────────────────────


def test_truncation_is_reported_honestly(client: TestClient, workspace_id: str, datasets: dict) -> None:
    status, body = sql(client, workspace_id, "SELECT * FROM продажи", row_limit=5)

    assert status == 200
    assert len(body["result"]["rows"]) == 5
    assert body["result"]["truncated"] is True
    assert body["result"]["truncated_by"] == "rows"
    assert body["run"]["truncated"] is True


def test_a_result_that_fits_is_not_marked_truncated(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    _, body = sql(client, workspace_id, "SELECT * FROM продажи LIMIT 3", row_limit=100)

    assert body["result"]["truncated"] is False
    assert body["result"]["truncated_by"] is None


def test_a_row_limit_above_the_ceiling_is_rejected_by_the_schema(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    status, _ = sql(client, workspace_id, "SELECT * FROM продажи", row_limit=10_000_000)

    assert status == 422


# ── история ───────────────────────────────────────────────────────────────────


def test_history_records_both_success_and_failure(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    """К истории чаще всего возвращаются именно после неудачи.

    История только удачных запросов отвечает не на тот вопрос.
    """
    sql(client, workspace_id, "SELECT count(*) FROM продажи")
    sql(client, workspace_id, "SELECT * FROM duckdb_settings()")

    runs = client.get(f"/api/workspaces/{workspace_id}/sql/history").json()["runs"]

    assert [run["status"] for run in runs] == ["failed", "succeeded"]
    assert runs[0]["error_code"] == "sql_rejected"


def test_history_never_contains_the_result_rows(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    sql(client, workspace_id, "SELECT * FROM продажи")
    runs = client.get(f"/api/workspaces/{workspace_id}/sql/history").json()["runs"]

    assert "rows" not in runs[0]
    assert runs[0]["row_count"] == 20


def test_history_survives_a_restart_of_the_application(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #история живёт на сервере, а не в браузере
    sql(client, workspace_id, "SELECT count(*) FROM продажи")
    context.reset_context()

    restarted = TestClient(create_app())
    runs = restarted.get(f"/api/workspaces/{workspace_id}/sql/history").json()["runs"]

    assert len(runs) == 1
    assert runs[0]["sql"] == "SELECT count(*) FROM продажи"


def test_history_of_a_missing_workspace_is_not_found(client: TestClient) -> None:
    response = client.get("/api/workspaces/ws_0000000000000000/sql/history")

    assert response.status_code == 404


# ── сохранённые запросы ───────────────────────────────────────────────────────


def test_a_saved_query_round_trips_through_http(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    created = client.post(
        f"/api/workspaces/{workspace_id}/sql/saved",
        json={"name": "Выручка по городам", "sql": "SELECT город FROM продажи"},
    )

    assert created.status_code == 201, created.text

    listed = client.get(f"/api/workspaces/{workspace_id}/sql/saved").json()["queries"]

    assert [item["name"] for item in listed] == ["Выручка по городам"]


def test_a_duplicate_name_is_a_conflict_not_a_server_error(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    body = {"name": "Выручка", "sql": "SELECT город FROM продажи"}
    client.post(f"/api/workspaces/{workspace_id}/sql/saved", json=body)
    second = client.post(f"/api/workspaces/{workspace_id}/sql/saved", json=body)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "saved_query_name_taken"


def test_an_unparseable_query_is_not_saved(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #запись, которую заведомо нельзя выполнить, не должна попадать в список
    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/saved",
        json={"name": "Плохой", "sql": "DROP TABLE продажи"},
    )

    assert response.status_code == 403
    assert client.get(f"/api/workspaces/{workspace_id}/sql/saved").json()["queries"] == []


def test_a_query_about_a_missing_dataset_can_still_be_saved(
    client: TestClient, workspace_id: str
) -> None:
    """Заготовку сохранить нужно уметь.

    Датасет может появиться позже; требовать его наличия значит запрещать готовить
    запросы заранее.
    """
    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/saved",
        json={"name": "Заготовка", "sql": "SELECT * FROM будущий_датасет"},
    )

    assert response.status_code == 201


def test_updating_a_deleted_saved_query_is_reported(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #гонка «редактирую — а его удалили»: воскрешать удалённое нельзя
    created = client.post(
        f"/api/workspaces/{workspace_id}/sql/saved",
        json={"name": "Исчезнет", "sql": "SELECT город FROM продажи"},
    ).json()
    client.delete(f"/api/workspaces/{workspace_id}/sql/saved/{created['saved_query_id']}")

    response = client.put(
        f"/api/workspaces/{workspace_id}/sql/saved/{created['saved_query_id']}",
        json={"name": "Новое", "sql": "SELECT id FROM продажи"},
    )

    assert response.status_code == 404
    assert client.get(f"/api/workspaces/{workspace_id}/sql/saved").json()["queries"] == []


def test_deleting_twice_is_reported_the_second_time(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    created = client.post(
        f"/api/workspaces/{workspace_id}/sql/saved",
        json={"name": "Раз", "sql": "SELECT id FROM продажи"},
    ).json()
    path = f"/api/workspaces/{workspace_id}/sql/saved/{created['saved_query_id']}"

    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 404


def test_saved_queries_survive_a_restart(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    client.post(
        f"/api/workspaces/{workspace_id}/sql/saved",
        json={"name": "Переживёт", "sql": "SELECT id FROM продажи"},
    )
    context.reset_context()

    restarted = TestClient(create_app())
    queries = restarted.get(f"/api/workspaces/{workspace_id}/sql/saved").json()["queries"]

    assert [item["name"] for item in queries] == ["Переживёт"]


# ── отмена ────────────────────────────────────────────────────────────────────


def test_cancelling_a_running_query_actually_stops_it(
    client: TestClient, workspace_id: str
) -> None:
    """Отмена прерывает работу, а не просто закрывает вкладку.

    Тяжёлый запрос запускается в отдельном потоке, отменяется по метке и обязан
    завершиться отменой заметно раньше своего таймаута.
    """
    frame = pl.DataFrame({"id": range(40_000), "amount": [float(n) for n in range(40_000)]})
    upload(client, workspace_id, frame, "большой.parquet")

    token = uuid.uuid4().hex
    outcome: dict = {}

    def execute() -> None:
        status, body = sql(
            client,
            workspace_id,
            "SELECT count(*) FROM большой a JOIN большой b ON a.amount < b.amount",
            query_token=token,
            timeout_seconds=120,
        )
        outcome["status"] = status
        outcome["body"] = body

    worker = threading.Thread(target=execute)
    started = time.perf_counter()
    worker.start()

    #ждём, пока запрос действительно зарегистрируется: отменять раньше нечего
    deadline = time.perf_counter() + 10

    while context.get_running_queries().running_count(workspace_id) == 0:
        if time.perf_counter() > deadline:
            pytest.fail("запрос не появился в реестре выполняющихся")

        time.sleep(0.05)

    cancelled = client.delete(f"/api/workspaces/{workspace_id}/sql/queries/{token}")
    worker.join(timeout=30)
    elapsed = time.perf_counter() - started

    assert cancelled.status_code == 204
    assert outcome["status"] == 499, outcome
    assert outcome["body"]["error"]["code"] == "sql_cancelled"
    assert elapsed < 30, f"запрос не был прерван: {elapsed:.1f} с"


def test_cancelling_a_finished_query_says_there_is_nothing_to_cancel(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #«уже завершён» — не сбой сервера, и интерфейс обязан различать это состояние
    token = uuid.uuid4().hex
    sql(client, workspace_id, "SELECT count(*) FROM продажи", query_token=token)

    response = client.delete(f"/api/workspaces/{workspace_id}/sql/queries/{token}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "query_not_running"


def test_a_malformed_token_is_refused(client: TestClient, workspace_id: str) -> None:
    response = client.delete(f"/api/workspaces/{workspace_id}/sql/queries/../../etc/passwd")

    assert response.status_code in {404, 422}


def test_the_registry_is_empty_after_a_query_finishes(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    #утечка записей означала бы, что метку нельзя использовать повторно, а память растёт
    token = uuid.uuid4().hex
    sql(client, workspace_id, "SELECT count(*) FROM продажи", query_token=token)

    assert context.get_running_queries().running_count(workspace_id) == 0


def test_a_failed_query_also_frees_its_token(
    client: TestClient, workspace_id: str, datasets: dict
) -> None:
    token = uuid.uuid4().hex
    sql(client, workspace_id, "SELECT * FROM duckdb_settings()", query_token=token)

    assert context.get_running_queries().running_count(workspace_id) == 0
    #метку можно использовать снова: она освободилась, а не осталась занятой навсегда
    status, _ = sql(client, workspace_id, "SELECT count(*) FROM продажи", query_token=token)

    assert status == 200
