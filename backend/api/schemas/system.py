"""Схемы API системного раздела.

Схемы API живут отдельно от доменных моделей намеренно: внутреннее представление
можно менять, не ломая контракт с frontend.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str = Field(description="Машиночитаемый код ошибки.")
    message: str = Field(description="Сообщение для пользователя, без внутренностей стека.")
    details: dict[str, Any] = Field(default_factory=dict, description="Дополнительные поля ошибки.")


class ErrorResponse(BaseModel):
    #единственная структура ошибки для всего API; detail дублирует message для совместимости
    error: ErrorBody
    detail: str
    request_id: str | None = Field(
        default=None,
        description="Идентификатор запроса: по нему сбой находится в логе backend.",
    )


class ContractInfo(BaseModel):
    format: str = Field(description="Идентификатор формата Dataset Package.")
    version: str = Field(description="Версия спецификации, которую производит этот сервис.")
    fingerprint_algorithm: str = Field(description="Идентификатор алгоритма отпечатка.")


class HealthResponse(BaseModel):
    status: str
    version: str
    workspace_root: str
    max_upload_mb: int
    modelarena_configured: bool = Field(
        description="DataArena работает и без ModelArena; флаг влияет только на кнопку перехода."
    )
    contract: ContractInfo
