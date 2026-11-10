"""Клиент локальной языковой модели: Ollama или любой OpenAI-совместимый сервер.

Модель работает на этом же компьютере: данные не уходят наружу, ключей нет.
Поддерживаются два протокола, без сторонних библиотек:

* ``ollama`` — родной API Ollama (``/api/chat``) со структурированным выводом по JSON-схеме
  (``format``): модель физически не может вернуть ответ другой формы;
* ``openai`` — ``/v1/chat/completions`` с ``response_format: json_schema`` (LM Studio,
  llama.cpp server, vLLM и другие локальные серверы).

Адрес и модель берутся только из настроек (переменные окружения), а не из запроса
пользователя: иначе endpoint превратился бы в прокси на произвольный адрес.
Ответ модели — данные, а не команды: проверка схемы и допустимых значений делается
выше, в сервисе советника.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from backend.core.errors import AppError

DEFAULT_URL = "http://127.0.0.1:11434"
TIMEOUT_SECONDS = 180
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AiUnavailableError(AppError):
    status_code = 503
    code = "ai_unavailable"


class AiNotConfiguredError(AppError):
    status_code = 409
    code = "ai_not_configured"


@dataclass(frozen=True)
class AiSettings:
    provider: str
    base_url: str
    model: str
    enabled: bool

    @classmethod
    def from_env(cls) -> AiSettings:
        provider = (os.environ.get("DATAARENA_AI_PROVIDER") or "ollama").strip().lower()
        base_url = (os.environ.get("DATAARENA_AI_URL") or DEFAULT_URL).strip().rstrip("/")
        model = (os.environ.get("DATAARENA_AI_MODEL") or "").strip()
        enabled = (os.environ.get("DATAARENA_AI_ENABLED") or "1").strip() not in {"0", "false", "no"}
        return cls(provider=provider, base_url=base_url, model=model, enabled=enabled)


@dataclass(frozen=True)
class Completion:
    content: dict[str, Any]
    model: str
    seconds: float
    output_tokens: int | None
    tokens_per_second: float | None


def _request(url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise AiNotConfiguredError("Адрес модели должен начинаться с http:// или https://.")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - адрес из настроек
        url,
        data=data,
        method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - адрес из настроек
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        detail = error.read(2000).decode("utf-8", "replace")
        raise AiUnavailableError(f"Модель ответила статусом {error.code}: {detail[:300]}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AiUnavailableError(
            "Локальная модель не отвечает. Запущена ли Ollama (или другой локальный сервер)?"
        ) from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise AiUnavailableError("Ответ модели слишком большой.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiUnavailableError("Сервер модели вернул не JSON.") from error


def list_models(settings: AiSettings) -> list[str]:
    if settings.provider == "ollama":
        payload = _request(f"{settings.base_url}/api/tags", None, 10)
        return sorted(item.get("name", "") for item in payload.get("models", []) if item.get("name"))
    payload = _request(f"{settings.base_url}/v1/models", None, 10)
    return sorted(item.get("id", "") for item in payload.get("data", []) if item.get("id"))


def complete_json(  # noqa: PLR0913 - параметры запроса к модели
    settings: AiSettings,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    model: str | None = None,
    max_tokens: int = 700,
    timeout: float = TIMEOUT_SECONDS,
) -> Completion:
    """Один запрос со структурированным выводом. Возвращает разобранный JSON-объект."""
    chosen = model or settings.model
    if not chosen:
        raise AiNotConfiguredError(
            "Модель не выбрана: задайте DATAARENA_AI_MODEL (например, qwen3:8b) или выберите модель в интерфейсе."
        )
    started = time.perf_counter()
    if settings.provider == "ollama":
        response = _request(
            f"{settings.base_url}/api/chat",
            {
                "model": chosen,
                "stream": False,
                #рассуждения модели не нужны: ответ короткий и структурированный, а «думающие»
                #модели на процессоре тратили бы на рассуждение минуту
                "think": False,
                "format": schema,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": max_tokens},
                "keep_alive": "10m",
            },
            timeout,
        )
        text = (response.get("message") or {}).get("content") or ""
        tokens = response.get("eval_count")
        eval_seconds = (response.get("eval_duration") or 0) / 1e9
        speed = round(tokens / eval_seconds, 2) if tokens and eval_seconds else None
    else:
        response = _request(
            f"{settings.base_url}/v1/chat/completions",
            {
                "model": chosen,
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_schema", "json_schema": {"name": "advice", "schema": schema}},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            timeout,
        )
        choice = (response.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        tokens = (response.get("usage") or {}).get("completion_tokens")
        speed = None
    seconds = round(time.perf_counter() - started, 2)
    try:
        content = json.loads(text)
    except json.JSONDecodeError as error:
        raise AiUnavailableError("Модель вернула ответ, который не разбирается как JSON.") from error
    if not isinstance(content, dict):
        raise AiUnavailableError("Модель вернула не объект.")
    return Completion(content=content, model=chosen, seconds=seconds, output_tokens=tokens, tokens_per_second=speed)
