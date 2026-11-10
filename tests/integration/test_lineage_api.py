"""Происхождение: откуда взялся датасет и что стало с каждой колонкой.

Раздел существует ради двух вопросов, которые задают через месяц после сборки.
Здесь проверяются ровно они, а не наличие эндпоинта.
"""

from __future__ import annotations

import io
import json
import zipfile
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
    return client.post("/api/workspaces", json={"name": "Происхождение"}).json()["workspace_id"]


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    buffer.seek(0)

    return client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    ).json()["dataset_id"]


def lineage(client: TestClient, workspace_id: str, dataset_id: str) -> dict:
    response = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/lineage")

    assert response.status_code == 200, response.text

    return response.json()


def test_a_column_says_which_steps_changed_it(client: TestClient, workspace_id: str) -> None:
    """«Откуда взялась эта колонка» — главный вопрос раздела.

    «Датасет получен из clients.csv» ещё можно восстановить по имени файла; «колонка
    `сумма` — это текст, разобранный в число на третьем шаге» не восстановить никак,
    а именно это объясняет, откуда в ней пропуски.
    """
    dataset_id = upload(
        client,
        workspace_id,
        pl.DataFrame({"сумма": ["1 200,50", "300", "н/д"], "город": ["Москва ", "Омск", "Уфа"]}),
        "клиенты.parquet",
    )
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {"step_id": "s1", "kind": "trim_whitespace", "params": {"columns": ["город"]}},
                {
                    "step_id": "s2",
                    "kind": "normalize_nulls",
                    "params": {"columns": ["сумма"]},
                },
                {"step_id": "s3", "kind": "parse_number", "params": {"column": "сумма"}},
            ]
        },
    )

    body = lineage(client, workspace_id, dataset_id)
    columns = {column["name"]: column for column in body["columns"]}

    assert columns["сумма"]["dtype"] == "Float64"
    assert [item["position"] for item in columns["сумма"]["events"]] == [2, 3]
    assert "Разобрать" in columns["сумма"]["events"][-1]["description"]
    #колонку «город» трогал только первый шаг
    assert [item["position"] for item in columns["город"]["events"]] == [1]


def test_a_renamed_column_keeps_its_history_and_its_original_name(
    client: TestClient, workspace_id: str
) -> None:
    #переименование уносит прежнее имя: если история осталась бы за старым именем,
    #раздел показал бы колонку без прошлого — и это худший вид молчания
    dataset_id = upload(
        client, workspace_id, pl.DataFrame({"amount": ["1", "2"]}), "источник.parquet"
    )
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {"step_id": "s1", "kind": "parse_number", "params": {"column": "amount"}},
                {
                    "step_id": "s2",
                    "kind": "rename_column",
                    "params": {"column": "amount", "to": "сумма"},
                },
            ]
        },
    )

    columns = {column["name"]: column for column in lineage(client, workspace_id, dataset_id)["columns"]}

    assert set(columns) == {"сумма"}
    #переименование — событие, а не новое происхождение: значения по-прежнему из файла
    assert columns["сумма"]["origin"] == "source"
    assert [item["column"] for item in columns["сумма"]["origins"]] == ["amount"]
    assert columns["сумма"]["source_name"] == "amount", "потеряно исходное имя колонки"
    assert [item["position"] for item in columns["сумма"]["events"]] == [1, 2]


def test_a_joined_dataset_names_both_parents_and_the_key(
    client: TestClient, workspace_id: str
) -> None:
    """Собранный датасет — не файл, взявшийся ниоткуда.

    Способ создания известен только в момент создания: если его не записать, потом
    восстановить нечем.
    """
    left = upload(
        client, workspace_id, pl.DataFrame({"id": [1, 2], "город": ["Москва", "Омск"]}), "клиенты.parquet"
    )
    right = upload(
        client, workspace_id, pl.DataFrame({"id": [1, 2], "сумма": [10.0, 20.0]}), "заказы.parquet"
    )
    created = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "kind": "inner",
            "name": "клиенты и заказы",
        },
    )

    assert created.status_code == 201, created.text

    body = lineage(client, workspace_id, created.json()["dataset_id"])

    assert body["status"] == "known"
    assert body["origin"] == "join"
    assert body["join"]["keys"] == [{"left": "id", "right": "id"}]
    assert body["join"]["kind"] == "inner"

    parents = body["parents"]
    assert [parent["dataset_name"] for parent in parents] == ["клиенты.parquet", "заказы.parquet"]
    assert [parent["role"] for parent in parents] == ["left", "right"]
    assert all(parent["current"]["available"] for parent in parents)
    assert all(parent["snapshot"] is not None for parent in parents)


def test_a_missing_parent_is_admitted_not_hidden(client: TestClient, workspace_id: str) -> None:
    """Родитель пропал — происхождение говорит об этом, а не показывает соединение ниоткуда.

    Удаления датасета в API сегодня нет, поэтому запись убирается прямо из хранилища:
    так выглядит и будущее удаление, и повреждённая база. Ветка защитная, и проверить
    её иначе нечем — а молчаливое «родитель есть» было бы враньём.
    """
    left = upload(client, workspace_id, pl.DataFrame({"id": [1], "a": ["x"]}), "левый.parquet")
    right = upload(client, workspace_id, pl.DataFrame({"id": [1], "b": ["y"]}), "правый.parquet")
    joined = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "name": "соединение",
        },
    ).json()["dataset_id"]

    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute("DELETE FROM datasets WHERE dataset_id = ?", (right,))
        connection.commit()

    body = lineage(client, workspace_id, joined)

    assert [parent["current"]["available"] for parent in body["parents"]] == [True, False]
    #снимок удалённого родителя остаётся: история ребёнка держится на нём, а не на родителе
    assert body["parents"][1]["dataset_name"] == "правый.parquet"
    assert body["parents"][1]["snapshot"] is not None
    columns = {column["name"]: column for column in body["columns"]}
    assert columns["b"]["origins"][0]["dataset_name"] == "правый.parquet"


def test_the_package_lineage_names_the_parents_too(
    client: TestClient, workspace_id: str, tmp_path: Path
) -> None:
    """То же самое обязано попасть в пакет: его читает ModelArena, а не интерфейс."""
    left = upload(client, workspace_id, pl.DataFrame({"id": [1, 2], "a": ["x", "y"]}), "л.parquet")
    right = upload(client, workspace_id, pl.DataFrame({"id": [1, 2], "b": ["z", "w"]}), "п.parquet")
    joined = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "name": "сборка",
        },
    ).json()["dataset_id"]

    response = client.get(f"/api/workspaces/{workspace_id}/datasets/{joined}/package")
    archive = tmp_path / "p.dapkg"
    archive.write_bytes(response.content)

    with zipfile.ZipFile(archive) as package:
        lineage_document = json.loads(package.read("lineage.json"))

    operations = [node["op"] for node in lineage_document["graph"]]

    #три чтения: оба родителя и собственный файл собранного датасета
    assert operations.count("read") == 3, "в пакете не видно обоих родителей"
    assert "join" in operations
    assert len(lineage_document["sources"]) == 3


def test_an_uploaded_dataset_is_simply_an_upload(client: TestClient, workspace_id: str) -> None:
    #датасет без соединения не должен обрастать выдуманным происхождением
    dataset_id = upload(client, workspace_id, pl.DataFrame({"a": [1, 2]}), "простой.parquet")
    body = lineage(client, workspace_id, dataset_id)

    assert body["status"] == "known"
    assert body["origin"] == "upload"
    assert body["join"] is None
    assert body["parents"] == []
    assert body["artifact"]["file_name"] == "простой.parquet"
    assert body["warnings"] == []
    assert body["columns"][0]["origin"] == "source"
    assert body["columns"][0]["events"] == []


# ── регрессии: случаи, на которых раздел врал ─────────────────────────────────


def put_steps(client: TestClient, workspace_id: str, dataset_id: str, steps: list[dict]) -> None:
    response = client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline", json={"steps": steps}
    )

    assert response.status_code == 200, response.text


def test_a_dropped_column_takes_its_history_with_it(client: TestClient, workspace_id: str) -> None:
    """Новая колонка под именем удалённой не наследует чужое прошлое.

    Было: `trim(b)`, `drop(b)`, `rename a → b` — и новая `b` показывала обрезку пробелов
    и собственное удаление, которых с её значениями никогда не происходило.
    """
    dataset_id = upload(
        client, workspace_id, pl.DataFrame({"a": ["x", "y"], "b": [" p", "q "]}), "два.parquet"
    )
    put_steps(
        client,
        workspace_id,
        dataset_id,
        [
            {"step_id": "s1", "kind": "trim_whitespace", "params": {"columns": ["b"]}},
            {"step_id": "s2", "kind": "drop_columns", "params": {"columns": ["b"]}},
            {"step_id": "s3", "kind": "rename_column", "params": {"column": "a", "to": "b"}},
        ],
    )

    columns = {column["name"]: column for column in lineage(client, workspace_id, dataset_id)["columns"]}

    assert set(columns) == {"b"}
    assert columns["b"]["source_name"] == "a"
    assert [item["position"] for item in columns["b"]["events"]] == [3], (
        "колонка унаследовала историю удалённой тёзки"
    )


def test_row_steps_do_not_pretend_to_change_columns(client: TestClient, workspace_id: str) -> None:
    """Отбор, удаление строк, сортировка, дедупликация и перестановка не меняют значений.

    Они читают колонку, но ни одно её значение не становится другим. Показать их в истории
    колонки — значит объяснять пропуски в ней шагом, который пропусков не создавал.
    """
    dataset_id = upload(
        client,
        workspace_id,
        pl.DataFrame({"a": [3, 1, 2, 2], "b": ["x", "y", "z", "z"]}),
        "строки.parquet",
    )
    put_steps(
        client,
        workspace_id,
        dataset_id,
        [
            {
                "step_id": "s1",
                "kind": "filter_rows",
                "params": {"column": "a", "operator": "gt", "value": "0"},
            },
            {
                "step_id": "s2",
                "kind": "drop_rows",
                "params": {"column": "b", "operator": "eq", "value": "nope"},
            },
            {"step_id": "s3", "kind": "sort_rows", "params": {"column": "a"}},
            {"step_id": "s4", "kind": "deduplicate", "params": {"columns": ["a", "b"]}},
            {"step_id": "s5", "kind": "reorder_columns", "params": {"order": ["b", "a"]}},
        ],
    )

    body = lineage(client, workspace_id, dataset_id)

    assert [column["name"] for column in body["columns"]] == ["b", "a"]
    assert all(column["events"] == [] for column in body["columns"]), body["columns"]
    #но что шаги колонки читали, видно — отдельно от истории её значений
    read_by = {column["name"]: column["read_by"] for column in body["columns"]}
    assert read_by == {"a": [1, 3, 4, 5], "b": [2, 4, 5]}


def test_join_columns_name_their_side_instead_of_guessing_the_suffix(
    client: TestClient, workspace_id: str
) -> None:
    """Сторона соединения берётся из карты колонок, а не угадывается по окончанию имени.

    Было: родная левая `total_2` объявлялась правой колонкой `total`, «переименованной
    суффиксом», а правая `amount` без конфликта имён — колонкой собственного файла сборки.
    Карта, сохранённая при сборке, отвечает на это точно.
    """
    left = upload(
        client, workspace_id, pl.DataFrame({"id": [1, 2], "total_2": [5, 6]}), "лево.parquet"
    )
    right = upload(
        client, workspace_id, pl.DataFrame({"id": [1, 2], "amount": [7, 8]}), "право.parquet"
    )
    joined = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "right_suffix": "_2",
            "name": "сборка",
        },
    )

    assert joined.status_code == 201, joined.text

    columns = {
        column["name"]: column
        for column in lineage(client, workspace_id, joined.json()["dataset_id"])["columns"]
    }

    assert [ref["dataset_name"] for ref in columns["total_2"]["origins"]] == ["лево.parquet"], (
        "левую колонку приписали правой стороне из-за окончания имени"
    )
    assert columns["total_2"]["source_name"] == "total_2"
    assert [ref["dataset_name"] for ref in columns["amount"]["origins"]] == ["право.parquet"], (
        "правая колонка выдана за колонку собственного файла сборки"
    )
    assert columns["id"]["origins"][0]["dataset_name"] == "лево.parquet"


@pytest.mark.parametrize("kind", ["inner", "left", "right", "full"])
def test_every_column_of_a_join_knows_its_side(
    client: TestClient, workspace_id: str, kind: str
) -> None:
    """Ни одна колонка результата не остаётся без происхождения — при любом виде соединения.

    Какая сторона отдаёт ключ, зависит от вида: левое оставляет левый, правое — правый,
    полное — оба. Ошибка здесь не видна в обычном прогоне: карта просто промолчала бы
    про колонку, и раздел показал бы «неизвестно» там, где всё известно.
    """
    left = upload(
        client, workspace_id, pl.DataFrame({"id": [1, 2], "цена": [10, 20]}), "лево.parquet"
    )
    right = upload(
        client, workspace_id, pl.DataFrame({"id": [2, 3], "цена": [7, 8]}), "право.parquet"
    )
    joined = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "kind": kind,
            "right_suffix": "_справа",
            "name": f"сборка_{kind}",
        },
    )

    assert joined.status_code == 201, joined.text

    columns = lineage(client, workspace_id, joined.json()["dataset_id"])["columns"]
    unknown = [column["name"] for column in columns if column["origin"] == "unknown"]

    assert unknown == [], f"колонки без происхождения: {unknown}"

    sides = {
        column["name"]: column["origins"][0]["dataset_name"]
        for column in columns
    }

    assert sides["цена"] == "лево.parquet"
    assert sides["цена_справа"] == "право.parquet"
    #ключ: значения результата принадлежат той стороне, которую соединение сохраняет
    assert sides["id"] == ("право.parquet" if kind == "right" else "лево.parquet")

    if kind == "full":
        #полное соединение оставляет оба ключа, и правый разводится суффиксом
        #пользователя, а не суффиксом библиотеки
        assert sides["id_справа"] == "право.parquet"
    else:
        assert "id_справа" not in sides


def test_a_join_built_before_the_column_map_says_unknown(
    client: TestClient, workspace_id: str
) -> None:
    """Старая запись без карты колонок не превращается в выдуманную сторону.

    Датасеты, собранные до появления карты, останутся у пользователей навсегда. Ответ
    «неизвестно» для них честен, а подстановка левой стороны по умолчанию — нет.
    """
    left = upload(client, workspace_id, pl.DataFrame({"id": [1], "a": [1]}), "лево.parquet")
    right = upload(client, workspace_id, pl.DataFrame({"id": [1], "b": [2]}), "право.parquet")
    joined = client.post(
        f"/api/workspaces/{workspace_id}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "name": "сборка",
        },
    ).json()["dataset_id"]

    store = context.get_workspace_store()

    with store._connect() as connection:
        stored = connection.execute(
            "SELECT document FROM dataset_provenance WHERE dataset_id = ?", (joined,)
        ).fetchone()[0]

    document = json.loads(stored)
    document["join"].pop("columns")

    with store._connect() as connection:
        connection.execute(
            "UPDATE dataset_provenance SET document = ? WHERE dataset_id = ?",
            (json.dumps(document, ensure_ascii=False), joined),
        )
        connection.commit()

    body = lineage(client, workspace_id, joined)

    assert body["parents"][0]["dataset_name"] == "лево.parquet", "родители должны остаться"
    assert body["warnings"] == ["join_column_map_missing"]
    assert [column["origin"] for column in body["columns"]] == ["unknown"] * 3
    assert all(column["origins"] == [] for column in body["columns"])
