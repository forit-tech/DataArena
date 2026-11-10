"""Регрессии RC-аудита выгрузки и пакета.

Выгрузка не должна менять данные, пакет — утверждать то, чего нет, а отказ любого из них —
оставлять за собой файлы.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context

REFERENCE = Path(__file__).resolve().parents[2] / "docs/contracts/reference"
FORMATS = ["csv", "tsv", "parquet", "json", "jsonl", "xlsx", "feather", "arrow"]


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
    return client.post("/api/workspaces", json={"name": "Выгрузка"}).json()["workspace_id"]


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Отдельный временный каталог: по нему видно, что осталось после отказа."""
    directory = tmp_path / "tmp"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))

    return directory


def upload(client: TestClient, ws: str, frame: pl.DataFrame | None, name: str, raw: bytes | None = None) -> str:
    buffer = io.BytesIO()

    if raw is not None:
        buffer.write(raw)
    elif frame is not None:
        frame.write_parquet(buffer)

    buffer.seek(0)
    response = client.post(f"/api/workspaces/{ws}/datasets", files={"file": (name, buffer)})

    assert response.status_code in {200, 201}, response.text

    return response.json()["dataset_id"]


def put(client: TestClient, ws: str, dataset_id: str, steps: list[dict]) -> None:
    response = client.put(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline",
        json={"steps": [{"step_id": f"s{index}", **step} for index, step in enumerate(steps, 1)]},
    )

    assert response.status_code == 200, response.text


def export(client: TestClient, ws: str, dataset_id: str, fmt: str) -> bytes:
    response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/export", params={"format": fmt})

    assert response.status_code == 200, response.text

    return response.content


def package(client: TestClient, ws: str, dataset_id: str) -> Any:
    return client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/package")


def load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("reference_validator", REFERENCE / "validate_package.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["reference_validator"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    return module


def validate(content: bytes, into: Path) -> list:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        archive.extractall(into)

    return load_validator().validate_package(into).errors


# ── RC-16: выгрузка не меняет данные ─────────────────────────────────────────


SAMPLE = pl.DataFrame(
    {
        "id": [1, 2, 3],
        "big": pl.Series([2**62, -(2**62), None], dtype=pl.Int64),
        "text": ["-5", "+7 999 123-45-67", "ёжик 名前"],
        "d": [dt.date(2024, 1, 1), None, dt.date(1900, 1, 1)],
        "flag": [True, None, False],
    }
)


@pytest.mark.parametrize("fmt", FORMATS)
def test_rc16_export_then_import_returns_the_same_table(client: TestClient, ws: str, fmt: str) -> None:
    dataset_id = upload(client, ws, SAMPLE, "исходник.parquet")
    other = client.post("/api/workspaces", json={"name": "Обратно"}).json()["workspace_id"]
    back = upload(client, other, None, f"обратно.{fmt}", raw=export(client, ws, dataset_id, fmt))
    body = client.get(f"/api/workspaces/{other}/datasets/{back}/rows", params={"limit": 10}).json()

    assert body["columns"] == SAMPLE.columns
    assert body["total_rows"] == SAMPLE.height
    assert [row["id"] for row in body["rows"]] == [1, 2, 3]
    assert [row["big"] for row in body["rows"]] == [2**62, -(2**62), None]
    #текст выгружается как есть: «-5» и телефон не превращаются в «'-5» и «'+7 …»
    assert [row["text"] for row in body["rows"]] == ["-5", "+7 999 123-45-67", "ёжик 名前"]
    assert [row["d"] for row in body["rows"]] == ["2024-01-01", None, "1900-01-01"]
    assert [row["flag"] for row in body["rows"]] == [True, None, False]


@pytest.mark.parametrize("fmt", ["csv", "tsv"])
def test_rc16_real_formulas_are_still_neutralised(client: TestClient, ws: str, fmt: str) -> None:
    hostile = pl.DataFrame({"x": ["=1+1", "@SUM(A1)", "-2+3+cmd|' /C calc'!A0", "+cmd", "-5", "+7 999"]})
    dataset_id = upload(client, ws, hostile, "формулы.parquet")
    text = export(client, ws, dataset_id, fmt).decode("utf-8")

    for value in ("'=1+1", "'@SUM(A1)", "'-2+3+cmd", "'+cmd"):
        assert value in text

    assert "'-5" not in text and "'+7" not in text


def test_rc16_xlsx_keeps_text_as_text(client: TestClient, ws: str) -> None:
    """В XLSX строки пишутся строками: апостроф не защищал, а оставался в ячейке."""
    dataset_id = upload(client, ws, pl.DataFrame({"x": ["=1+1", "-5"]}), "x.parquet")
    frame = pl.read_excel(io.BytesIO(export(client, ws, dataset_id, "xlsx")))

    assert frame["x"].to_list() == ["=1+1", "-5"]


# ── RC-17: отказ не оставляет файлов ─────────────────────────────────────────


def test_rc17_a_failed_export_leaves_no_temporary_directory(client: TestClient, ws: str, scratch: Path) -> None:
    dataset_id = upload(client, ws, SAMPLE, "x.parquet")

    for fmt in ("bogus", "xlsx"):
        if fmt == "xlsx":
            #строк больше предела XLSX: выгрузка отказывает после создания каталога
            from backend.services import export as export_service

            original = export_service.MATERIALIZED_ROW_LIMIT
            export_service.MATERIALIZED_ROW_LIMIT = 1

        try:
            response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/export", params={"format": fmt})
        finally:
            if fmt == "xlsx":
                export_service.MATERIALIZED_ROW_LIMIT = original

        assert response.status_code in {415, 422}

    assert list(scratch.iterdir()) == []


def test_rc17_a_failed_package_leaves_no_temporary_directory(client: TestClient, ws: str, scratch: Path) -> None:
    dataset_id = upload(client, ws, SAMPLE, "x.parquet")
    put(client, ws, dataset_id, [{"kind": "filter_rows", "params": {"column": "id", "operator": "gt", "value": "100"}}])

    assert package(client, ws, dataset_id).status_code == 422
    assert list(scratch.iterdir()) == []


# ── RC-18: пакет не утверждает того, чего нет ────────────────────────────────


def test_rc18_huge_unsigned_integers_are_refused_with_a_reason(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, pl.DataFrame({"u": pl.Series([1, 2**64 - 1], dtype=pl.UInt64)}), "u.parquet")
    response = package(client, ws, dataset_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "package_failed"
    assert "2^63" in response.json()["error"]["message"]


def test_rc18_the_profile_is_strict_json_even_with_nan_and_infinity(client: TestClient, ws: str, tmp_path: Path) -> None:
    frame = pl.DataFrame({"f": [1.0, float("nan"), float("inf"), -float("inf"), 3.0]})
    dataset_id = upload(client, ws, frame, "f.parquet")
    content = package(client, ws, dataset_id).content

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        profile = json.loads(
            archive.read("profile.json"),
            parse_constant=lambda literal: pytest.fail(f"в профиле литерал {literal}"),
        )

    stats = profile["columns"][0]["numeric_stats"]
    assert (stats["min"], stats["max"]) == (1.0, 3.0)
    assert validate(content, tmp_path / "unpacked") == []


def test_rc18_the_recipe_requires_source_columns_by_their_source_names(client: TestClient, ws: str) -> None:
    """После «a → b» и отбора по «b» рецепт требовал от источника колонку «b», которой в нём нет."""
    dataset_id = upload(client, ws, pl.DataFrame({"a": [1, 2], "c": [3, 4]}), "r.parquet")
    put(client, ws, dataset_id, [
        {"kind": "rename_column", "params": {"column": "a", "to": "b"}},
        {"kind": "filter_rows", "params": {"column": "b", "operator": "gt", "value": "0"}},
        {"kind": "duplicate_column", "params": {"column": "c", "to": "копия"}},
        {"kind": "sort_rows", "params": {"column": "копия"}},
    ])

    with zipfile.ZipFile(io.BytesIO(package(client, ws, dataset_id).content)) as archive:
        recipe = json.loads(archive.read("recipe.json"))

    assert recipe["inputs"][0]["required_columns"] == ["a", "c"]


def test_rc18_a_long_dataset_name_still_gives_a_valid_package(client: TestClient, ws: str, tmp_path: Path) -> None:
    """Имя в манифесте ограничено двумястами символами, имя датасета — двумястами пятьюдесятью пятью."""
    dataset_id = upload(client, ws, SAMPLE, "д" * 240 + ".parquet")
    response = package(client, ws, dataset_id)

    assert response.status_code == 200, response.text
    assert validate(response.content, tmp_path / "unpacked") == []


def test_rc18_a_pipeline_changed_while_packaging_gives_no_package(
    client: TestClient, ws: str, monkeypatch: pytest.MonkeyPatch, scratch: Path
) -> None:
    from backend.domain.pipeline.models import Pipeline, Step, StepKind
    from backend.services.package import builder

    dataset_id = upload(client, ws, SAMPLE, "гонка.parquet")
    original = builder.build

    def racing(*args: Any, **kwargs: Any) -> Any:
        built = original(*args, **kwargs)
        context.get_workspace_store().save_pipeline(
            Pipeline(dataset_id=dataset_id, steps=(Step("s1", StepKind.SORT_ROWS, {"column": "id"}),))
        )

        return built

    monkeypatch.setattr(builder, "build", racing)
    response = package(client, ws, dataset_id)

    assert response.status_code == 422
    assert "изменился во время сборки" in response.json()["error"]["message"]
    assert list(scratch.iterdir()) == []
