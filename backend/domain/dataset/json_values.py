"""Приведение значений таблицы к тому, что переживёт JSON.

Функция живёт в доменном слое, потому что говорит о значениях данных, а не о транспорте
и не о движке. Её нужны и постраничной выдаче таблицы, и статистике колонки, и результату
SQL-запроса; держать её в одном из этих мест значило бы заставить остальных импортировать
чужой слой ради одной функции.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any


def json_safe_value(value: Any) -> Any:  # noqa: PLR0911 - диспетчер типов
    #NaN и бесконечности не являются валидным JSON: сериализатор либо выдаёт `NaN`,
    #который не разберёт браузер, либо падает уже посреди отправленного ответа
    if value is None:
        return None

    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value

    if isinstance(value, Decimal):
        #строка, а не float: перевод в float теряет точность, ради которой Decimal и берут
        return str(value)

    if isinstance(value, datetime | date | time):
        return value.isoformat()

    if isinstance(value, timedelta):
        return value.total_seconds()

    if isinstance(value, bytes):
        return f"<{len(value)} байт>"

    if isinstance(value, list | tuple):
        return [json_safe_value(item) for item in value]

    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}

    return value
