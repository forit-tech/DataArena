"""Реестр выполняющихся запросов — то, без чего отмена была бы кнопкой без действия.

Отмена работает так: клиент присылает вместе с запросом собственную метку, сервер держит
по ней управляющий объект, пока запрос выполняется, и отдельный вызов эту метку прерывает.
Метка нужна именно от клиента: идентификатор, выданный в ответе, пришёл бы уже после
того, как запрос закончился, и отменять было бы нечего.

Реестр живёт в памяти процесса и намеренно не переживает перезапуск: он описывает то,
что выполняется прямо сейчас, а после перезапуска не выполняется ничего.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from backend.adapters.engine.duckdb_session import QueryHandle
from backend.core.errors import AppError, ValidationError

#метка — шестнадцатеричное представление uuid4: длина и алфавит проверяются до того,
#как значение попадёт в ключ реестра или в лог
QUERY_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class QueryTokenInUseError(AppError):
    #та же метка уже выполняется: молча заменить запись значило бы потерять управление
    #предыдущим запросом, и отменить его стало бы нечем
    status_code = 409
    code = "query_token_in_use"


class QueryNotRunningError(AppError):
    #отменять нечего: запрос уже закончился или его метки не было. Это не ошибка сервера,
    #и интерфейс должен показать «уже завершён», а не «сбой»
    status_code = 404
    code = "query_not_running"


def ensure_query_token(value: str) -> str:
    if not QUERY_TOKEN_PATTERN.match(value):
        raise ValidationError("Некорректная метка запроса.", details={"query_token": value[:64]})

    return value


class RunningQueries:
    """Что выполняется прямо сейчас, в разрезе рабочих пространств."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[tuple[str, str], QueryHandle] = {}

    @contextmanager
    def slot(self, workspace_id: str, token: str) -> Iterator[QueryHandle]:
        """Занимает метку на время выполнения и освобождает её при любом исходе.

        Ручка создаётся **сразу**, ещё до открытия соединения с движком. Прежняя версия
        сначала занимала место пустым значением, а ручку подставляла позже, — и отмена,
        пришедшая в этот промежуток, отвечала «нечего отменять» запросу, который через
        мгновение начинался и досчитывался до конца. Соединение ручка получает позже,
        а состояние отмены у неё есть с первой миллисекунды.
        """
        ensure_query_token(token)
        key = (workspace_id, token)
        handle = QueryHandle()

        with self._lock:
            if key in self._handles:
                raise QueryTokenInUseError(
                    "Запрос с такой меткой уже выполняется.", details={"query_token": token}
                )

            self._handles[key] = handle

        try:
            yield handle
        finally:
            with self._lock:
                self._handles.pop(key, None)

    def cancel(self, workspace_id: str, token: str) -> None:
        """Прерывает выполняющийся запрос. Отсутствие запроса — не ошибка сервера."""
        ensure_query_token(token)

        with self._lock:
            handle = self._handles.get((workspace_id, token))

        if handle is None:
            raise QueryNotRunningError(
                "Запрос не выполняется: он уже завершился или не запускался.",
                details={"query_token": token},
            )

        #прерывание вызывается вне блокировки: оно обращается к движку, и держать
        #на это время общий замок значило бы задерживать все остальные запросы
        handle.cancel()

    def running_count(self, workspace_id: str) -> int:
        with self._lock:
            return sum(1 for key in self._handles if key[0] == workspace_id)
