"""Release gate раздела «Происхождение».

Каждый тест — один способ, которым раздел мог бы соврать: унаследовать чужую историю,
выдать сортировку за преобразование, приписать колонку не той стороне, пересказать
текущего родителя вместо того, из которого собран датасет, превратить неизвестное
в загрузку, упасть на повреждённой записи или разойтись с пакетом.

Тесты идут через HTTP и через хранилище так же, как пользователь: загрузка, конвейер,
сборка, ещё конвейер, происхождение, пакет, перезапуск.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.api.app import create_app
from backend.core import config
from backend.domain.dataset.identifiers import new_workspace_id
from backend.services import context

REFERENCE = Path(__file__).resolve().parents[2] / "docs/contracts/reference"


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
    return client.post("/api/workspaces", json={"name": "Происхождение"}).json()["workspace_id"]


def upload(client: TestClient, ws: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()

    if name.endswith(".csv"):
        frame.write_csv(buffer)
    else:
        frame.write_parquet(buffer)

    buffer.seek(0)
    response = client.post(f"/api/workspaces/{ws}/datasets", files={"file": (name, buffer)})

    assert response.status_code in {200, 201}, response.text

    return response.json()["dataset_id"]


def put_steps(client: TestClient, ws: str, dataset_id: str, steps: list[dict]) -> None:
    response = client.put(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline",
        json={"steps": [{"step_id": f"s{index}", **step} for index, step in enumerate(steps, 1)]},
    )

    assert response.status_code == 200, response.text


def join(client: TestClient, ws: str, left: str, right: str, **spec: Any) -> str:
    payload = {
        "left_dataset_id": left,
        "right_dataset_id": right,
        "keys": [{"left": "id", "right": "id"}],
        "name": "сборка",
        **spec,
    }
    response = client.post(f"/api/workspaces/{ws}/joins", json=payload)

    assert response.status_code == 201, response.text

    return response.json()["dataset_id"]


def lineage(client: TestClient, ws: str, dataset_id: str) -> dict:
    response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/lineage")

    assert response.status_code == 200, response.text

    return response.json()


def columns_of(body: dict) -> dict[str, dict]:
    return {column["name"]: column for column in body["columns"]}


def history(column: dict) -> list[tuple[str, str, int]]:
    """История колонки в виде (датасет, операция, номер шага) — для сравнения порядка."""
    return [(item["dataset_name"], item["operation"], item["position"]) for item in column["events"]]


def historical(body: dict) -> dict:
    """Ответ без пометок о текущем состоянии родителей: только то, что обязано не меняться."""
    frozen = json.loads(json.dumps(body))

    for parent in frozen["parents"]:
        parent.pop("current")

    return frozen


def rewrite_provenance(dataset_id: str, document: str) -> None:
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute(
            "UPDATE dataset_provenance SET document = ? WHERE dataset_id = ?", (document, dataset_id)
        )
        connection.commit()


# ── идентичность колонки в конвейере ─────────────────────────────────────────


TEXT = pl.DataFrame({"id": [1, 2, 3], "a": [" x", "y ", "н/д"], "n": ["1", "2", "3"]})


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "cast_column", "params": {"column": "n", "to": "integer"}},
        {"kind": "fill_missing", "params": {"column": "n", "value": "0"}},
        {"kind": "trim_whitespace", "params": {"columns": ["n"]}},
        {"kind": "normalize_nulls", "params": {"columns": ["n"]}},
        {"kind": "replace_values", "params": {"column": "n", "from": "1", "to": "9"}},
        {"kind": "change_case", "params": {"columns": ["n"], "case": "upper"}},
        {"kind": "parse_number", "params": {"column": "n"}},
    ],
    ids=lambda step: step["kind"],
)
def test_a_value_changing_step_adds_exactly_one_event_to_its_column(
    client: TestClient, ws: str, step: dict
) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    put_steps(client, ws, dataset_id, [step])

    columns = columns_of(lineage(client, ws, dataset_id))

    assert [item["operation"] for item in columns["n"]["events"]] == [step["kind"]]
    assert columns["n"]["origin"] == "source"
    assert columns["a"]["events"] == [] and columns["id"]["events"] == []


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "filter_rows", "params": {"column": "n", "operator": "eq", "value": "1"}},
        {"kind": "drop_rows", "params": {"column": "n", "operator": "eq", "value": "1"}},
        {"kind": "sort_rows", "params": {"column": "n", "descending": True}},
        {"kind": "deduplicate", "params": {"columns": ["n"]}},
        {"kind": "reorder_columns", "params": {"order": ["n", "a", "id"]}},
    ],
    ids=lambda step: step["kind"],
)
def test_a_row_step_reads_a_column_without_becoming_its_history(
    client: TestClient, ws: str, step: dict
) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    put_steps(client, ws, dataset_id, [step])

    body = lineage(client, ws, dataset_id)
    columns = columns_of(body)

    assert all(column["events"] == [] for column in body["columns"])
    assert 1 in columns["n"]["read_by"]
    assert body["events"][0]["changes"] == []
    assert "n" in body["events"][0]["reads"]


def test_rename_keeps_the_origin_and_chains_through_names(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    put_steps(
        client,
        ws,
        dataset_id,
        [
            {"kind": "trim_whitespace", "params": {"columns": ["a"]}},
            {"kind": "rename_column", "params": {"column": "a", "to": "b"}},
            {"kind": "normalize_nulls", "params": {"columns": ["b"]}},
            {"kind": "rename_column", "params": {"column": "b", "to": "c"}},
        ],
    )

    column = columns_of(lineage(client, ws, dataset_id))["c"]

    assert column["origin"] == "source"
    assert [(ref["column"], ref["source_file"]) for ref in column["origins"]] == [("a", "т.parquet")]
    assert [item["position"] for item in column["events"]] == [1, 2, 3, 4]
    assert [(item["inputs"], item["output"]) for item in column["events"]] == [
        (["a"], "a"),
        (["a"], "b"),
        (["b"], "b"),
        (["b"], "c"),
    ]


def test_drop_ends_the_identity_and_a_new_namesake_starts_clean(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    put_steps(
        client,
        ws,
        dataset_id,
        [
            {"kind": "trim_whitespace", "params": {"columns": ["a"]}},
            {"kind": "rename_column", "params": {"column": "a", "to": "b"}},
            {"kind": "drop_columns", "params": {"columns": ["b"]}},
            {"kind": "duplicate_column", "params": {"column": "n", "to": "b"}},
            {"kind": "sort_rows", "params": {"column": "b"}},
        ],
    )

    columns = columns_of(lineage(client, ws, dataset_id))

    assert set(columns) == {"id", "n", "b"}
    assert columns["b"]["origin"] == "step"
    assert [ref["column"] for ref in columns["b"]["origins"]] == ["n"]
    #ни обрезки, ни переименования, ни удаления прежней «b» — только копирование
    assert [item["position"] for item in columns["b"]["events"]] == [4]
    assert columns["b"]["read_by"] == [5]


def test_mutation_before_and_after_a_rename_stays_in_step_order(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    put_steps(
        client,
        ws,
        dataset_id,
        [
            {"kind": "normalize_nulls", "params": {"columns": ["a"]}},
            {"kind": "rename_column", "params": {"column": "a", "to": "имя"}},
            {"kind": "trim_whitespace", "params": {"columns": ["имя"]}},
            {"kind": "fill_missing", "params": {"column": "имя", "value": "?"}},
        ],
    )

    column = columns_of(lineage(client, ws, dataset_id))["имя"]

    assert [item["operation"] for item in column["events"]] == [
        "normalize_nulls",
        "rename_column",
        "trim_whitespace",
        "fill_missing",
    ]


@pytest.mark.parametrize(
    "steps",
    [
        #удалить колонку дважды
        [
            {"kind": "drop_columns", "params": {"columns": ["a"]}},
            {"kind": "drop_columns", "params": {"columns": ["a"]}},
        ],
        #переименовать в уже занятое имя
        [{"kind": "rename_column", "params": {"column": "a", "to": "n"}}],
    ],
    ids=["drop-twice", "rename-onto-existing"],
)
def test_impossible_pipelines_are_refused_before_lineage_can_lie(
    client: TestClient, ws: str, steps: list[dict]
) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    response = client.put(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline",
        json={"steps": [{"step_id": f"s{i}", **step} for i, step in enumerate(steps, 1)]},
    )

    assert response.status_code == 422
    assert lineage(client, ws, dataset_id)["events"] == []


def test_awkward_names_survive_unchanged(client: TestClient, ws: str) -> None:
    long_name = "колонка_" + "ж" * 300
    frame = pl.DataFrame({"id": [1], "сумма_2": [1], long_name: ["x"], "名前": ["y"], "a b": [1]})
    dataset_id = upload(client, ws, frame, "имена.parquet")
    put_steps(client, ws, dataset_id, [{"kind": "rename_column", "params": {"column": long_name, "to": "короткое"}}])

    columns = columns_of(lineage(client, ws, dataset_id))

    assert columns["короткое"]["source_name"] == long_name
    assert columns["сумма_2"]["origins"][0]["column"] == "сумма_2"
    assert columns["名前"]["origin"] == "source"
    assert columns["a b"]["origin"] == "source"


def test_an_empty_dataset_and_an_empty_pipeline_are_an_answer_not_an_error(
    client: TestClient, ws: str
) -> None:
    frame = pl.DataFrame({"id": pl.Series([], dtype=pl.Int64), "a": pl.Series([], dtype=pl.Utf8)})
    dataset_id = upload(client, ws, frame, "пусто.parquet")

    body = lineage(client, ws, dataset_id)

    assert body["status"] == "known"
    assert body["events"] == []
    assert [column["origin"] for column in body["columns"]] == ["source", "source"]


# ── импорт ────────────────────────────────────────────────────────────────────


def test_every_import_writes_an_explicit_record_with_the_file(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "клиенты.csv")
    store = context.get_workspace_store()

    with store._connect() as connection:
        document = json.loads(
            connection.execute(
                "SELECT document FROM dataset_provenance WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()[0]
        )

    dataset = store.get_dataset(dataset_id)

    assert document["status"] == "known"
    assert document["origin"] == "upload"
    assert document["source"] == {
        "file_name": "клиенты.csv",
        "format": "csv",
        "sha256": dataset.source.sha256,
        "bytes": dataset.source.bytes,
    }

    column = columns_of(lineage(client, ws, dataset_id))["a"]

    assert column["origins"] == [
        {
            "dataset_id": dataset_id,
            "dataset_name": "клиенты.csv",
            "column": "a",
            "source_file": "клиенты.csv",
            "source_sha256": dataset.source.sha256,
        }
    ]


# ── сборка ────────────────────────────────────────────────────────────────────


CARDINALITY = {
    #1:1 — внутреннее
    "inner": (pl.DataFrame({"id": [1, 2], "x": [1, 2]}), pl.DataFrame({"id": [1, 2], "y": [3, 4]})),
    #1:N — левое
    "left": (pl.DataFrame({"id": [1, 2], "x": [1, 2]}), pl.DataFrame({"id": [1, 1, 2], "y": [3, 4, 5]})),
    #N:1 — правое
    "right": (pl.DataFrame({"id": [1, 1, 2], "x": [1, 2, 3]}), pl.DataFrame({"id": [1, 2], "y": [4, 5]})),
    #N:M — полное
    "full": (pl.DataFrame({"id": [1, 1, 3], "x": [1, 2, 3]}), pl.DataFrame({"id": [1, 1, 4], "y": [4, 5, 6]})),
}


@pytest.mark.parametrize("kind", list(CARDINALITY))
def test_cardinality_does_not_change_which_parent_a_column_came_from(
    client: TestClient, ws: str, kind: str
) -> None:
    left_frame, right_frame = CARDINALITY[kind]
    left = upload(client, ws, left_frame, "лево.parquet")
    right = upload(client, ws, right_frame, "право.parquet")
    child = join(client, ws, left, right, kind=kind, right_suffix="_п")

    body = lineage(client, ws, child)
    columns = columns_of(body)

    assert body["join"]["kind"] == kind
    assert columns["x"]["origins"][0]["source_file"] == "лево.parquet"
    assert columns["y"]["origins"][0]["source_file"] == "право.parquet"
    assert all(column["origin"] != "unknown" for column in body["columns"])
    #каждая колонка сборки заканчивает историю соединением
    assert all(column["events"][-1]["operation"] == "join" for column in body["columns"])


def test_composite_keys_and_the_suffix_map_are_recorded_exactly(client: TestClient, ws: str) -> None:
    left = upload(
        client,
        ws,
        pl.DataFrame({"a": [1, 2], "b": ["x", "y"], "status": ["l1", "l2"], "total_2": [5, 6]}),
        "лево.parquet",
    )
    right = upload(
        client,
        ws,
        pl.DataFrame({"x": [1, 2], "y": ["x", "y"], "status": ["r1", "r2"], "only_right": [7, 8]}),
        "право.parquet",
    )
    child = join(
        client,
        ws,
        left,
        right,
        keys=[{"left": "a", "right": "x"}, {"left": "b", "right": "y"}],
        right_suffix="_right",
    )

    body = lineage(client, ws, child)
    columns = columns_of(body)
    mapping = {item["name"]: (item["side"], item["source"]) for item in body["join"]["columns"]}

    assert body["join"]["keys"] == [{"left": "a", "right": "x"}, {"left": "b", "right": "y"}]
    assert mapping["status"] == ("left", "status")
    assert mapping["status_right"] == ("right", "status")
    assert mapping["only_right"] == ("right", "only_right")
    #окончание имени не делает левую колонку правой
    assert mapping["total_2"] == ("left", "total_2")
    assert columns["status_right"]["origins"][0]["column"] == "status"
    assert columns["status_right"]["origins"][0]["source_file"] == "право.parquet"
    assert columns["total_2"]["origins"][0]["source_file"] == "лево.parquet"


def test_null_keys_do_not_disturb_the_column_map(client: TestClient, ws: str) -> None:
    left = upload(client, ws, pl.DataFrame({"id": [1, None, 3], "x": [1, 2, 3]}), "лево.parquet")
    right = upload(client, ws, pl.DataFrame({"id": [1, None], "y": [4, 5]}), "право.parquet")
    child = join(client, ws, left, right, kind="left")

    columns = columns_of(lineage(client, ws, child))

    assert columns["id"]["origins"][0]["source_file"] == "лево.parquet"
    assert columns["y"]["origins"][0]["source_file"] == "право.parquet"


def build_family(client: TestClient, ws: str) -> tuple[str, str, str]:
    """Родитель с конвейером, соединение, конвейер ребёнка: полная цепочка из задачи."""
    customers = upload(
        client,
        ws,
        pl.DataFrame({"id": [1, 2], "email": [" A@x ", None], "status": ["new", "old"]}),
        "customers.csv",
    )
    orders = upload(
        client,
        ws,
        pl.DataFrame({"id": [1, 2], "amount": ["10", "20"], "status": ["paid", "open"]}),
        "orders.parquet",
    )
    put_steps(
        client,
        ws,
        customers,
        [
            {"kind": "trim_whitespace", "params": {"columns": ["email"]}},
            {"kind": "fill_missing", "params": {"column": "email", "value": "?"}},
        ],
    )
    put_steps(client, ws, orders, [{"kind": "cast_column", "params": {"column": "amount", "to": "integer"}}])
    child = join(client, ws, customers, orders, kind="left", right_suffix="_right", name="customer_orders")
    put_steps(
        client,
        ws,
        child,
        [
            {"kind": "sort_rows", "params": {"column": "amount"}},
            {"kind": "change_case", "params": {"columns": ["email"], "case": "lower"}},
        ],
    )

    return customers, orders, child


def test_a_column_history_runs_from_the_parent_file_through_the_join(client: TestClient, ws: str) -> None:
    _, _, child = build_family(client, ws)

    body = lineage(client, ws, child)
    columns = columns_of(body)

    assert history(columns["email"]) == [
        ("customers.csv", "trim_whitespace", 1),
        ("customers.csv", "fill_missing", 2),
        ("customer_orders.parquet", "join", 0),
        ("customer_orders.parquet", "change_case", 2),
    ]
    assert columns["email"]["origins"][0]["source_file"] == "customers.csv"
    assert history(columns["amount"]) == [
        ("orders.parquet", "cast_column", 1),
        ("customer_orders.parquet", "join", 0),
    ]
    #сортировка ребёнка по сумме — чтение, не преобразование
    assert columns["amount"]["read_by"] == [1]
    assert columns["status_right"]["origins"][0]["column"] == "status"
    assert columns["status"]["origins"][0]["source_file"] == "customers.csv"


def test_changing_a_parent_after_the_join_does_not_rewrite_the_child(client: TestClient, ws: str) -> None:
    customers, orders, child = build_family(client, ws)
    before = lineage(client, ws, child)

    put_steps(
        client,
        ws,
        customers,
        [
            {"kind": "change_case", "params": {"columns": ["email"], "case": "upper"}},
            {"kind": "rename_column", "params": {"column": "email", "to": "почта"}},
        ],
    )
    put_steps(client, ws, orders, [])

    after = lineage(client, ws, child)

    assert historical(after) == historical(before)
    assert [parent["current"]["changed_since_join"] for parent in after["parents"]] == [True, True]
    assert [parent["current"]["changed_since_join"] for parent in before["parents"]] == [False, False]


def test_deleting_the_parents_does_not_destroy_the_child_history(client: TestClient, ws: str) -> None:
    customers, orders, child = build_family(client, ws)
    before = lineage(client, ws, child)
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute("DELETE FROM datasets WHERE dataset_id IN (?, ?)", (customers, orders))
        connection.commit()

    after = lineage(client, ws, child)

    assert historical(after) == historical(before)
    assert [parent["current"] for parent in after["parents"]] == [
        {"available": False, "changed_since_join": None},
        {"available": False, "changed_since_join": None},
    ]


def test_a_grandchild_keeps_the_whole_chain_after_every_ancestor_is_gone(client: TestClient, ws: str) -> None:
    customers, orders, child = build_family(client, ws)
    extra = upload(client, ws, pl.DataFrame({"id": [1, 2], "note": ["a", "b"]}), "notes.parquet")
    grandchild = join(client, ws, child, extra, name="полная")
    before = lineage(client, ws, grandchild)
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute(
            "DELETE FROM datasets WHERE dataset_id IN (?, ?, ?)", (customers, orders, child)
        )
        connection.commit()

    after = columns_of(lineage(client, ws, grandchild))

    assert historical(lineage(client, ws, grandchild)) == historical(before)
    assert [item["operation"] for item in after["email"]["events"]] == [
        "trim_whitespace",
        "fill_missing",
        "join",
        "change_case",
        "join",
    ]
    assert after["email"]["origins"][0]["source_file"] == "customers.csv"


def test_a_parent_changed_during_the_join_is_refused(
    client: TestClient, ws: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гонка: конвейер родителя сохранили, пока соединение писало результат.

    Снимок описывал бы одно состояние, данные — другое. Сборка обязана отказать.
    """
    from backend.domain.pipeline.models import Pipeline, Step, StepKind
    from backend.services.joins import workspace as joins_workspace

    left = upload(client, ws, pl.DataFrame({"id": [1], "a": [" x"]}), "лево.parquet")
    right = upload(client, ws, pl.DataFrame({"id": [1], "b": [1]}), "право.parquet")
    original = joins_workspace.result_of

    def racing(*args: Any, **kwargs: Any) -> pl.LazyFrame:
        store = context.get_workspace_store()
        store.save_pipeline(
            Pipeline(
                dataset_id=left,
                steps=(Step("s1", StepKind.TRIM_WHITESPACE, {"columns": ["a"]}),),
            )
        )

        return original(*args, **kwargs)

    monkeypatch.setattr(joins_workspace, "result_of", racing)
    response = client.post(
        f"/api/workspaces/{ws}/joins",
        json={"left_dataset_id": left, "right_dataset_id": right, "keys": [{"left": "id", "right": "id"}], "name": "гонка"},
    )

    assert response.status_code == 422, response.text
    assert "изменился во время сборки" in response.json()["error"]["message"]


# ── хранение ──────────────────────────────────────────────────────────────────


def test_a_restart_changes_nothing(client: TestClient, ws: str, tmp_path: Path) -> None:
    _, _, child = build_family(client, ws)
    before = lineage(client, ws, child)

    context.reset_context()
    config.get_settings.cache_clear()
    restarted = TestClient(create_app())

    assert lineage(restarted, ws, child) == before


def test_a_missing_record_is_unknown_not_an_upload(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute("DELETE FROM dataset_provenance WHERE dataset_id = ?", (dataset_id,))
        connection.commit()

    body = lineage(client, ws, dataset_id)

    assert body["status"] == "legacy_unknown"
    assert body["unknown_reason"] == "missing_record"
    assert body["origin"] is None
    assert body["parents"] == []
    assert body["warnings"] == ["provenance_unknown"]
    assert all(column["origin"] == "unknown" and column["origins"] == [] for column in body["columns"])


CORRUPTED = {
    "not-json": "{oops",
    "not-an-object": "[1, 2, 3]",
    "unknown-origin": json.dumps({"status": "known", "origin": "teleport"}),
    "join-without-keys": json.dumps(
        {"origin": "join", "join": {"left_dataset_id": "ds_x", "right_dataset_id": "ds_y", "kind": "inner", "right_suffix": "_2", "keys": []}}
    ),
    "wrong-types": json.dumps({"status": "known", "origin": "upload", "source": {"file_name": 1}}),
    "bad-side": json.dumps(
        {
            "origin": "join",
            "join": {
                "left_dataset_id": "ds_x",
                "right_dataset_id": "ds_y",
                "kind": "inner",
                "right_suffix": "_2",
                "keys": [{"left": "id", "right": "id"}],
                "columns": [{"name": "id", "side": "middle", "source": "id"}],
            },
        }
    ),
    "unknown-with-details": json.dumps({"status": "legacy_unknown", "unknown_reason": "nope"}),
    "deep-nesting": "[" * 100_000 + "]" * 100_000,
    "bool-as-number": json.dumps({"status": "known", "origin": "upload", "source": {"file_name": "a", "format": "csv", "sha256": "0", "bytes": True}}),
}


@pytest.mark.parametrize("document", list(CORRUPTED.values()), ids=list(CORRUPTED))
def test_a_corrupted_record_degrades_to_unknown_instead_of_a_500(
    client: TestClient, ws: str, document: str
) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    rewrite_provenance(dataset_id, document)

    body = lineage(client, ws, dataset_id)

    assert body["status"] == "legacy_unknown"
    assert body["unknown_reason"] in {"corrupted_record", "oversized_record"}
    assert all(column["origin"] == "unknown" for column in body["columns"])
    #пакет строится из того же ответа и тоже не падает
    assert client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/package").status_code == 200


def test_an_oversized_record_is_not_parsed(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    rewrite_provenance(
        dataset_id,
        json.dumps({"status": "known", "origin": "upload", "padding": "x" * (9 * 1024 * 1024)}),
    )

    body = lineage(client, ws, dataset_id)

    assert body["unknown_reason"] == "oversized_record"


def test_a_corrupted_parent_id_in_a_join_record_is_not_a_500(client: TestClient, ws: str) -> None:
    left = upload(client, ws, pl.DataFrame({"id": [1], "a": [1]}), "лево.parquet")
    right = upload(client, ws, pl.DataFrame({"id": [1], "b": [1]}), "право.parquet")
    child = join(client, ws, left, right)
    store = context.get_workspace_store()

    with store._connect() as connection:
        document = json.loads(
            connection.execute(
                "SELECT document FROM dataset_provenance WHERE dataset_id = ?", (child,)
            ).fetchone()[0]
        )

    document["join"]["left_dataset_id"] = "../../etc/passwd"
    rewrite_provenance(child, json.dumps(document))

    body = lineage(client, ws, child)

    assert body["parents"][0]["current"]["available"] is False


def migrated_store(tmp_path: Path, version: int) -> tuple[WorkspaceStore, dict[str, str]]:
    """Хранилище, в котором записи о происхождении нет, как у базы прежней версии."""
    path = tmp_path / "state.db"
    store = WorkspaceStore(path)
    workspace = store.create_workspace(new_workspace_id(), "Миграция")
    from backend.domain.lineage.models import Provenance, UploadSource
    from tests.unit.test_workspace_store import make_dataset

    ids = {}

    for fmt, sha in (("csv", "a"), ("parquet", "b")):
        dataset = make_dataset(workspace.workspace_id, sha256=sha * 64)
        dataset.source = type(dataset.source)(
            file_name=f"f.{fmt}", format=fmt, bytes=10, sha256=sha * 64
        )
        store.add_dataset(
            dataset,
            Provenance.upload(UploadSource("x", fmt, sha * 64, 10)),
            content_addressed=True,
        )
        ids[fmt] = dataset.dataset_id

    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM dataset_provenance")
    connection.execute("UPDATE schema_version SET version = ?", (version,))
    connection.commit()
    connection.close()

    return WorkspaceStore(path), ids


def test_migration_from_before_joins_records_every_dataset_as_an_upload(tmp_path: Path) -> None:
    store, ids = migrated_store(tmp_path, 3)

    for fmt, dataset_id in ids.items():
        provenance = store.get_provenance(dataset_id)

        assert provenance.is_upload
        assert provenance.source is not None and provenance.source.file_name == f"f.{fmt}"


def test_migration_after_joins_does_not_invent_the_origin_of_a_parquet(tmp_path: Path) -> None:
    #Parquet из базы, где соединения уже были, мог быть и загрузкой, и сборкой
    store, ids = migrated_store(tmp_path, 6)

    assert store.get_provenance(ids["csv"]).is_upload
    assert store.get_provenance(ids["parquet"]).status.value == "legacy_unknown"


# ── пакет ─────────────────────────────────────────────────────────────────────


def load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("reference_validator", REFERENCE / "validate_package.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["reference_validator"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    return module


def package_bytes(client: TestClient, ws: str, dataset_id: str) -> bytes:
    response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/package")

    assert response.status_code == 200, response.text

    return response.content


#отметки времени сборки: это событие, а не содержимое, и контракт разрешает им отличаться
_BUILD_TIME = {"created_at", "computed_at"}


def stable(content: bytes) -> dict[str, Any]:
    """Пакет без отметок времени сборки — то, что обязано совпадать между сборками."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if key not in _BUILD_TIME}

        if isinstance(value, list):
            return [strip(item) for item in value]

        return value

    result: dict[str, Any] = {}

    with zipfile.ZipFile(io.BytesIO(content)) as package:
        for name in sorted(package.namelist()):
            raw = package.read(name)
            result[name] = strip(json.loads(raw)) if name.endswith(".json") else raw

    #хэши и размеры файлов с отметкой времени зависят от неё
    for part in result["manifest.json"].get("parts", []):
        if part.get("path") in {"profile.json", "lineage.json"}:
            part.pop("sha256", None)
            part.pop("bytes", None)

    return result


def package_lineage(content: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        return json.loads(package.read("lineage.json"))


def test_a_joined_package_passes_the_reference_validator(client: TestClient, ws: str, tmp_path: Path) -> None:
    """Прежде родители писались в `sources` без `format` и `sha256`, обязательных по схеме."""
    _, _, child = build_family(client, ws)
    unpacked = tmp_path / "unpacked"

    with zipfile.ZipFile(io.BytesIO(package_bytes(client, ws, child))) as package:
        package.extractall(unpacked)

    report = load_validator().validate_package(unpacked)

    assert report.errors == [], report.errors


def test_the_package_tells_the_same_story_as_the_api(client: TestClient, ws: str) -> None:
    _, _, child = build_family(client, ws)
    body = lineage(client, ws, child)
    document = package_lineage(package_bytes(client, ws, child))
    by_id = {source["id"]: source for source in document["sources"]}

    for parent in body["parents"]:
        source = by_id[f"src_{parent['dataset_id']}"]

        assert source["sha256"] == parent["snapshot"]["artifact_sha256"]
        assert source["name"] == parent["dataset_name"]

    nodes = [node["op"] for node in document["graph"]]
    #родители в состоянии на момент сборки, затем соединение, чтение результата и его шаги
    assert nodes == ["read", "trim_whitespace", "fill_missing", "read", "cast_column", "join", "read", "sort_rows", "change_case"]
    assert [node["op"] for node in document["graph"][-len(body["events"]):]] == [
        event["operation"] for event in body["events"]
    ]


def test_the_child_package_ignores_later_parent_changes_and_is_deterministic(client: TestClient, ws: str) -> None:
    customers, _, child = build_family(client, ws)
    first = stable(package_bytes(client, ws, child))

    assert stable(package_bytes(client, ws, child)) == first, "две сборки одного пакета разошлись"

    put_steps(client, ws, customers, [])

    assert stable(package_bytes(client, ws, child)) == first, "пакет ребёнка пересказал нынешнего родителя"

    put_steps(client, ws, child, [{"kind": "sort_rows", "params": {"column": "amount"}}])
    changed = package_lineage(package_bytes(client, ws, child))

    assert [node["op"] for node in changed["graph"]][-2:] == ["read", "sort_rows"]


def test_an_unknown_dataset_packages_with_an_honest_note(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute("DELETE FROM dataset_provenance WHERE dataset_id = ?", (dataset_id,))
        connection.commit()

    document = package_lineage(package_bytes(client, ws, dataset_id))

    assert "неизвестно" in document["graph"][0]["note"]


# ── находки враждебного разбора ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "document",
    ['{"steps": [1]}', '{"steps": "abc"}', '{"undo": [[1]]}', '{"undo": [5]}'],
    ids=["step-not-object", "steps-not-list", "undo-step-not-object", "undo-state-not-list"],
)
def test_a_corrupted_pipeline_record_is_a_pipeline_error_not_a_500(
    client: TestClient, ws: str, document: str
) -> None:
    """Прежде такие записи давали AttributeError и TypeError — то есть пятисотку."""
    dataset_id = upload(client, ws, TEXT, "т.parquet")
    put_steps(client, ws, dataset_id, [{"kind": "trim_whitespace", "params": {"columns": ["a"]}}])
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute(
            "UPDATE dataset_pipelines SET document = ? WHERE dataset_id = ?", (document, dataset_id)
        )
        connection.commit()

    response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/lineage")

    assert response.status_code == 422, response.text


def test_a_corrupted_parent_pipeline_does_not_break_the_child(client: TestClient, ws: str) -> None:
    #история ребёнка держится на снимке; испорченный конвейер родителя лишает его только
    #пометки «изменён после сборки», и то честно: сравнить не с чем
    customers, _, child = build_family(client, ws)
    before = lineage(client, ws, child)
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute(
            "UPDATE dataset_pipelines SET document = ? WHERE dataset_id = ?", ("{oops", customers)
        )
        connection.commit()

    after = lineage(client, ws, child)

    assert historical(after) == historical(before)
    assert after["parents"][0]["current"] == {"available": True, "changed_since_join": None}


def test_a_refused_join_leaves_no_staging_files(
    client: TestClient, ws: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Сборка, отказавшая после записи результата, не оставляет файлов во временном каталоге."""
    import tempfile

    from backend.services.joins import workspace as joins_workspace

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(staging_root))
    left = upload(client, ws, pl.DataFrame({"id": [1], "a": [1]}), "лево.parquet")
    right = upload(client, ws, pl.DataFrame({"id": [1], "b": [1]}), "право.parquet")

    def refuse(*_: Any) -> None:
        raise joins_workspace.JoinError("отказ после записи результата")

    monkeypatch.setattr(joins_workspace, "_require_unchanged", refuse)
    response = client.post(
        f"/api/workspaces/{ws}/joins",
        json={
            "left_dataset_id": left,
            "right_dataset_id": right,
            "keys": [{"left": "id", "right": "id"}],
            "name": "сборка",
        },
    )

    assert response.status_code == 422
    assert list(staging_root.iterdir()) == []
