"""Системный раздел API и единый контракт ошибок."""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.errors import DatasetNotFoundError, ValidationError


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_health_reports_service_state(client: TestClient) -> None:
    response = client.get("/api/system/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["max_upload_mb"] > 0
    assert isinstance(payload["modelarena_configured"], bool)


def test_health_reports_the_contract_version(client: TestClient) -> None:
    #версию контракта можно сверить, не читая файлы пакета: это нужно и человеку, и ModelArena
    contract = client.get("/api/system/health").json()["contract"]

    assert contract["format"] == "dataarena.package"
    assert contract["version"] == "1.0.0"
    assert contract["fingerprint_algorithm"] == "dataarena-logical-sha256-v1"


def test_datarena_works_without_modelarena(monkeypatch: pytest.MonkeyPatch) -> None:
    #ключевое требование: отсутствие ModelArena не ломает ничего, кроме кнопки перехода
    from backend.core import config

    monkeypatch.delenv("DATAARENA_MODELARENA_URL", raising=False)
    config.get_settings.cache_clear()

    try:
        response = TestClient(create_app()).get("/api/system/health")
        assert response.status_code == 200
        assert response.json()["modelarena_configured"] is False
    finally:
        config.get_settings.cache_clear()


def test_unknown_route_uses_the_common_error_shape(client: TestClient) -> None:
    response = client.get("/api/system/does-not-exist")

    assert response.status_code == 404
    payload = response.json()
    #request_id лежит рядом с error, а не внутри details: details принадлежит предметной
    #области, а идентификатор запроса — свойство транспорта
    assert set(payload) == {"error", "detail", "request_id"}
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["detail"] == payload["error"]["message"]


def test_domain_error_keeps_its_code_and_status() -> None:
    app = create_app()
    router = APIRouter()

    @router.get("/api/boom")
    def boom() -> None:
        raise DatasetNotFoundError("Датасет ds_missing не найден.", details={"dataset_id": "ds_missing"})

    app.include_router(router)
    response = TestClient(app).get("/api/boom")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "dataset_not_found"
    assert response.json()["error"]["details"] == {"dataset_id": "ds_missing"}


def test_unexpected_error_does_not_leak_internals() -> None:
    #трассировка полезна разработчику в логе, но в ответе она выглядит как утечка внутренностей
    app = create_app()
    router = APIRouter()

    @router.get("/api/explode")
    def explode() -> None:
        raise RuntimeError("секретный путь /var/secrets/token и внутренняя деталь")

    app.include_router(router)
    response = TestClient(app, raise_server_exceptions=False).get("/api/explode")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "секретный путь" not in response.text
    assert "RuntimeError" not in response.text


def test_validation_error_does_not_echo_user_values() -> None:
    #значения полей могут содержать пользовательские данные, которым не место в ответе об ошибке
    app = create_app()
    router = APIRouter()

    @router.get("/api/needs-number")
    def needs_number(amount: int) -> dict:
        return {"amount": amount}

    app.include_router(router)
    response = TestClient(app).get("/api/needs-number?amount=секрет-пользователя")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"
    assert "секрет-пользователя" not in response.text


def test_domain_validation_error_maps_to_422() -> None:
    app = create_app()
    router = APIRouter()

    @router.get("/api/bad-step")
    def bad_step() -> None:
        raise ValidationError("Колонка не существует.")

    app.include_router(router)
    response = TestClient(app).get("/api/bad-step")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_openapi_declares_the_common_error_responses(client: TestClient) -> None:
    #OpenAPI не должен врать про контракт: описанные ответы об ошибках обязаны быть в схеме
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/api/system/health"]["get"]["responses"]

    for status in ("400", "404", "422", "500"):
        assert status in responses
