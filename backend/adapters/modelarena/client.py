"""Минимальный HTTP-клиент ModelArena: список прогонов и лидерборд прогона.

Адрес берётся только из настройки `DATAARENA_MODELARENA_URL`, а не из запроса
пользователя: иначе endpoint превратился бы в прокси на произвольный адрес (SSRF).
Идентификатор прогона проверяется по шаблону до подстановки в путь.

Связь намеренно слабая: только чтение двух ответов, без общих библиотек и без
изменения ModelArena. Если ModelArena не запущена, DataArena сообщает об этом
и продолжает работать — матрицу можно загрузить файлом.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from backend.core.errors import AppError, NotFoundError

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
HTTP_NOT_FOUND = 404


class ModelArenaUnavailableError(AppError):
    status_code = 502
    code = "modelarena_unavailable"


class ModelArenaNotConfiguredError(AppError):
    status_code = 409
    code = "modelarena_not_configured"


def _get(base_url: str | None, path: str) -> Any:
    if not base_url:
        raise ModelArenaNotConfiguredError(
            "Адрес ModelArena не настроен (переменная DATAARENA_MODELARENA_URL). "
            "Матрицу можно загрузить файлом: лидерборд ModelArena в JSON или CSV."
        )
    url = base_url.rstrip("/") + path
    if not url.startswith(("http://", "https://")):
        raise ModelArenaNotConfiguredError("Адрес ModelArena должен начинаться с http:// или https://.")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == HTTP_NOT_FOUND:
            raise NotFoundError("ModelArena не знает такого прогона.") from error
        raise ModelArenaUnavailableError(f"ModelArena ответила статусом {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ModelArenaUnavailableError(f"ModelArena по адресу {base_url} не отвечает. Запущена ли она?") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise ModelArenaUnavailableError("Ответ ModelArena слишком большой.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelArenaUnavailableError("ModelArena вернула не JSON.") from error


def list_runs(base_url: str | None) -> list[dict[str, Any]]:
    payload = _get(base_url, "/api/arena/runs")
    runs = payload.get("runs") if isinstance(payload, dict) else payload
    return [item for item in (runs or []) if isinstance(item, dict)]


def fetch_leaderboard(base_url: str | None, run_id: str) -> dict[str, Any]:
    if not RUN_ID_PATTERN.match(run_id):
        raise NotFoundError("Некорректный идентификатор прогона ModelArena.")
    payload = _get(base_url, f"/api/arena/runs/{run_id}/leaderboard")
    if not isinstance(payload, dict):
        raise ModelArenaUnavailableError("Лидерборд ModelArena имеет неожиданный формат.")
    return payload
