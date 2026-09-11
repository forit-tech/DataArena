"""Доменные ошибки и единый формат ответа.

Иерархия перенесена из AutoDataAnalysis: она задаёт одну структуру ошибки для всего API,
чтобы frontend не разбирал три разных формата от FastAPI, Starlette и предметного кода.

Класс ошибки заводится **вместе с кодом, который её возбуждает**. Ошибки под будущие
этапы — превышение размера загрузки, таймаут SQL, недоступность ModelArena — здесь
намеренно отсутствуют: класс без места возбуждения нельзя ни проверить тестом, ни
удержать в соответствии с реальным поведением, а в OpenAPI он объявляет статус,
который сервис не умеет возвращать.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    #этот базовый класс отделяет ожидаемые ошибки предметной области от непредвиденных сбоев процесса
    #благодаря ему HTTP-слой отвечает одинаковой структурой и не превращает каждое исключение в 400
    status_code = 400
    code = "app_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(AppError):
    #запрос синтаксически корректен, но содержит недопустимые значения
    status_code = 422
    code = "validation_error"


class UnsupportedFormatError(AppError):
    #неподдерживаемый формат файла отделён от повреждённого содержимого: это разные действия пользователя
    status_code = 415
    code = "unsupported_format"


class DatasetReadError(AppError):
    #повреждённый или нечитаемый датасет с поддерживаемым расширением
    status_code = 400
    code = "dataset_read_error"


class NotFoundError(AppError):
    #отсутствующий workspace, dataset, версия, сохранённый запрос или рецепт
    status_code = 404
    code = "not_found"


class WorkspaceNotFoundError(NotFoundError):
    code = "workspace_not_found"


class DatasetNotFoundError(NotFoundError):
    code = "dataset_not_found"


def build_error_payload(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    #эта функция задаёт единственную структуру ошибки для всего API
    #поле detail дублирует сообщение: так ответ читается и теми клиентами, которые ждут формат FastAPI
    #
    #request_id лежит рядом с error, а не внутри details: details принадлежит предметной
    #области и описывает саму ошибку, а идентификатор запроса — свойство транспорта.
    #Смешав их, мы заставили бы каждого потребителя details отфильтровывать чужое поле
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
        "detail": message,
    }

    if request_id:
        payload["request_id"] = request_id

    return payload
