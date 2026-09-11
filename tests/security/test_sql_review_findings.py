"""Regression-проверки находок разбора Этапа 3.

Каждая проверка здесь появилась после воспроизведённого дефекта, а не «на всякий случай».
В заголовке каждой — чем именно она была вызвана.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from backend.adapters.engine.duckdb_session import (
    MAX_LIKE_WILDCARDS,
    DatasetUnreadableError,
    QueryLimits,
    SqlRejectedError,
    execute_query,
    hardened_session,
    parse_query,
)
from backend.api.app import create_app
from backend.core import config
from backend.domain.dataset.identifiers import new_dataset_id, new_workspace_id
from backend.domain.dataset.models import (
    ColumnSchema,
    Dataset,
    DatasetSchema,
    DatasetSource,
    DatasetStatus,
    LogicalType,
    SemanticType,
)
from backend.services import context
from backend.services.running_queries import (
    QueryNotRunningError,
    QueryTokenInUseError,
    RunningQueries,
)
from backend.services.sql_workspace import DatasetNotQueryableError, SqlWorkspace


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
    return client.post("/api/workspaces", json={"name": "Разбор"}).json()["workspace_id"]


def upload_csv(client: TestClient, workspace_id: str, name: str, content: str) -> dict:
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets",
        files={"file": (name, content.encode("utf-8"))},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── R-46: шаблон LIKE, убивавший процесс ──────────────────────────────────────


def test_a_backtracking_like_pattern_is_refused() -> None:
    """Такой шаблон не «медленный» — он убивал процесс целиком.

    Воспроизведено на живом движке: `text LIKE '%_%_%_…'` из сорока чередований роняет
    интерпретатор с `Fatal Python error`. Ни таймаут, ни предел памяти, ни отмена не
    помогают: все они живут в том же процессе и умирают вместе с ним, а вместе с ними —
    все остальные запросы и весь сервер.

    Сам падающий запрос здесь не выполняется намеренно: он завершил бы и этот процесс.
    Проверяется, что до выполнения он не доходит.

    **Эта проверка остаётся навсегда.** Она не отменяется вынесением SQL в отдельный
    процесс (долг D-01) и не заменяется им: изоляция меняет последствие падения,
    а проверка доказывает, что один пользовательский запрос не роняет DataArena.
    После обновления DuckDB, переписывания валидатора или смены исполнителя запросов
    она должна продолжать доказывать ровно это.
    """
    #число подстановок перебирать незачем: границу проверяет тест предела,
    #а формы и позиции — соседний тест
    sql = "SELECT * FROM rows WHERE text LIKE '" + "%_" * 400 + "%'"

    with pytest.raises(SqlRejectedError) as raised:
        parse_query(sql)

    assert "LIKE" in str(raised.value)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM rows WHERE text NOT LIKE '{pattern}'",
        "SELECT * FROM rows WHERE text ILIKE '{pattern}'",
        "SELECT * FROM rows WHERE text LIKE '{pattern}' ESCAPE '!'",
        "SELECT CASE WHEN text LIKE '{pattern}' THEN 1 END FROM rows",
        "WITH t AS (SELECT * FROM rows WHERE text LIKE '{pattern}') SELECT * FROM t",
        "SELECT * FROM rows GROUP BY text HAVING max(text) LIKE '{pattern}'",
    ],
)
def test_the_pattern_is_checked_in_every_form_and_position(sql: str) -> None:
    #падает всё семейство LIKE, а не только сам LIKE: ILIKE и NOT LIKE проверены отдельно
    with pytest.raises(SqlRejectedError):
        parse_query(sql.format(pattern="%_" * 100))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM rows WHERE text LIKE '%москва%'",
        "SELECT * FROM rows WHERE text ILIKE 'МОС%'",
        "SELECT * FROM rows WHERE text LIKE '_____'",
        "SELECT * FROM rows WHERE text LIKE '%%%%%'",
        "SELECT * FROM rows WHERE text LIKE '" + "%_" * MAX_LIKE_WILDCARDS + "'",
    ],
)
def test_ordinary_like_patterns_still_work(sql: str) -> None:
    """Обычный поиск по подстроке остаётся доступен.

    Проверка бесполезна, если ломает работу: LIKE — самый частый способ найти строку.
    Одиночные серии «%» и «_» безопасны — измерено, что падение вызывает именно
    чередование.
    """
    parse_query(sql)


def test_a_pattern_taken_from_data_is_not_the_same_danger(tmp_path: Path) -> None:
    """Шаблон из колонки проверить нельзя — и не нужно.

    Измерено: движок исполняет непостоянный шаблон другим путём и не падает. Поэтому
    проверка ограничена постоянными шаблонами сознательно, а не по недосмотру.
    """
    path = tmp_path / "rows.parquet"
    pl.DataFrame({"text": ["значение"] * 100, "pat": ["%_" * 100 + "%"] * 100}).write_parquet(path)

    with hardened_session({"rows": path}) as connection:
        result = execute_query(
            connection,
            "SELECT count(*) AS n FROM rows WHERE text LIKE pat",
            limits=QueryLimits(timeout_seconds=30),
        )

    assert result.rows[0]["n"] == 0


def test_the_backtracking_pattern_is_refused_over_http(
    client: TestClient, workspace_id: str
) -> None:
    #через API, тем же путём, которым ходит интерфейс, и сервер обязан пережить это
    upload_csv(client, workspace_id, "данные.csv", "text\nзначение\nдругое\n")
    pattern = "%_" * 400 + "%"

    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries",
        json={"sql": f"SELECT count(*) FROM данные WHERE text LIKE '{pattern}'"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "sql_rejected"

    #сервер продолжает работать: это половина смысла проверки
    still_alive = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries",
        json={"sql": "SELECT count(*) AS n FROM данные"},
    )
    assert still_alive.status_code == 200
    assert still_alive.json()["result"]["rows"][0]["n"] == 2


# ── R-47: датасет, который читается таблицей, но не движком ───────────────────


def test_a_dataset_the_engine_cannot_open_is_a_domain_error_not_a_crash(
    client: TestClient, workspace_id: str
) -> None:
    """CSV с разным числом полей в строках: Polars читает, pyarrow — нет.

    Датасет при этом показан в таблице и значится готовым, а запрос к нему падал
    необработанным исключением, то есть ответом 500 без объяснения.
    """
    dataset = upload_csv(client, workspace_id, "рваный.csv", "a,b\n1,2\n3\n4,5,6\n")

    page = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?limit=5"
    )
    assert page.status_code == 200, "постраничный просмотр такого файла работает"

    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries",
        json={"sql": "SELECT count(*) FROM рваный"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dataset_unreadable"
    assert "рваный" in response.json()["error"]["message"]


def test_the_reason_survives_but_the_path_does_not(tmp_path: Path) -> None:
    #обрезка по последнему двоеточию оставляла от причины одно число: путь именно вычищается
    path = tmp_path / "ragged.csv"
    path.write_text("a,b\n1,2\n3\n4,5,6\n", encoding="utf-8")

    with pytest.raises(DatasetUnreadableError) as raised, hardened_session({"рваный": path}):
        pass

    message = str(raised.value)

    assert "CSV" in message, f"причина потеряна: {message}"
    assert str(tmp_path) not in message
    assert "C:/" not in message and "C:\\" not in message


# ── R-48: идентификатор запроса при внутренней ошибке ─────────────────────────


def test_an_unexpected_error_still_carries_a_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Идентификатор терялся ровно там, ради чего заводился.

    Обработчик необработанного исключения выполняется в `ServerErrorMiddleware`,
    снаружи пользовательских middleware, и contextvar там пуст. Ответ 500 уходил
    с пустым `X-Request-ID` и текстом «записаны в лог под идентификатором .».
    """
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    application = create_app()
    router = APIRouter()

    @router.get("/boom")
    def boom() -> dict:
        raise RuntimeError("непредвиденный сбой внутри")

    application.include_router(router, prefix="/api")
    failing = TestClient(application, raise_server_exceptions=False)

    response = failing.get("/api/boom")

    assert response.status_code == 500
    assert response.headers.get("X-Request-ID"), "заголовок пуст"
    assert response.json()["request_id"], "идентификатор не попал в тело"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    #и в сообщении он подставлен, а не оставлен пустым местом
    assert response.json()["request_id"] in response.json()["error"]["message"]

    config.get_settings.cache_clear()
    context.reset_context()


def test_a_domain_error_still_carries_its_request_id(client: TestClient) -> None:
    #обратная сторона: правка не должна была сломать работавший случай
    response = client.get("/api/workspaces/ws_0000000000000000")

    assert response.status_code == 404
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


# ── защиты, у которых не было ни одного теста ─────────────────────────────────


def test_the_same_query_token_cannot_be_used_twice_at_once() -> None:
    """Занятая метка отвергается, иначе управление первым запросом теряется.

    Защита существовала, но не была покрыта ничем: молчаливая замена записи оставила бы
    выполняющийся запрос без ручки, и отменить его стало бы нечем.
    """
    registry = RunningQueries()
    token = uuid.uuid4().hex

    with ExitStack() as running:
        running.enter_context(registry.slot("ws_0123456789abcdef", token))

        with pytest.raises(QueryTokenInUseError):
            running.enter_context(registry.slot("ws_0123456789abcdef", token))

    #после освобождения метку можно использовать снова
    with registry.slot("ws_0123456789abcdef", token):
        pass


def test_the_same_token_in_another_workspace_is_a_different_query() -> None:
    #метки не общие: совпадение у двух рабочих пространств не должно мешать
    registry = RunningQueries()
    token = uuid.uuid4().hex

    with registry.slot("ws_0123456789abcdef", token), registry.slot("ws_0123456789abcdee", token):
        pass


def test_cancelling_a_token_of_another_workspace_does_nothing() -> None:
    #отмена ограничена рабочим пространством: чужой запрос ею не остановить
    registry = RunningQueries()
    token = uuid.uuid4().hex

    with registry.slot("ws_0123456789abcdef", token), pytest.raises(QueryNotRunningError):
        registry.cancel("ws_0123456789abcdee", token)


def test_a_dataset_that_failed_normalisation_is_refused_with_its_reason(
    tmp_path: Path,
) -> None:
    """Датасет в незавершённом состоянии не должен молча отсутствовать в SQL.

    Защита существовала, но не проверялась: без неё запрос к такому датасету дошёл бы
    до открытия несуществующего артефакта.
    """
    from backend.adapters.storage.workspace_store import WorkspaceStore

    store = WorkspaceStore(tmp_path / "state.db")
    workspace = store.create_workspace(new_workspace_id(), "Проверка")
    store.add_dataset(
        Dataset(
            dataset_id=new_dataset_id(),
            workspace_id=workspace.workspace_id,
            name="битый.json",
            alias="",
            source=DatasetSource(file_name="битый.json", format="json", bytes=10, sha256="a" * 64),
            schema=DatasetSchema(
                columns=(
                    ColumnSchema("a", 0, "String", LogicalType.STRING, SemanticType.TEXT, True),
                )
            ),
            row_count=None,
            created_at=datetime.now(UTC),
            status=DatasetStatus.NORMALIZATION_FAILED,
            status_reason="конвертация не удалась",
        )
    )

    workspace_sql = SqlWorkspace(
        store=store, workspace_root=tmp_path, workspace_id=workspace.workspace_id
    )

    with pytest.raises(DatasetNotQueryableError) as raised:
        workspace_sql.run("SELECT * FROM битый")

    assert "конвертация не удалась" in str(raised.value)


@pytest.mark.parametrize(
    "field,value",
    [("name", "и" * 200), ("description", "о" * 2000)],
)
def test_over_long_saved_query_fields_are_refused(
    client: TestClient, workspace_id: str, field: str, value: str
) -> None:
    #пределы существовали и не проверялись ничем
    body = {"name": "Обычное", "sql": "SELECT 1 FROM t", field: value}
    response = client.post(f"/api/workspaces/{workspace_id}/sql/saved", json=body)

    assert response.status_code in {403, 422}


# ── TOCTOU ────────────────────────────────────────────────────────────────────


def test_deleting_the_workspace_during_a_query_does_not_break_the_server(
    client: TestClient, workspace_id: str
) -> None:
    """Рабочее пространство удаляют, пока запрос выполняется.

    Запрос либо доработает на уже открытом артефакте, либо получит доменную ошибку.
    Недопустимо только одно: необработанное исключение и ответ 500.
    """
    upload_csv(
        client,
        workspace_id,
        "данные.csv",
        "id,text\n" + "".join(f"{n},значение{n}\n" for n in range(5000)),
    )

    outcome: dict = {}

    def query() -> None:
        response = client.post(
            f"/api/workspaces/{workspace_id}/sql/queries",
            json={
                "sql": "SELECT count(*) AS n FROM данные a JOIN данные b ON a.id = b.id",
                "timeout_seconds": 30,
            },
        )
        outcome["status"] = response.status_code
        outcome["body"] = response.json()

    worker = threading.Thread(target=query)
    worker.start()
    time.sleep(0.05)
    client.delete(f"/api/workspaces/{workspace_id}")
    worker.join(timeout=60)

    assert outcome["status"] != 500, f"необработанное исключение: {outcome}"


def test_a_query_against_a_deleted_workspace_is_not_found(
    client: TestClient, workspace_id: str
) -> None:
    upload_csv(client, workspace_id, "данные.csv", "id\n1\n2\n")
    client.delete(f"/api/workspaces/{workspace_id}")

    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries", json={"sql": "SELECT * FROM данные"}
    )

    assert response.status_code == 404


def test_the_artifact_file_disappearing_before_binding_is_a_domain_error(
    client: TestClient, workspace_id: str, tmp_path: Path
) -> None:
    """Файл исчезает между чтением списка датасетов и открытием соединения.

    Окно между проверкой и использованием неустранимо; оно обязано превращаться
    в понятный отказ, а не в `FileNotFoundError` и ответ 500.
    """
    upload_csv(client, workspace_id, "данные.csv", "id\n1\n2\n")

    for path in (tmp_path / "workspaces" / workspace_id / "sources").rglob("*"):
        if path.is_file():
            path.unlink()

    response = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries", json={"sql": "SELECT * FROM данные"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_dataset"


# ── раскрытие внутренних подробностей ─────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        #четыре разных источника сообщения: ошибка связывания, ошибка выполнения,
        #отказ белого списка, отказ по типу оператора. Остальные повторяют их путь
        "SELECT нет_такой_колонки FROM данные",
        "SELECT 1/0 FROM данные",
        "SELECT * FROM read_parquet('C:/Windows/win.ini')",
        "ATTACH 'C:/Users/secret.db' AS s",
    ],
)
def test_no_error_reveals_a_server_path_or_a_traceback(
    client: TestClient, workspace_id: str, sql: str
) -> None:
    #сообщение уходит в браузер и в историю: путей, трассировок и окружения там быть не должно
    upload_csv(client, workspace_id, "данные.csv", "id\n1\n2\n")
    body = str(client.post(f"/api/workspaces/{workspace_id}/sql/queries", json={"sql": sql}).json())

    for marker in ("C:/", "C:\\", "AppData", "Traceback", ".duckdb", "site-packages", "/etc/"):
        assert marker not in body, f"в ответе есть «{marker}»: {body[:200]}"


def test_the_history_stores_a_code_and_never_the_engine_message(
    client: TestClient, workspace_id: str
) -> None:
    """История переживает перезапуск и уходит в экспорт — сообщению движка там не место.

    Текст самого запроса при этом хранится дословно, включая написанный пользователем
    путь: это его собственный ввод, и подменять его в истории значило бы показывать
    не то, что он запускал.
    """
    upload_csv(client, workspace_id, "данные.csv", "id\n1\n2\n")
    client.post(
        f"/api/workspaces/{workspace_id}/sql/queries",
        json={"sql": "SELECT * FROM read_parquet('C:/Windows/win.ini')"},
    )

    entry = client.get(f"/api/workspaces/{workspace_id}/sql/history").json()["runs"][0]

    assert entry["error_code"] == "sql_rejected"
    assert entry["sql"] == "SELECT * FROM read_parquet('C:/Windows/win.ini')"

    #всё, кроме введённого пользователем текста, обязано быть чистым
    beyond_the_query = {key: value for key, value in entry.items() if key != "sql"}

    for marker in ("C:/", "C:\\", "AppData", "Traceback", "Permission Error"):
        assert marker not in str(beyond_the_query), f"в истории есть «{marker}»"


# ── одновременная работа ──────────────────────────────────────────────────────


def test_two_heavy_queries_do_not_block_each_other_indefinitely(
    client: TestClient, workspace_id: str
) -> None:
    """Две тяжёлые задачи одновременно обязаны обе завершиться по таймауту.

    Общий замок на выполнение превратил бы второй запрос в вечное ожидание.
    """
    upload_csv(
        client,
        workspace_id,
        "данные.csv",
        "id\n" + "".join(f"{n}\n" for n in range(20_000)),
    )

    results: list[int] = []

    def heavy() -> None:
        response = client.post(
            f"/api/workspaces/{workspace_id}/sql/queries",
            json={
                "sql": "SELECT count(*) AS n FROM данные a JOIN данные b ON a.id < b.id",
                "timeout_seconds": 3,
            },
        )
        results.append(response.status_code)

    started = time.perf_counter()
    workers = [threading.Thread(target=heavy) for _ in range(2)]

    for worker in workers:
        worker.start()

    for worker in workers:
        worker.join(timeout=90)

    elapsed = time.perf_counter() - started

    assert len(results) == 2, "не все запросы завершились"
    assert all(status in {200, 408} for status in results), results
    assert elapsed < 60, f"обе задачи заняли {elapsed:.0f} с — похоже на общий замок"


# ── R-49: управляющий символ разводил выполненное и записанное ────────────────


def test_a_nul_byte_in_the_query_is_refused() -> None:
    """Разборщик и движок понимают нулевой байт по-разному.

    `duckdb.extract_statements` считает «SELECT 1 FROM t\x00; DROP TABLE t» одним
    оператором SELECT и возвращает его **целиком**, вместе с хвостом. Движок при
    выполнении обрывает строку на этом байте. Значит, выполняется одно, а в историю
    и в лог уходит другое: читающий журнал увидит одобренный DROP, которого не было.
    """
    with pytest.raises(SqlRejectedError):
        parse_query("SELECT 1 FROM rows\x00; DROP TABLE rows")


#проверка одна — диапазонная, поэтому нужны края и один середины
@pytest.mark.parametrize("code_point", [0x00, 0x1B, 0x7F])
def test_control_characters_are_refused(code_point: int) -> None:
    with pytest.raises(SqlRejectedError):
        parse_query(f"SELECT 1 FROM rows WHERE a = '{chr(code_point)}'")


@pytest.mark.parametrize("sql", ["SELECT 1\nFROM rows", "SELECT\t1 FROM rows", "SELECT 1\r\nFROM rows"])
def test_formatting_whitespace_is_still_allowed(sql: str) -> None:
    #на переводе строки и табуляции держится форматирование: запрещать их незачем
    parse_query(sql)


def test_what_the_history_stores_is_what_the_engine_received(
    client: TestClient, workspace_id: str
) -> None:
    #то же свойство, проверенное через API: в истории ровно тот текст, что выполнялся
    upload_csv(client, workspace_id, "данные.csv", "id\n1\n2\n")
    sql = "  ;SELECT count(*) AS n FROM данные"

    response = client.post(f"/api/workspaces/{workspace_id}/sql/queries", json={"sql": sql})
    assert response.status_code == 200

    entry = client.get(f"/api/workspaces/{workspace_id}/sql/history").json()["runs"][0]

    assert entry["sql"] == "SELECT count(*) AS n FROM данные"
    assert "\x00" not in entry["sql"]
