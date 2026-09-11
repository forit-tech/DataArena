"""Сборка FastAPI-приложения и единые обработчики ошибок."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api.request_context import (
    REQUEST_ID_HEADER,
    request_id_middleware,
    request_id_of,
)
from backend.api.routes import health, sql, system, workspaces
from backend.api.schemas.system import ErrorResponse
from backend.core.config import get_settings
from backend.core.errors import AppError, build_error_payload
from backend.core.logging import configure_logging, get_logger

API_DESCRIPTION = """
Локальная рабочая среда для табличных данных: просмотр, SQL, диагностика качества,
преобразования, сборка датасета из нескольких источников и экспорт.

**Граница.** DataArena отвечает только за слой данных. Модели обучает ModelArena —
отдельный сервис. Единственная точка соприкосновения — Dataset Package `dataarena.package/1`.
DataArena полностью работает без ModelArena.

**Формат ошибок.** Любая ошибка возвращается объектом
`{"error": {"code", "message", "details"}, "detail": "..."}`. Поле `detail` дублирует сообщение.
""".strip()

#общие ответы об ошибках описываются один раз и подключаются ко всем маршрутам
#
#перечислены только статусы, которые сервис действительно умеет возвращать: 408 и 413
#были объявлены «на будущее», под таймаут SQL и лимит загрузки, и OpenAPI обещал
#поведение, которого нет. Статус добавляется вместе с кодом, который его возбуждает
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Данные не удалось обработать."},
    403: {"model": ErrorResponse, "description": "Запрос выходит за пределы разрешённого."},
    408: {"model": ErrorResponse, "description": "Запрос выполнялся дольше отведённого времени."},
    404: {"model": ErrorResponse, "description": "Объект не найден."},
    409: {"model": ErrorResponse, "description": "Объект с таким содержимым уже существует."},
    415: {"model": ErrorResponse, "description": "Неподдерживаемый формат файла."},
    422: {"model": ErrorResponse, "description": "Некорректные параметры запроса."},
    500: {"model": ErrorResponse, "description": "Внутренняя ошибка."},
    507: {"model": ErrorResponse, "description": "Недостаточно места на диске."},
}


def create_app() -> FastAPI:
    #фабрика вместо модульного синглтона нужна тестам: каждый тест поднимает свой экземпляр
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="DataArena API",
        version="0.1.0",
        description=API_DESCRIPTION,
        responses=COMMON_ERROR_RESPONSES,
    )

    #без CORS браузер заблокирует запросы с Vite-сервера: frontend и backend работают на разных портах
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        #браузеру нужно разрешение читать этот заголовок, иначе пользователь не сможет
        #процитировать идентификатор запроса при обращении за помощью
        expose_headers=[REQUEST_ID_HEADER],
    )

    #идентификатор запроса ставится до всего остального, чтобы попасть в каждую строку лога
    app.middleware("http")(request_id_middleware)

    for module in (system, workspaces, sql, health):
        app.include_router(module.router, prefix="/api")

    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    #без этих обработчиков FastAPI, Starlette и предметный код отвечали бы тремя разными форматами,
    #и frontend разбирал бы каждый отдельно
    logger = get_logger()

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
        logger.warning(
            "Ожидаемая ошибка предметной области",
            extra={
                "request_id": request_id_of(request),
                "path": request.url.path,
                "code": error.code,
                "status": error.status_code,
            },
        )
        return JSONResponse(
            status_code=error.status_code,
            content=build_error_payload(
                error.code, error.message, error.details, request_id=request_id_of(request)
            ),
            headers={REQUEST_ID_HEADER: request_id_of(request)},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        logger.warning(
            "Запрос не соответствует контракту",
            extra={"request_id": request_id_of(request), "path": request.url.path},
        )
        return JSONResponse(
            status_code=422,
            content=build_error_payload(
                "request_validation_error",
                "Запрос не соответствует контракту API.",
                {"errors": _safe_validation_errors(error)},
                request_id=request_id_of(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        #идентификатор кладётся и в заголовок, и в тело, как во всех остальных обработчиках:
        #иначе клиент, читающий только тело, теряет его на части ответов — проверено вживую,
        #у ответа 404 идентификатор был в заголовке и отсутствовал в JSON
        return JSONResponse(
            status_code=error.status_code,
            content=build_error_payload(
                "http_error", str(error.detail), request_id=request_id_of(request)
            ),
            headers={REQUEST_ID_HEADER: request_id_of(request)},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        #трассировка остаётся в логе целиком, наружу уходит короткое сообщение
        #и идентификатор, по которому эту трассировку можно найти
        logger.exception(
            "Необработанная ошибка при обработке запроса",
            extra={"request_id": request_id_of(request), "path": request.url.path},
            exc_info=error,
        )
        return JSONResponse(
            status_code=500,
            content=build_error_payload(
                "internal_error",
                "Внутренняя ошибка сервера. Подробности записаны в лог backend "
                f"под идентификатором {request_id_of(request)}.",
                request_id=request_id_of(request),
            ),
            headers={REQUEST_ID_HEADER: request_id_of(request)},
        )


def _safe_validation_errors(error: RequestValidationError) -> list[dict[str, str]]:
    #эта функция оставляет от ошибок валидации только место и текст, без сырых входных значений:
    #входные значения могут содержать пользовательские данные, которым не место в ответе об ошибке
    return [
        {
            "location": ".".join(str(part) for part in item.get("loc", [])),
            "message": str(item.get("msg", "")),
        }
        for item in error.errors()
    ]


app = create_app()
