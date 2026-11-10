"""Выгрузка: форматы, круговой путь, формулы и предел строк.

Главные обещания раздела: выгружается результат конвейера, типы и пропуски переживают
круг там, где формат это позволяет, а значения-формулы не выполняются при открытии файла.
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
from backend.services.export import safe_file_name

FRAME = pl.DataFrame(
    {
        "город": ["Москва", "Казань", None],
        "сумма": [100.5, None, -3.25],
        "флаг": [True, False, True],
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


@pytest.fixture
def dataset(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/workspaces", json={"name": "Выгрузка"}).json()["workspace_id"]
    buffer = io.BytesIO()
    FRAME.write_parquet(buffer)
    buffer.seek(0)
    dataset_id = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("data.parquet", buffer)}
    ).json()["dataset_id"]

    return workspace_id, dataset_id


def test_parquet_survives_the_round_trip_with_types_and_gaps(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    """Круговой путь — единственная честная проверка выгрузки."""
    workspace_id, dataset_id = dataset
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/export?format=parquet"
    )

    assert response.status_code == 200
    assert "parquet" in response.headers["content-type"]

    restored = pl.read_parquet(io.BytesIO(response.content))

    assert restored.equals(FRAME), "типы или пропуски не пережили выгрузку"


def test_the_export_contains_the_result_of_the_pipeline(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    #выгружать исходник, когда на экране результат шагов, — значит отдать не то,
    #что человек видит
    workspace_id, dataset_id = dataset
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {"step_id": "s1", "kind": "drop_columns", "params": {"columns": ["флаг"]}}
            ]
        },
    )

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/export?format=parquet"
    )
    restored = pl.read_parquet(io.BytesIO(response.content))

    assert restored.columns == ["город", "сумма"]


def test_a_value_that_looks_like_a_formula_is_not_executed_by_a_spreadsheet(
    client: TestClient,
) -> None:
    """Значение с «=» в CSV — это исполняемая формула при открытии в Excel.

    Защищаться обязан тот, кто файл создаёт: открывающий его человек о подмене
    не узнает, пока формула не выполнится на его машине.
    """
    workspace_id = client.post("/api/workspaces", json={"name": "Формулы"}).json()["workspace_id"]
    buffer = io.BytesIO()
    pl.DataFrame(
        {"поле": ["=1+1", "+cmd|' /c calc'!A0", "@SUM(A1)", "-5", "обычное"]}
    ).write_parquet(buffer)
    buffer.seek(0)
    dataset_id = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("f.parquet", buffer)}
    ).json()["dataset_id"]

    text = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/export?format=csv"
    ).content.decode("utf-8")

    lines = [line.strip('"') for line in text.splitlines()[1:]]

    assert all(not line.lstrip('"').startswith(("=", "+", "@")) for line in lines), text
    assert "'=1+1" in text
    assert "обычное" in text, "обычные значения не должны меняться"


def test_a_format_with_a_row_limit_refuses_instead_of_truncating(tmp_path: Path) -> None:
    """Обрезать молча — худшее: пользователь унесёт файл, считая его полным.

    Предел здесь задаётся маленьким намеренно. Писать миллион строк ради проверки
    поведения не нужно: проверяется отказ, а не само число.
    """
    from backend.services.export import ExportError, _materialize

    lazy = pl.LazyFrame({"a": range(20)})

    with pytest.raises(ExportError) as failure:
        _materialize(lazy, tmp_path / "x.xlsx", "xlsx", row_limit=5)

    assert failure.value.status_code == 422
    assert "помещается" in failure.value.message
    assert not (tmp_path / "x.xlsx").exists(), "отказ не должен оставлять обрезанный файл"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "etc passwd.csv"),
        ('отчёт"; rm -rf /', "отчёт rm -rf.csv"),
        ("", "dataset.csv"),
        ("данные.parquet", "данные.csv"),
    ],
)
def test_a_file_name_cannot_escape_the_directory_or_the_header(
    raw: str, expected: str
) -> None:
    """Имя приходит от пользователя и попадает в заголовок ответа и в путь."""
    produced = safe_file_name(raw, "csv")

    assert "/" not in produced
    assert "\\" not in produced
    assert '"' not in produced
    assert not produced.startswith(".")
    assert produced == expected


def test_the_formats_list_comes_from_the_server_with_its_limits(client: TestClient) -> None:
    body = client.get("/api/export/formats").json()
    keys = {item["key"] for item in body["formats"]}

    assert {"csv", "parquet", "jsonl"} <= keys
    assert all(item["can_write"] for item in body["formats"])
    xlsx = next((item for item in body["formats"] if item["key"] == "xlsx"), None)

    if xlsx is not None:
        assert xlsx["row_limit"], "у XLSX есть жёсткий предел, и он должен быть виден"


def test_a_timezone_aware_column_does_not_break_the_excel_export(client: TestClient) -> None:
    """Excel не хранит часовой пояс, и запись такой колонки падала необработанной ошибкой.

    Выбор здесь между отказом и приведением к UTC. Приведение выбрано потому, что момент
    сохраняется, а предупреждение формата видно в интерфейсе до нажатия «Выгрузить».
    """
    import datetime as dt

    workspace_id = client.post("/api/workspaces", json={"name": "пояс"}).json()["workspace_id"]
    buffer = io.BytesIO()
    pl.DataFrame(
        {
            "момент": [
                dt.datetime(2026, 1, 1, 12, 30, tzinfo=dt.UTC),
                dt.datetime(2026, 6, 1, 0, 0, tzinfo=dt.UTC),
            ]
        }
    ).write_parquet(buffer)
    buffer.seek(0)
    dataset_id = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("время.parquet", buffer)}
    ).json()["dataset_id"]

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/export?format=xlsx"
    )

    assert response.status_code == 200, response.text
    restored = pl.read_excel(io.BytesIO(response.content))
    assert restored.height == 2

    #предупреждение о потере пояса приходит с сервера, а не выдумывается интерфейсом
    xlsx = next(
        item for item in client.get("/api/export/formats").json()["formats"] if item["key"] == "xlsx"
    )
    assert any("часовой пояс" in warning for warning in xlsx["warnings"])
