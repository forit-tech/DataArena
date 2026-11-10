"""Регрессии RC-аудита: дефекты, найденные враждебной проверкой перед v1.0.0-rc.

Каждый тест воспроизводит найденный дефект так, как его встретил бы пользователь,
и фиксирует правильное поведение. Идентификатор дефекта — в имени раздела.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
def ws(client: TestClient) -> str:
    return client.post("/api/workspaces", json={"name": "Аудит"}).json()["workspace_id"]


def upload(client: TestClient, ws: str, frame: pl.DataFrame | None, name: str, raw: bytes | None = None) -> dict:
    buffer = io.BytesIO()

    if raw is not None:
        buffer.write(raw)
    elif frame is not None and name.endswith(".csv"):
        frame.write_csv(buffer)
    elif frame is not None:
        frame.write_parquet(buffer)

    buffer.seek(0)
    response = client.post(f"/api/workspaces/{ws}/datasets", files={"file": (name, buffer)})

    assert response.status_code in {200, 201}, response.text

    return response.json()


def put_steps(client: TestClient, ws: str, dataset_id: str, steps: list[dict]) -> Any:
    return client.put(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline",
        json={"steps": [{"step_id": f"s{index}", **step} for index, step in enumerate(steps, 1)]},
    )


def join(client: TestClient, ws: str, left: str, right: str, **spec: Any) -> dict:
    response = client.post(
        f"/api/workspaces/{ws}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "name": "сборка",
            **spec,
        },
    )

    assert response.status_code == 201, response.text

    return response.json()


def lineage(client: TestClient, ws: str, dataset_id: str) -> dict:
    response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/lineage")

    assert response.status_code == 200, response.text

    return response.json()


def artifact_bytes(dataset_id: str) -> bytes:
    root = Path(config.get_settings().workspace_root)

    return next(root.rglob(f"{dataset_id}.parquet")).read_bytes()


LEFT = pl.DataFrame({"id": [1, 2], "a": ["x", "y"]})
RIGHT = pl.DataFrame({"id": [1, 2], "b": [10, 20]})


# ── RC-01: сборка — событие, а не адрес содержимого ───────────────────────────


def test_rc01_a_second_join_with_identical_bytes_is_its_own_dataset(client: TestClient, ws: str) -> None:
    """Прежде вторая сборка с теми же байтами возвращала первую: другое имя, другой вид
    соединения, и история утверждала «inner», хотя пользователь просил «left»."""
    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    first = join(client, ws, left, right, kind="inner", name="первая")
    second = join(client, ws, left, right, kind="left", name="вторая")

    assert second["dataset_id"] != first["dataset_id"]
    assert second["name"] == "вторая.parquet"
    assert lineage(client, ws, second["dataset_id"])["join"]["kind"] == "left"
    assert lineage(client, ws, first["dataset_id"])["join"]["kind"] == "inner"


def test_rc01_a_join_after_a_parent_change_records_the_new_parent_state(client: TestClient, ws: str) -> None:
    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    first = join(client, ws, left, right, name="до")
    #сортировка по ключу не меняет байтов результата — но меняет состояние родителя
    assert put_steps(client, ws, left, [{"kind": "sort_rows", "params": {"column": "id"}}]).status_code == 200
    second = join(client, ws, left, right, name="после")

    assert second["dataset_id"] != first["dataset_id"]
    steps = lineage(client, ws, second["dataset_id"])["parents"][0]["snapshot"]["steps"]
    assert [step["operation"] for step in steps] == ["sort_rows"]


def test_rc01_a_join_equal_to_an_uploaded_file_does_not_become_that_upload(client: TestClient, ws: str) -> None:
    """Результат сборки, совпавший с загруженным файлом, не выдаётся за этот файл."""
    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    built = join(client, ws, left, right, name="эталон")
    raw = artifact_bytes(built["dataset_id"])

    other = client.post("/api/workspaces", json={"name": "Второе"}).json()["workspace_id"]
    left2 = upload(client, other, LEFT, "A.parquet")["dataset_id"]
    right2 = upload(client, other, RIGHT, "B.parquet")["dataset_id"]
    uploaded = upload(client, other, None, "C.parquet", raw=raw)
    result = join(client, other, left2, right2, name="моя_сборка")

    assert result["dataset_id"] != uploaded["dataset_id"]
    assert result["name"] == "моя_сборка.parquet"
    body = lineage(client, other, result["dataset_id"])
    assert body["origin"] == "join"
    assert [parent["dataset_id"] for parent in body["parents"]] == [left2, right2]
    #а загруженный файл остаётся загрузкой
    assert lineage(client, other, uploaded["dataset_id"])["origin"] == "upload"


def test_rc01_uploading_the_bytes_of_a_join_result_is_an_upload_not_the_join(client: TestClient, ws: str) -> None:
    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    built = join(client, ws, left, right, name="сборка")
    uploaded = upload(client, ws, None, "файл.parquet", raw=artifact_bytes(built["dataset_id"]))

    assert uploaded["dataset_id"] != built["dataset_id"]
    assert lineage(client, ws, uploaded["dataset_id"])["origin"] == "upload"


def test_rc01_the_same_file_uploaded_twice_is_still_one_dataset(client: TestClient, ws: str) -> None:
    #загрузка остаётся адресом содержимого: это задокументированное поведение раздела
    first = upload(client, ws, LEFT, "A.parquet")
    second = upload(client, ws, LEFT, "A-копия.parquet")

    assert second["dataset_id"] == first["dataset_id"]


def test_rc01_migration_lets_old_join_results_be_their_own_entities(
    client: TestClient, ws: str
) -> None:
    """База версии 7: индекс без условия. После миграции сборка перестаёт быть адресом содержимого.

    Миграция повторяема: второе открытие базы ничего не ломает.
    """
    import sqlite3

    from backend.adapters.storage.workspace_store import WorkspaceStore

    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    first = join(client, ws, left, right, name="старая")
    path = Path(config.get_settings().workspace_root) / "state.db"
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX datasets_by_content")
    connection.execute("CREATE UNIQUE INDEX datasets_by_content ON datasets(workspace_id, source_sha256)")
    connection.execute("UPDATE datasets SET content_addressed = 1")
    connection.execute("UPDATE schema_version SET version = 7")
    connection.commit()
    connection.close()

    WorkspaceStore(path)
    WorkspaceStore(path)
    context.reset_context()
    config.get_settings.cache_clear()
    restarted = TestClient(create_app())
    second = restarted.post(
        f"/api/workspaces/{ws}/joins",
        json={"left_dataset_id": left, "right_dataset_id": right, "keys": [{"left": "id", "right": "id"}], "name": "новая"},
    )

    assert second.status_code == 201, second.text
    assert second.json()["dataset_id"] != first["dataset_id"]
    #а повторная загрузка по-прежнему возвращает прежний датасет
    assert upload(restarted, ws, LEFT, "A2.parquet")["dataset_id"] == left


# ── RC-02: имя сборки уводило архив пакета за пределы временного каталога ────


@pytest.mark.parametrize(
    "name",
    [
        "../../rc02_escape",
        "..\\..\\rc02_escape",
        "C:/rc02_escape",
        "/tmp/rc02_escape",  # noqa: S108 - враждебное имя, файл по нему не создаётся
        "a/b",
        "nul\x00byte",
        "x" * 400,
    ],
    ids=["dotdot", "backslash", "drive", "absolute", "slash", "nul", "long"],
)
def test_rc02_a_hostile_join_name_cannot_write_outside_the_package_directory(
    client: TestClient, ws: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    import tempfile

    scratch = tmp_path / "scratch" / "deep"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    built = join(client, ws, left, right, name=name)

    assert "/" not in built["name"] and "\\" not in built["name"] and "\x00" not in built["name"]
    response = client.get(f"/api/workspaces/{ws}/datasets/{built['dataset_id']}/package")

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert ".." not in disposition and "/" not in disposition.split("filename", 1)[1].replace("utf-8''", "")
    #ни одного архива выше временного каталога
    assert list(tmp_path.rglob("*.dapkg")) == [] or all(
        scratch in path.parents for path in tmp_path.rglob("*.dapkg")
    )


# ── RC-03: правое соединение подписывало левую колонку правым ключом ─────────


def test_rc03_right_join_with_a_left_column_named_like_the_right_key(client: TestClient, ws: str) -> None:
    """Слева {id, rid}, справа {rid, x}, ключ id = rid, RIGHT JOIN.

    Было: колонка «rid» содержала левые значения, а карта называла её правым ключом;
    настоящий ключ уходил в «rid_right» — суффикс библиотеки, которого никто не выбирал.
    """
    left = upload(client, ws, pl.DataFrame({"id": [1, 2], "rid": [100, 200]}), "L.parquet")["dataset_id"]
    right = upload(client, ws, pl.DataFrame({"rid": [1, 2], "x": ["p", "q"]}), "R.parquet")["dataset_id"]
    built = join(
        client, ws, left, right, kind="right", keys=[{"left": "id", "right": "rid"}], right_suffix="_2"
    )
    rows = client.get(f"/api/workspaces/{ws}/datasets/{built['dataset_id']}/rows", params={"limit": 10}).json()
    mapping = {item["name"]: (item["side"], item["source"]) for item in lineage(client, ws, built["dataset_id"])["join"]["columns"]}

    assert sorted(rows["columns"]) == ["rid", "rid_2", "x"]
    assert [row["rid"] for row in rows["rows"]] == [100, 200]
    assert [row["rid_2"] for row in rows["rows"]] == [1, 2]
    assert mapping == {"rid": ("left", "rid"), "rid_2": ("right", "rid"), "x": ("right", "x")}


# ── RC-05: предпросмотр с несуществующей колонкой ключа ──────────────────────


def test_rc05_preview_with_a_missing_key_is_a_422(client: TestClient, ws: str) -> None:
    left = upload(client, ws, LEFT, "A.parquet")["dataset_id"]
    right = upload(client, ws, RIGHT, "B.parquet")["dataset_id"]
    response = client.post(
        f"/api/workspaces/{ws}/joins/preview",
        json={"left_dataset_id": left, "right_dataset_id": right, "keys": [{"left": "нет", "right": "id"}]},
    )

    assert response.status_code == 422, response.text


# ── RC-23: повреждённый файл при загрузке ─────────────────────────────────────


def truncated_parquet() -> bytes:
    buffer = io.BytesIO()
    pl.DataFrame({"a": list(range(1000))}).write_parquet(buffer)

    return buffer.getvalue()[:-40] + b"PAR1"


@pytest.mark.parametrize(
    "raw",
    [b"PAR1" + b"\x00" * 100 + b"PAR1", truncated_parquet()],
    ids=["garbage", "truncated"],
)
def test_rc23_a_damaged_file_is_named_damaged_and_leaves_nothing_behind(
    client: TestClient, ws: str, raw: bytes
) -> None:
    """Прежде — общая ошибка операции, а сам файл оставался в `sources/` навсегда."""
    response = client.post(f"/api/workspaces/{ws}/datasets", files={"file": ("битый.parquet", io.BytesIO(raw))})

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "dataset_read_error"
    assert "повреждён" in response.json()["error"]["message"]
    root = Path(config.get_settings().workspace_root) / ws
    assert [path for path in root.rglob("*") if path.is_file()] == []


def test_rc03_right_join_suffixes_only_real_conflicts(client: TestClient, ws: str) -> None:
    """В правом соединении левого ключа в результате нет: правая колонка с его именем
    ни с чем не сталкивается и суффикса не получает."""
    left = upload(client, ws, pl.DataFrame({"k": [1, 2], "v": ["a", "b"]}), "L.parquet")["dataset_id"]
    right = upload(client, ws, pl.DataFrame({"r": [1, 2], "k": ["x", "y"]}), "R.parquet")["dataset_id"]
    built = join(client, ws, left, right, kind="right", keys=[{"left": "k", "right": "r"}], right_suffix="_2")
    rows = client.get(f"/api/workspaces/{ws}/datasets/{built['dataset_id']}/rows", params={"limit": 10}).json()

    assert sorted(rows["columns"]) == ["k", "r", "v"]
    assert [row["k"] for row in rows["rows"]] == ["x", "y"]
