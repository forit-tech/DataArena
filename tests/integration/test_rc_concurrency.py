"""Регрессии RC-аудита: одновременные правки и чтения.

DataArena — один процесс, но FastAPI обслуживает запросы пулом потоков: две вкладки,
двойной щелчок или быстрая серия отмен — это одновременные запросы к одному датасету.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.domain.pipeline.models import Pipeline, Step, StepKind
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
    return client.post("/api/workspaces", json={"name": "Одновременно"}).json()["workspace_id"]


def upload(client: TestClient, ws: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    buffer.seek(0)

    return client.post(f"/api/workspaces/{ws}/datasets", files={"file": (name, buffer)}).json()["dataset_id"]


def put(client: TestClient, ws: str, dataset_id: str, steps: list[dict]) -> int:
    return client.put(
        f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline",
        json={"steps": [{"step_id": f"s{index}", **step} for index, step in enumerate(steps, 1)]},
    ).status_code


def together(*calls: Any) -> None:
    threads = [threading.Thread(target=call) for call in calls]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


SORT = {"kind": "sort_rows", "params": {"column": "a"}}


def test_rc19_concurrent_undos_each_undo_one_step(client: TestClient, ws: str) -> None:
    """Прежде из четырёх одновременных отмен срабатывала одна, а ответ был «успешно» у всех."""
    dataset_id = upload(client, ws, pl.DataFrame({"a": [3, 1, 2]}), "u.parquet")

    for count in range(1, 6):
        assert put(client, ws, dataset_id, [SORT] * count) == 200

    codes: list[int] = []
    together(*(
        lambda: codes.append(client.post(f"/api/workspaces/{ws}/datasets/{dataset_id}/pipeline/undo").status_code)
        for _ in range(4)
    ))
    pipeline = context.get_workspace_store().get_pipeline(dataset_id)

    assert codes == [200] * 4
    assert (len(pipeline.undo), len(pipeline.redo), len(pipeline.steps)) == (1, 4, 1)


def test_rc19_concurrent_saves_keep_every_state_in_the_history(client: TestClient, ws: str) -> None:
    dataset_id = upload(client, ws, pl.DataFrame({"a": [3, 1, 2]}), "p.parquet")
    together(*(lambda count=count: put(client, ws, dataset_id, [SORT] * count) for count in range(1, 9)))

    assert len(context.get_workspace_store().get_pipeline(dataset_id).undo) == 8


def test_rc20_a_pipeline_saved_during_a_health_check_does_not_hide_behind_its_stamp(
    client: TestClient, ws: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт, посчитанный по старым шагам, сохранялся под меткой новых и считался актуальным."""
    from backend.services.health import report

    dataset_id = upload(client, ws, pl.DataFrame({"a": [1, None, None, None, 5] * 20}), "h.parquet")
    original = report.replayed
    raced = []

    def racing(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)

        if not raced:
            raced.append(True)
            context.get_workspace_store().save_pipeline(
                Pipeline(
                    dataset_id=dataset_id,
                    steps=(Step("s1", StepKind.FILL_MISSING, {"column": "a", "value": "0"}),),
                )
            )

        return result

    monkeypatch.setattr(report, "replayed", racing)
    url = f"/api/workspaces/{ws}/datasets/{dataset_id}/health"
    first = {finding["code"] for finding in client.get(url).json()["findings"]}
    second = {finding["code"] for finding in client.get(url).json()["findings"]}

    #первый отчёт честно описывает шаги, по которым посчитан; второй — уже новые шаги
    assert any("null" in code or "missing" in code for code in first), first
    assert not any("null" in code or "missing" in code for code in second), second


def test_rc21_one_unreadable_pipeline_does_not_close_the_sql_window(client: TestClient, ws: str) -> None:
    healthy = upload(client, ws, pl.DataFrame({"a": [1]}), "здоровый.parquet")
    broken = upload(client, ws, pl.DataFrame({"b": [1]}), "сломанный.parquet")
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO dataset_pipelines (dataset_id, document, updated_at) VALUES (?, '{oops', '')",
            (broken,),
        )
        connection.commit()

    response = client.get(f"/api/workspaces/{ws}/sql/datasets")

    assert response.status_code == 200, response.text
    flags = {item["dataset_id"]: item["has_pipeline_steps"] for item in response.json()["datasets"]}
    assert flags == {healthy: False, broken: None}


# ── RC-22: удаление рабочего пространства ────────────────────────────────────


def xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    pl.DataFrame({"a": [1, 2], "b": ["x", "y"]}).write_excel(buffer)

    return buffer.getvalue()


def test_rc22_deleting_a_workspace_removes_its_files_and_spares_the_neighbour(client: TestClient) -> None:
    """Прежде удалялись только записи: файлы оставались на диске навсегда. А конвертация
    переиспользовалась между пространствами, и файл соседа лежал в чужом каталоге."""
    root = Path(config.get_settings().workspace_root)
    first = client.post("/api/workspaces", json={"name": "Первое"}).json()["workspace_id"]
    second = client.post("/api/workspaces", json={"name": "Второе"}).json()["workspace_id"]
    raw = xlsx_bytes()
    kept = []

    for workspace in (first, second):
        response = client.post(
            f"/api/workspaces/{workspace}/datasets", files={"file": ("same.xlsx", io.BytesIO(raw))}
        )
        assert response.status_code == 201, response.text
        kept.append(response.json()["dataset_id"])

    neighbour = context.get_workspace_store().get_dataset(kept[1])
    #у соседа свой производный файл, в своём каталоге
    assert neighbour.derived is not None
    assert Path(neighbour.derived.path).resolve().is_relative_to((root / second).resolve())

    assert client.delete(f"/api/workspaces/{first}").status_code == 204
    assert not (root / first).exists()
    assert client.get(f"/api/workspaces/{second}/datasets/{kept[1]}/rows").status_code == 200


def test_rc22_a_legacy_cross_workspace_file_survives_the_deletion(client: TestClient) -> None:
    """Базы до версии 8 уже содержат ссылки соседей на чужие файлы: их удалять нельзя."""
    root = Path(config.get_settings().workspace_root)
    first = client.post("/api/workspaces", json={"name": "Первое"}).json()["workspace_id"]
    second = client.post("/api/workspaces", json={"name": "Второе"}).json()["workspace_id"]
    raw = xlsx_bytes()
    owner = client.post(f"/api/workspaces/{first}/datasets", files={"file": ("x.xlsx", io.BytesIO(raw))}).json()["dataset_id"]
    borrower = client.post(f"/api/workspaces/{second}/datasets", files={"file": ("x.xlsx", io.BytesIO(raw))}).json()["dataset_id"]
    store = context.get_workspace_store()
    shared = store.get_dataset(owner).derived.path  # type: ignore[union-attr]

    with store._connect() as connection:
        connection.execute("UPDATE derived_artifacts SET path = ? WHERE dataset_id = ?", (shared, borrower))
        connection.commit()

    assert client.delete(f"/api/workspaces/{first}").status_code == 204
    assert Path(shared).exists()
    assert not (root / first / "sources").exists()
    assert client.get(f"/api/workspaces/{second}/datasets/{borrower}/rows").status_code == 200
