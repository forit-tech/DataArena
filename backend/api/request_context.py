"""Идентификатор запроса для расследования сбоев.

Без него строки лога, относящиеся к одной неудачной загрузке, невозможно связать между
собой: в них есть путь и датасет, но нет ничего, что отличало бы один запрос от другого
такого же. Пользователь при этом видит сообщение об ошибке и не может назвать поддержке
ничего, кроме времени.

Идентификатор возвращается в заголовке `X-Request-ID` и в `details` ответа об ошибке,
поэтому его можно процитировать и найти в логе.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_ALPHABET = "0123456789abcdef"
_LENGTH = 12
#заголовок от клиента не принимается: он попадает в лог и в ответ, поэтому пришедшее
#снаружи значение позволило бы засорить лог или подделать чужой идентификатор
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def current_request_id() -> str:
    return _request_id.get()


def request_id_of(request: Request) -> str:
    """Идентификатор запроса, надёжный в том числе внутри обработчика ошибки.

    Одного contextvar недостаточно. Обработчик необработанного исключения выполняется
    в `ServerErrorMiddleware`, а он стоит **снаружи** пользовательских middleware:
    контекст там уже другой, и переменная пуста. Проверено — ответ 500 уходил
    с пустым `X-Request-ID` и текстом «записаны в лог под идентификатором .»,
    то есть ровно в том случае, ради которого идентификатор и заводился.

    Значение дублируется в `request.state`, потому что объект запроса доходит
    до обработчика в любом случае.
    """
    stored = getattr(request.state, "request_id", "")

    return stored if isinstance(stored, str) and stored else _request_id.get()


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    #этот middleware ставит идентификатор до обработки и снимает его после,
    #чтобы значение не протекло в соседний запрос из того же потока
    request_id = new_request_id()
    token = _request_id.set(request_id)
    #на самом запросе значение переживает переход в другой контекст выполнения
    request.state.request_id = request_id

    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = current_request_id()
        return response
    finally:
        _request_id.reset(token)
