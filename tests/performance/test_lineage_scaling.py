"""Происхождение не читает строки.

Оно складывается из схемы, шагов и записей хранилища. Если бы где-то по дороге данные
читались целиком, ответ для таблицы в миллионы строк занимал бы секунды и память,
а для таблицы в десять строк — миллисекунды. Здесь проверяется, что это не так,
и заодно измеряется широкий случай: сотни колонок и десятки шагов.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context

WIDTH = 400
STEPS = 60


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


def wide(rows: int) -> pl.DataFrame:
    return pl.DataFrame({f"c{index}": pl.Series(["  x "] * rows) for index in range(WIDTH)})


def steps() -> list[dict]:
    #каждый шаг трогает свою колонку, часть — переименовывает: свёртка проходит все ветки
    result = []

    for index in range(STEPS):
        column = f"c{index}"

        if index % 3 == 0:
            result.append({"kind": "rename_column", "params": {"column": column, "to": f"r{index}"}})
        elif index % 3 == 1:
            result.append({"kind": "trim_whitespace", "params": {"columns": [column]}})
        else:
            result.append({"kind": "sort_rows", "params": {"column": column}})

    return [{"step_id": f"s{index}", **step} for index, step in enumerate(result, 1)]


def timed_lineage(client: TestClient, rows: int) -> float:
    workspace_id = client.post("/api/workspaces", json={"name": f"масштаб {rows}"}).json()["workspace_id"]
    buffer = io.BytesIO()
    wide(rows).write_parquet(buffer)
    buffer.seek(0)
    dataset_id = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("wide.parquet", buffer)}
    ).json()["dataset_id"]
    response = client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline", json={"steps": steps()}
    )

    assert response.status_code == 200, response.text

    url = f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/lineage"
    #первый запрос прогревает импорт модулей и кэши движка
    client.get(url)
    started = time.perf_counter()
    response = client.get(url)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    assert len(response.json()["columns"]) == WIDTH

    return elapsed


def test_lineage_time_does_not_grow_with_rows(client: TestClient) -> None:
    small = timed_lineage(client, 10)
    large = timed_lineage(client, 50_000)

    print(f"\nlineage {WIDTH} колонок x {STEPS} шагов: 10 строк {small:.3f} с, 50 000 строк {large:.3f} с")

    #двадцать миллионов ячеек против четырёх тысяч: если бы данные читались, разница
    #была бы на порядки. Запас на шум измерения — трёхкратный
    assert large < max(small * 3, small + 0.5)
    assert large < 10
