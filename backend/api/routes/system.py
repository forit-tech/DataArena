"""Системный раздел API: проверка живости и версия контракта."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas.system import HealthResponse
from backend.core.config import get_settings

router = APIRouter(prefix="/system", tags=["system"])

APP_VERSION = "0.1.0"
#версия контракта Dataset Package, которую производит этот сервис; см. docs/contracts/
PACKAGE_FORMAT = "dataarena.package"
PACKAGE_VERSION = "1.0.0"
FINGERPRINT_ALGORITHM = "dataarena-logical-sha256-v1"


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Проверка живости backend",
    description=(
        "Быстрый ответ для интерфейса и контейнерного healthcheck. Не читает датасеты "
        "и не считает ничего тяжёлого. Дополнительно сообщает версию контракта Dataset Package, "
        "чтобы её можно было сверить без чтения файлов."
    ),
)
def health_check() -> dict:
    #этот endpoint нужен интерфейсу, чтобы отличать «backend не запущен» от «файл не читается»
    settings = get_settings()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "workspace_root": str(settings.workspace_root),
        "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
        "modelarena_configured": settings.modelarena_configured,
        "contract": {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION,
            "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        },
    }
