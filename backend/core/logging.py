"""Structured logging.

Требование п. 19 ТЗ. В AutoDataAnalysis логирование сводилось к одному logger.exception,
поэтому по логам нельзя было ответить на вопрос «что делал пользователь перед ошибкой».

Записи выводятся построчно в JSON: их читает и человек, и любой сборщик логов, а поля
не расползаются по свободному тексту.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "dataarena"
#эти поля есть у каждой записи logging и не являются полезной нагрузкой конкретного события
_RESERVED_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    #этот форматтер печатает запись одной строкой JSON вместе со всеми переданными extra-полями
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_FIELDS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            #трассировка нужна в логе целиком, но наружу, в HTTP-ответ, она не уходит никогда
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


class SafeStreamHandler(logging.StreamHandler):
    """Обработчик, который не теряет запись из-за кодировки консоли.

    Консоль Windows пишет в cp1251 и не знает «μ», иероглифов и эмодзи, которые приходят
    в текстах ошибок вместе с данными пользователя. Стандартный обработчик в таком случае
    печатал «Logging error» и терял запись — ровно ту, где была трассировка падения.
    Здесь такая запись повторяется в ASCII: JSON с экранированными символами остаётся JSON.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)

            try:
                self.stream.write(message + self.terminator)
            except UnicodeEncodeError:
                ascii_only = json.dumps(json.loads(message), ensure_ascii=True)
                self.stream.write(ascii_only + self.terminator)

            self.flush()
        except Exception:  # noqa: BLE001 - так же поступает стандартный обработчик
            self.handleError(record)


def configure_logging(level: int = logging.INFO) -> None:
    #эта функция настраивает единственный обработчик процесса и не дублирует записи при повторном вызове
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return

    handler = SafeStreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
