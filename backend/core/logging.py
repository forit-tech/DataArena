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


def configure_logging(level: int = logging.INFO) -> None:
    #эта функция настраивает единственный обработчик процесса и не дублирует записи при повторном вызове
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
