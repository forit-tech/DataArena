"""Запись лога не теряется, если консоль не знает символа из данных."""

from __future__ import annotations

import io
import json
import logging

from backend.core.logging import JsonFormatter, SafeStreamHandler


def test_a_record_with_characters_outside_the_console_encoding_is_escaped_not_lost() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251", errors="strict", write_through=True)
    handler = SafeStreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.Logger("rc-audit")
    logger.addHandler(handler)

    logger.error("Ошибка в колонке «ts»: Datetime('μs', 'UTC') и 名前 🙂")

    line = raw.getvalue().decode("cp1251").strip()
    assert json.loads(line)["message"] == "Ошибка в колонке «ts»: Datetime('μs', 'UTC') и 名前 🙂"
