"""Что уходит наружу в ответе об ошибке, а что остаётся в логе.

Проверяется не наличие обработчика, а конкретные утечки: трассировка, внутренние пути,
переменные окружения и пользовательские значения не должны попадать в HTTP-ответ.
"""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.request_context import REQUEST_ID_HEADER
from backend.core.errors import DatasetNotFoundError


def _client_with(route: APIRouter) -> TestClient:
    app = create_app()
    app.include_router(route)
    return TestClient(app, raise_server_exceptions=False)


def test_stack_trace_and_internal_paths_never_reach_the_client() -> None:
    router = APIRouter()

    @router.get("/api/leak")
    def leak() -> None:
        raise RuntimeError(
            "не удалось открыть C:/Users/secret/.dataarena/state.db, токен ABC123SECRET"
        )

    response = _client_with(router).get("/api/leak")
    body = response.text

    assert response.status_code == 500
    for forbidden in ("Traceback", "RuntimeError", "C:/Users/secret", "ABC123SECRET", "state.db"):
        assert forbidden not in body, f"в ответе оказалось «{forbidden}»"


def test_environment_variables_do_not_leak_through_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    #переменные окружения читаются только конфигом и не должны появляться в ответе
    monkeypatch.setenv("DATAARENA_MODELARENA_URL", "http://internal.example/secret-token")
    router = APIRouter()

    @router.get("/api/env-leak")
    def env_leak() -> None:
        import os

        raise RuntimeError(os.environ.get("DATAARENA_MODELARENA_URL", ""))

    response = _client_with(router).get("/api/env-leak")

    assert "secret-token" not in response.text


def test_every_error_carries_a_request_id_for_investigation() -> None:
    #без идентификатора строки лога одной неудачной операции невозможно связать между собой,
    #а пользователь не может назвать ничего, кроме времени
    router = APIRouter()

    @router.get("/api/known")
    def known() -> None:
        raise DatasetNotFoundError("Датасет не найден.", details={"dataset_id": "ds_x"})

    response = _client_with(router).get("/api/known")
    payload = response.json()

    assert payload["request_id"]
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]


def test_request_id_stays_out_of_domain_details() -> None:
    #details принадлежит предметной области и описывает саму ошибку; идентификатор запроса —
    #свойство транспорта. Смешав их, мы заставили бы каждого потребителя фильтровать чужое поле
    router = APIRouter()

    @router.get("/api/details")
    def details() -> None:
        raise DatasetNotFoundError("Нет.", details={"dataset_id": "ds_x"})

    payload = _client_with(router).get("/api/details").json()

    assert payload["error"]["details"] == {"dataset_id": "ds_x"}


def test_two_requests_get_different_identifiers() -> None:
    #идентификатор не должен протекать между запросами из общего пула потоков
    router = APIRouter()

    @router.get("/api/boom")
    def boom() -> None:
        raise DatasetNotFoundError("Нет.")

    client = _client_with(router)
    first = client.get("/api/boom").json()["request_id"]
    second = client.get("/api/boom").json()["request_id"]

    assert first != second


def test_client_supplied_request_id_is_ignored() -> None:
    #значение из заголовка попадает в лог и в ответ: принятое снаружи, оно позволило бы
    #засорить лог или выдать себя за чужой запрос
    router = APIRouter()

    @router.get("/api/spoof")
    def spoof() -> None:
        raise DatasetNotFoundError("Нет.")

    #значение только ASCII: HTTP-заголовок не переносит другие байты, и подделка в реальной
    #атаке тоже была бы ASCII
    spoofed = "spoofed-request-id"
    response = _client_with(router).get("/api/spoof", headers={REQUEST_ID_HEADER: spoofed})

    assert response.json()["request_id"] != spoofed
    assert response.headers[REQUEST_ID_HEADER] != spoofed


def test_malformed_json_body_is_rejected_without_echoing_it() -> None:
    router = APIRouter()

    @router.post("/api/json")
    def accept(payload: dict) -> dict:
        return payload

    response = _client_with(router).post(
        "/api/json",
        content=b'{"secret_value": "ABC123SECRET", broken',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "ABC123SECRET" not in response.text


def test_unexpected_content_type_does_not_crash_the_handler() -> None:
    router = APIRouter()

    @router.post("/api/json2")
    def accept(payload: dict) -> dict:
        return payload

    response = _client_with(router).post(
        "/api/json2", content=b"\x00\x01\x02", headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code in (415, 422)
    assert "Traceback" not in response.text


def test_error_body_is_valid_json_even_for_unexpected_failures() -> None:
    #frontend разбирает ответ как JSON: неструктурированный текст лишил бы его кода ошибки
    router = APIRouter()

    @router.get("/api/weird")
    def weird() -> None:
        raise ValueError("нестандартный сбой")

    response = _client_with(router).get("/api/weird")
    payload = json.loads(response.text)

    assert payload["error"]["code"] == "internal_error"
    assert payload["detail"]


def test_every_error_shape_carries_the_identifier_in_the_body_too() -> None:
    #проверено вживую: у ответа 404 идентификатор был в заголовке и отсутствовал в теле,
    #поэтому клиент, читающий только JSON, терял его на части ответов
    from backend.api.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.get("/api/system/does-not-exist")

    payload = response.json()
    assert payload["request_id"]
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]
