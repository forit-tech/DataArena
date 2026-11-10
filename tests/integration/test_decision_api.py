"""Decision Lab через HTTP: расчёт, импорт, отсутствие ModelArena."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _payload(**overrides: object) -> dict:
    payload: dict = {
        "criteria": [
            {"key": "auc", "label": "ROC-AUC", "direction": "max", "weight": 5, "group": "quality"},
            {"key": "ms", "label": "Инференс", "direction": "min", "weight": 3, "unit": "мс"},
            {"key": "mb", "label": "Размер", "direction": "min", "weight": 2, "unit": "МБ"},
        ],
        "alternatives": [
            {"key": "A", "label": "A", "values": {"auc": {"value": 0.91, "std": 0.01}, "ms": {"value": 45}, "mb": {"value": 120}}},
            {"key": "B", "label": "B", "values": {"auc": {"value": 0.89, "std": 0.01}, "ms": {"value": 12}, "mb": {"value": 35}}},
            {"key": "C", "label": "C", "values": {"auc": {"value": 0.93, "std": 0.01}, "ms": {"value": 110}, "mb": {"value": 280}}},
        ],
        "constraints": [{"criterion": "ms", "op": "<=", "bound": 50}],
        "sensitivity": {"samples": 200, "seed": 7},
    }
    payload.update(overrides)
    return payload


def test_evaluate_returns_three_modes(client: TestClient) -> None:
    response = client.post("/api/decision/evaluate", json=_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["modes"]["primary"]["winner"] == "C"
    assert body["modes"]["topsis"]["winner"] == "B"
    assert body["modes"]["robust"]["winner"] == "B"
    assert body["method"]["seed"] == 7
    assert body["method"]["version"] == "cp-topsis-r/2"
    weights = {item["key"]: item["effective_weight"] for item in body["criteria"]}
    assert weights == pytest.approx({"auc": 0.5, "ms": 0.3, "mb": 0.2})
    status = {item["key"]: item["status"] for item in body["alternatives"]}
    assert status["C"] == "infeasible"
    assert body["explanation"]


def test_zero_weights_are_rejected_with_reason(client: TestClient) -> None:
    payload = _payload()
    for item in payload["criteria"]:
        item["weight"] = 0
    response = client.post("/api/decision/evaluate", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "decision_invalid"


def test_negative_weight_is_rejected_by_schema(client: TestClient) -> None:
    payload = _payload()
    payload["criteria"][0]["weight"] = -1
    assert client.post("/api/decision/evaluate", json=payload).status_code == 422


def test_null_value_means_unmeasured(client: TestClient) -> None:
    payload = _payload(constraints=[])
    payload["alternatives"][0]["values"]["ms"] = {"value": None}
    body = client.post("/api/decision/evaluate", json=payload).json()
    status = {item["key"]: item for item in body["alternatives"]}
    assert status["A"]["status"] == "incomplete"


def test_import_csv(client: TestClient) -> None:
    content = b"model,roc_auc,inference_ms,model_mb\nlogreg,0.91,4,0.1\ngbm,0.94,9,2.5\n"
    response = client.post("/api/decision/import", files={"file": ("models.csv", content, "text/csv")})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "csv"
    assert [item["label"] for item in body["alternatives"]] == ["logreg", "gbm"]


def test_import_rejects_unknown_json(client: TestClient) -> None:
    response = client.post("/api/decision/import", files={"file": ("x.json", b'{"foo": 1}', "application/json")})
    assert response.status_code == 422


def test_modelarena_not_configured_is_explicit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core import config

    monkeypatch.delenv("DATAARENA_MODELARENA_URL", raising=False)
    config.get_settings.cache_clear()
    try:
        response = client.get("/api/decision/modelarena/runs")
    finally:
        config.get_settings.cache_clear()
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "modelarena_not_configured"


def test_modelarena_unreachable_is_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core import config

    #порт 9 (discard) на loopback закрыт: соединение отвергается сразу
    monkeypatch.setenv("DATAARENA_MODELARENA_URL", "http://127.0.0.1:9")
    config.get_settings.cache_clear()
    try:
        response = client.get("/api/decision/modelarena/runs")
    finally:
        config.get_settings.cache_clear()
    assert response.status_code == 502
