"""Регрессии RC-аудита конвейера: шаги, которые молча портили данные или давали пятисотку.

Первый тест — матрица «операция × тип колонки» на настоящих данных: ни одно сочетание
не должно отвечать пятисоткой, а шаг, принятый при сохранении, обязан выполняться.
Прежде таких сочетаний было 82. Остальные тесты — смысловые: что именно делает шаг.
"""

from __future__ import annotations

import datetime as dt
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
from backend.services.pipeline.operations import CAST_TARGETS


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    root = tmp_path_factory.mktemp("workspaces")
    patch = pytest.MonkeyPatch()
    patch.setenv("DATAARENA_WORKSPACE_ROOT", str(root))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app(), raise_server_exceptions=False)

    patch.undo()
    config.get_settings.cache_clear()
    context.reset_context()


@pytest.fixture(scope="module")
def ws(client: TestClient) -> str:
    return client.post("/api/workspaces", json={"name": "Конвейер"}).json()["workspace_id"]


def upload(client: TestClient, ws: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    buffer.seek(0)
    response = client.post(f"/api/workspaces/{ws}/datasets", files={"file": (name, buffer)})

    assert response.status_code in {200, 201}, response.text

    return response.json()["dataset_id"]


def put(client: TestClient, ws: str, dataset_id: str, steps: list[dict]) -> Any:
    return client.put(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline",
        json={"steps": [{"step_id": f"s{index}", **step} for index, step in enumerate(steps, 1)]},
    )


def rows(client: TestClient, ws: str, dataset_id: str) -> list[dict]:
    response = client.get(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline/rows", params={"limit": 100}
    )

    assert response.status_code == 200, response.text

    return response.json()["rows"]


def applied(client: TestClient, ws: str, frame: pl.DataFrame, step: dict, name: str = "t.parquet") -> list[dict]:
    dataset_id = upload(client, ws, frame, name)
    response = put(client, ws, dataset_id, [step])

    assert response.status_code == 200, response.text

    return rows(client, ws, dataset_id)


def refused(client: TestClient, ws: str, frame: pl.DataFrame, step: dict, name: str = "t.parquet") -> str:
    dataset_id = upload(client, ws, frame, name)
    response = put(client, ws, dataset_id, [step])

    assert response.status_code == 422, response.text

    return response.json()["error"]["message"]


# ── матрица ──────────────────────────────────────────────────────────────────


MATRIX = pl.DataFrame(
    {
        "i8": pl.Series([1, -128, None, 127], dtype=pl.Int8),
        "u64": pl.Series([1, 2**63 + 5, None, 0], dtype=pl.UInt64),
        "f": [1.5, float("nan"), None, float("inf")],
        "b": [True, False, None, True],
        "s": [" 1,5 ", "", None, "  "],
        "d": [dt.date(2024, 1, 1), None, dt.date(1900, 1, 1), dt.date(2099, 12, 31)],
        "dtz": pl.Series(
            [dt.datetime(2024, 1, 1, 12), None, dt.datetime(2024, 1, 2), dt.datetime(1970, 1, 1)]
        ).dt.replace_time_zone("Europe/Moscow"),
        "cat": pl.Series(["a ", "B", None, "a "], dtype=pl.Categorical),
    }
)


def matrix_steps(column: str) -> list[dict]:
    return [
        *({"kind": "cast_column", "params": {"column": column, "to": target}} for target in CAST_TARGETS),
        *(
            {"kind": kind, "params": {"column": column, "operator": operator, "value": value}}
            for kind in ("filter_rows", "drop_rows")
            for operator in ("eq", "gt", "contains")
            for value in ("1", "2024-01-01", "true")
        ),
        {"kind": "sort_rows", "params": {"column": column, "descending": True}},
        {"kind": "replace_values", "params": {"column": column, "from": "1", "to": "2"}},
        {"kind": "replace_values", "params": {"column": column, "from": "a", "to": "b", "mode": "substring"}},
        *({"kind": "fill_missing", "params": {"column": column, "value": value}} for value in ("0", "2024-01-01", "x")),
        {"kind": "normalize_nulls", "params": {"columns": [column]}},
        {"kind": "deduplicate", "params": {"columns": [column]}},
        {"kind": "trim_whitespace", "params": {"columns": [column]}},
        {"kind": "change_case", "params": {"columns": [column], "case": "upper"}},
        {"kind": "parse_number", "params": {"column": column}},
        {"kind": "parse_datetime", "params": {"column": column}},
    ]


@pytest.mark.parametrize("column", MATRIX.columns)
def test_rc06_no_step_on_any_column_type_answers_with_a_500(client: TestClient, ws: str, column: str) -> None:
    dataset_id = upload(client, ws, MATRIX, "матрица.parquet")
    failures = []

    for step in matrix_steps(column):
        put(client, ws, dataset_id, [])
        impact = client.post(
            f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline/impact",
            json={"step": {"step_id": "x", **step}},
        )
        saved = put(client, ws, dataset_id, [step])
        result = (
            client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline/rows", params={"limit": 10})
            if saved.status_code == 200
            else None
        )
        codes = (impact.status_code, saved.status_code, result.status_code if result else None)

        #оценка и сохранение соглашаются; принятый шаг выполняется; пятисоток нет
        if 500 in codes or impact.status_code != saved.status_code or (result is not None and result.status_code != 200):
            failures.append((step, codes))

    assert failures == []


# ── смысл шагов ──────────────────────────────────────────────────────────────


AMOUNTS = pl.DataFrame({"id": [1, 2, 3, 4], "amount": [5.0, None, 200.0, float("nan")]})


def test_rc07_drop_rows_removes_only_rows_where_the_condition_is_true(client: TestClient, ws: str) -> None:
    """«Удалить, где сумма > 100» удаляло и строки с пропуском, и NaN."""
    kept = applied(client, ws, AMOUNTS, {"kind": "drop_rows", "params": {"column": "amount", "operator": "gt", "value": "100"}})

    assert [row["id"] for row in kept] == [1, 2, 4]


def test_rc07_filter_does_not_treat_nan_as_greater_than_everything(client: TestClient, ws: str) -> None:
    kept = applied(client, ws, AMOUNTS, {"kind": "filter_rows", "params": {"column": "amount", "operator": "gt", "value": "100"}})

    assert [row["id"] for row in kept] == [3]


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "filter_rows", "params": {"column": "amount", "operator": "gt", "value": "abc"}},
        {"kind": "filter_rows", "params": {"column": "amount", "operator": "eq", "value": "abc"}},
        {"kind": "drop_rows", "params": {"column": "amount", "operator": "lt", "value": "десять"}},
        {"kind": "fill_missing", "params": {"column": "amount", "value": "abc"}},
    ],
    ids=["filter-gt", "filter-eq", "drop-lt", "fill"],
)
def test_rc08_an_unparseable_value_is_refused_instead_of_becoming_null(
    client: TestClient, ws: str, step: dict
) -> None:
    """Прежде «> abc» удаляло все строки, «= abc» находило пропуски, а заполнение не делало ничего."""
    assert "не подходит" in refused(client, ws, AMOUNTS, step)


@pytest.mark.parametrize(
    ("frame", "step"),
    [
        (pl.DataFrame({"flag": [True, False, None]}), {"kind": "replace_values", "params": {"column": "flag", "from": "true", "to": "false"}}),
        (
            pl.DataFrame({"ts": pl.Series([dt.datetime(2024, 1, 1)]).dt.replace_time_zone("UTC")}),
            {"kind": "replace_values", "params": {"column": "ts", "from": "x", "to": "y"}},
        ),
        (pl.DataFrame({"s": ["abc"]}), {"kind": "replace_values", "params": {"column": "s", "from": "", "to": "X", "mode": "substring"}}),
        (pl.DataFrame({"n": [1, 2]}), {"kind": "replace_values", "params": {"column": "n", "from": "1", "to": "два"}}),
        (pl.DataFrame({"n": [1, 2]}), {"kind": "sort_rows", "params": {"column": "n", "descending": "false"}}),
    ],
    ids=["bool-was-500", "tz-was-nulled", "empty-substring-was-XaXbXcX", "number-to-text", "descending-string"],
)
def test_rc09_steps_that_corrupted_data_are_refused(client: TestClient, ws: str, frame: pl.DataFrame, step: dict) -> None:
    refused(client, ws, frame, step)


def test_rc09_replacing_a_number_compares_numbers_not_their_text(client: TestClient, ws: str) -> None:
    """Через текст «1» не находило 1.0, и замена молча ничего не делала."""
    result = applied(client, ws, pl.DataFrame({"x": [1.0, 2.5]}), {"kind": "replace_values", "params": {"column": "x", "from": "1", "to": "10"}})

    assert [row["x"] for row in result] == [10.0, 2.5]


def test_rc10_text_to_boolean_is_parsed_by_words(client: TestClient, ws: str) -> None:
    """Прежде приведение текста к логическому давало пятисотку на первой строке данных."""
    result = applied(
        client,
        ws,
        pl.DataFrame({"s": ["да", "No", " TRUE ", "0", "может быть", None]}),
        {"kind": "cast_column", "params": {"column": "s", "to": "boolean"}},
    )

    assert [row["s"] for row in result] == [True, False, True, False, None, None]


def test_rc10_a_timezone_column_is_compared_in_its_own_zone(client: TestClient, ws: str) -> None:
    """Сравнение с датой из формы давало пятисотку: наивное значение против значения с поясом."""
    frame = pl.DataFrame(
        {"ts": pl.Series([dt.datetime(2024, 1, 1, 0, 30), dt.datetime(2024, 1, 2, 12)]).dt.replace_time_zone("Europe/Moscow")}
    )
    result = applied(client, ws, frame, {"kind": "filter_rows", "params": {"column": "ts", "operator": "gt", "value": "2024-01-01 01:00"}})

    assert len(result) == 1


def test_rc11_cleaning_a_categorical_column_keeps_it_categorical(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, pl.DataFrame({"c": pl.Series([" a", "b "], dtype=pl.Categorical)}), "к.parquet")
    response = put(client, ws, dataset_id, [{"kind": "trim_whitespace", "params": {"columns": ["c"]}}])

    assert response.status_code == 200, response.text
    assert response.json()["result_types"]["c"] == "Categorical"
    assert [row["c"] for row in rows(client, ws, dataset_id)] == ["a", "b"]


def test_rc12_parse_number_understands_both_thousand_separators(client: TestClient, ws: str) -> None:
    """«1,234.56» и «1.234,56» становились пропусками; одна запятая остаётся десятичной."""
    result = applied(
        client,
        ws,
        pl.DataFrame({"n": ["1,234.56", "1.234,56", "1 200,5", "1,5", "1,234,567", "1.234.567", "12.5", "abc"]}),
        {"kind": "parse_number", "params": {"column": "n"}},
    )

    assert [row["n"] for row in result] == [1234.56, 1234.56, 1200.5, 1.5, 1234567.0, 1234567.0, 12.5, None]


def test_rc13_dates_in_mixed_records_are_parsed_value_by_value(client: TestClient, ws: str) -> None:
    """Формат угадывался по первому значению: остальные записи становились пропусками,
    а неугадываемый формат ронял весь запрос."""
    result = applied(
        client,
        ws,
        pl.DataFrame({"d": ["2024-01-31", "01.02.2024", "не дата", " 03/02/2024 "]}),
        {"kind": "cast_column", "params": {"column": "d", "to": "date"}},
    )

    assert [row["d"] for row in result] == ["2024-01-31", "2024-02-01", None, "2024-02-03"]


def test_rc14_impact_with_parameters_of_the_wrong_type_is_a_422(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, AMOUNTS, "оценка.parquet")

    for step in (
        {"kind": "deduplicate", "params": {"columns": 5}},
        {"kind": "normalize_nulls", "params": {"columns": ["amount"], "tokens": 5}},
    ):
        response = client.post(
            f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline/impact",
            json={"step": {"step_id": "x", **step}},
        )

        assert response.status_code == 422, response.text


def test_rc15_the_source_file_never_changes(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, AMOUNTS, "неприкосновенный.parquet")
    root = Path(config.get_settings().workspace_root)
    source = next(root.rglob(f"sources/{dataset_id}.*"))
    before = source.read_bytes()

    put(client, ws, dataset_id, [
        {"kind": "drop_rows", "params": {"column": "amount", "operator": "gt", "value": "1"}},
        {"kind": "rename_column", "params": {"column": "amount", "to": "сумма"}},
        {"kind": "deduplicate", "params": {}},
    ])
    rows(client, ws, dataset_id)
    client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/export", params={"format": "csv"})

    assert source.read_bytes() == before
