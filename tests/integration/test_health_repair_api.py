"""Исправление находок диагностики: совет → предпросмотр → применение → откат.

Проверяется весь путь через HTTP: исправление — обычный шаг конвейера, находка после
него исчезает, отмена конвейера возвращает данные в точности как были, а устаревший
предпросмотр не применяется поверх изменённого конвейера.
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

DIRTY = pl.DataFrame(
    {
        "id": list(range(1, 13)),
        "город": ["Москва", "москва", " Москва", "Казань", "Казань ", "казань", "Москва", "Омск", "Москва", "Казань", "Омск", "Москва"],
        "менеджер": ["Иванова", "Петров ", " Сидорова", "Иванова", "Петров", "Сидорова", "Иванова", "Петров", "Сидорова", "Иванова", "Петров", "Сидорова"],
        "сумма": [100.0, None, 300.0, None, 500.0, 600.0, 700.0, None, 900.0, 1000.0, 1100.0, 1200.0],
        "комментарий": [None] * 12,
    },
    schema_overrides={"комментарий": pl.Utf8},
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
    workspace_id = client.post("/api/workspaces", json={"name": "Ремонт"}).json()["workspace_id"]
    buffer = io.BytesIO()
    DIRTY.write_parquet(buffer)
    buffer.seek(0)
    created = client.post(f"/api/workspaces/{workspace_id}/datasets", files={"file": ("dirty.parquet", buffer)})
    assert created.status_code == 201, created.text
    return workspace_id, created.json()["dataset_id"]


def base(workspace_id: str, dataset_id: str) -> str:
    return f"/api/workspaces/{workspace_id}/datasets/{dataset_id}"


def finding(client: TestClient, url: str, code: str, column: str) -> dict | None:
    report = client.get(f"{url}/health").json()
    for item in report["findings"]:
        if item["code"] == code and column in item["columns"]:
            return item
    return None


def test_whitespace_repair_full_cycle(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "whitespace_anomaly", "менеджер")
    assert target is not None

    advice = client.get(f"{url}/health/findings/{target['finding_id']}/repair").json()
    assert advice["explanation"]["cause"]
    option = advice["options"][0]
    assert option["safety"] == "safe"
    assert option["steps"][0]["kind"] == "trim_whitespace"

    preview = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/preview", json={"option_id": option["option_id"]}
    ).json()
    assert preview["rows_removed"] == 0
    assert preview["values_lost"] == 0
    assert preview["changed_rows"] == 2
    assert {(s["before"], s["after"]) for s in preview["samples"]} == {("Петров ", "Петров"), (" Сидорова", "Сидорова")}

    applied = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/apply",
        json={"option_id": option["option_id"], "steps_fingerprint": preview["steps_fingerprint"]},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"][0]["step_id"].startswith("repair-whitespace_anomaly-")

    #находка исчезла: диагностика проверяет результат конвейера
    assert finding(client, url, "whitespace_anomaly", "менеджер") is None
    rows = client.get(f"{url}/pipeline/rows?limit=50").json()["rows"]
    assert {row["менеджер"] for row in rows} == {"Иванова", "Петров", "Сидорова"}

    #откат возвращает точное прежнее состояние
    assert client.post(f"{url}/pipeline/undo").status_code == 200
    restored = client.get(f"{url}/pipeline/rows?limit=50").json()["rows"]
    assert [row["менеджер"] for row in restored] == DIRTY["менеджер"].to_list()
    assert finding(client, url, "whitespace_anomaly", "менеджер") is not None


def test_case_variants_go_to_most_frequent_spelling(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "case_variant_categories", "город")
    assert target is not None
    advice = client.get(f"{url}/health/findings/{target['finding_id']}/repair").json()
    canonical = next(o for o in advice["options"] if o["option_id"] == "canonical")
    assert canonical["safety"] == "safe"

    preview = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/preview", json={"option_id": "canonical"}
    ).json()
    client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/apply",
        json={"option_id": "canonical", "steps_fingerprint": preview["steps_fingerprint"]},
    )

    rows = client.get(f"{url}/pipeline/rows?limit=50").json()["rows"]
    #«Москва» встречается чаще «москва», «Казань» чаще «казань»: правильные написания сохранены
    assert {row["город"] for row in rows} == {"Москва", "Казань", "Омск"}


def test_stale_preview_is_refused(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "whitespace_anomaly", "менеджер")
    assert target is not None
    preview = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/preview", json={"option_id": "trim"}
    ).json()

    #конвейер меняют между предпросмотром и применением
    client.put(
        f"{url}/pipeline",
        json={"steps": [{"step_id": "manual-1", "kind": "drop_columns", "params": {"columns": ["комментарий"]}}]},
    )

    response = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/apply",
        json={"option_id": "trim", "steps_fingerprint": preview["steps_fingerprint"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "repair_conflict"


def test_missing_values_need_a_decision_with_leakage_note(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "missing_values", "сумма")
    assert target is not None
    advice = client.get(f"{url}/health/findings/{target['finding_id']}/repair").json()
    option = advice["options"][0]
    assert option["safety"] == "decision"
    assert "обучающей части" in option["note"]
    assert option["steps"][0]["params"]["value"] == "700.0"


def test_empty_column_drop_is_a_decision_not_automatic(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "all_null_column", "комментарий")
    assert target is not None
    advice = client.get(f"{url}/health/findings/{target['finding_id']}/repair").json()
    assert advice["options"][0]["safety"] == "decision"
    assert "контракт" in advice["options"][0]["note"]


def test_unknown_option_is_404(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "whitespace_anomaly", "менеджер")
    assert target is not None
    response = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/preview", json={"option_id": "rm -rf"}
    )
    assert response.status_code == 404


def test_missing_values_can_drop_rows_with_preview_of_loss(client: TestClient, dataset: tuple[str, str]) -> None:
    url = base(*dataset)
    target = finding(client, url, "missing_values", "сумма")
    assert target is not None
    advice = client.get(f"{url}/health/findings/{target['finding_id']}/repair").json()
    assert any(o["option_id"] == "drop_rows" and o["safety"] == "lossy" for o in advice["options"])

    preview = client.post(
        f"{url}/health/findings/{target['finding_id']}/repair/preview", json={"option_id": "drop_rows"}
    ).json()
    assert preview["rows_removed"] == 3
