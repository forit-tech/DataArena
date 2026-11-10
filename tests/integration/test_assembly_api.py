"""Умная сборка: связи, опасные соединения, назначение и цель — на данных с известным ответом."""

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

#клиенты и их заказы: правильная связь — client_id, 1:N
CLIENTS = pl.DataFrame({"client_id": list(range(1, 301)), "сегмент": ["A", "B", "C"] * 100})
ORDERS = pl.DataFrame(
    {
        "order_id": list(range(1, 901)),
        "client_id": [i % 300 + 1 for i in range(900)],
        "price": [float(100 + i % 50) for i in range(900)],
        "price_usd": [float(100 + i % 50) / 90 for i in range(900)],
        "status": ["paid", "paid", "refund"] * 300,
    }
)
#справочник моделей и отдельные продажи: общее только «model» с повторами с обеих сторон
MODELS = pl.DataFrame({"model": ["Golf", "Polo", "Camry"] * 10, "trim": [f"t{i}" for i in range(30)]})
SALES = pl.DataFrame({"model": ["Golf", "Polo", "Camry"] * 40, "amount": [float(i) for i in range(120)]})
#таблица без общего с остальными
WEATHER = pl.DataFrame({"city_code": [f"X{i}" for i in range(50)], "temp": [float(i) for i in range(50)]})


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()
    yield TestClient(create_app())
    config.get_settings.cache_clear()
    context.reset_context()


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> str:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    buffer.seek(0)
    response = client.post(f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)})
    assert response.status_code == 201, response.text
    return response.json()["dataset_id"]


def discover(client: TestClient, frames: dict[str, pl.DataFrame]) -> tuple[dict, dict[str, str]]:
    workspace_id = client.post("/api/workspaces", json={"name": "Сборка"}).json()["workspace_id"]
    ids = {name: upload(client, workspace_id, frame, f"{name}.parquet") for name, frame in frames.items()}
    response = client.get(f"/api/workspaces/{workspace_id}/joins/discover")
    assert response.status_code == 200, response.text
    return response.json(), ids


def pair_of(result: dict, ids: dict[str, str], a: str, b: str) -> dict:
    wanted = {ids[a], ids[b]}
    return next(p for p in result["pairs"] if {p["left_dataset_id"], p["right_dataset_id"]} == wanted)


def test_one_to_many_key_is_found_and_safe(client: TestClient) -> None:
    result, ids = discover(client, {"clients": CLIENTS, "orders": ORDERS})
    pair = pair_of(result, ids, "clients", "orders")
    assert pair["verdict"] == "join"
    best = pair["candidates"][0]
    assert best["keys"][0]["left"] == "client_id"
    assert best["cardinality"] in ("1:N", "N:1")
    assert best["estimated_rows"] == 900


def test_many_to_many_is_refused_with_aggregation_advice(client: TestClient) -> None:
    result, ids = discover(client, {"models": MODELS, "sales": SALES})
    pair = pair_of(result, ids, "models", "sales")
    assert pair["verdict"] == "aggregate"
    assert "Не соединяйте построчно" in pair["advice"]
    dangerous = [c for c in pair["candidates"] if c["verdict"] == "dangerous"]
    assert dangerous and dangerous[0]["estimated_rows"] == 1200


def test_unrelated_tables_get_no_relation(client: TestClient) -> None:
    result, ids = discover(client, {"clients": CLIENTS, "weather": WEATHER})
    pair = pair_of(result, ids, "clients", "weather")
    assert pair["verdict"] == "none"
    assert pair["candidates"] == []


def test_targets_and_leakage_suspects(client: TestClient) -> None:
    result, ids = discover(client, {"orders": ORDERS, "clients": CLIENTS})
    orders = next(d for d in result["datasets"] if d["dataset_id"] == ids["orders"])
    targets = {t["column"]: t for t in orders["targets"]}
    assert targets["price"]["task"] == "regression"
    assert targets["status"]["task"] == "classification"
    assert targets["status"]["classes"] == 2
    #price_usd — та же величина в другой валюте: признак-утечка для цели price
    assert any(s["column"] == "price_usd" for s in targets["price"]["leakage_suspects"])
    #идентификатор не предлагается целью
    assert "order_id" not in targets
    roles = {c["name"]: c["role"] for c in orders["columns"]}
    assert roles["order_id"] == "identifier"


def test_purposes_are_hypotheses_with_reasons(client: TestClient) -> None:
    result, ids = discover(client, {"orders": ORDERS, "clients": CLIENTS})
    orders = next(d for d in result["datasets"] if d["dataset_id"] == ids["orders"])
    purposes = {p["purpose"] for p in orders["purposes"]}
    assert {"analytics", "regression", "classification"} <= purposes
    assert all(p["reason"] for p in orders["purposes"])


def test_coinciding_float_measures_are_not_offered_as_keys(client: TestClient) -> None:
    """Регрессия: мощность и сумма продажи совпадали по значениям и считались связью 1:1."""
    models = MODELS.with_columns(pl.Series("hp", [float(90 + i) for i in range(30)]))
    result, ids = discover(client, {"models": models, "sales": SALES})
    pair = pair_of(result, ids, "models", "sales")
    keys = [tuple(k["left"] for k in c["keys"]) for c in pair["candidates"]]
    assert ("hp",) not in keys
    assert pair["verdict"] == "aggregate"
