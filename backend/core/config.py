"""Единственное место, где читается окружение.

Каждое поле имеет рабочее значение по умолчанию: приложение обязано полностью работать
без единой переменной окружения. Мусорный ввод не роняет старт — опечатка в .env не должна
делать локальный запуск хрупким без всякой пользы.

Поле заводится **вместе с кодом, который его читает**. Настройки под будущие этапы —
бюджет материализации, таймаут SQL, размер страницы — здесь отсутствуют: описанная,
но не читаемая настройка выглядит работающей и молча ни на что не влияет.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_MAX_UPLOAD_MEGABYTES = 512


def _read_int(variable_name: str, default_value: int) -> int:
    #эта функция читает целочисленную настройку и молча возвращает значение по умолчанию на мусорном вводе
    raw_value = os.environ.get(variable_name)

    if raw_value is None:
        return default_value

    try:
        parsed_value = int(raw_value)
    except ValueError:
        return default_value

    return parsed_value if parsed_value > 0 else default_value


@dataclass(frozen=True)
class Settings:
    #эта модель собирает все внешние настройки в одном месте, чтобы модули не читали os.environ вразнобой
    workspace_root: Path
    max_upload_bytes: int
    allowed_origins: tuple[str, ...]
    modelarena_base_url: str | None

    @property
    def modelarena_configured(self) -> bool:
        #DataArena обязана полностью работать без ModelArena: этот флаг влияет только на кнопку перехода
        return self.modelarena_base_url is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    #эта функция собирает настройки один раз за процесс, поэтому конфигурация не меняется между запросами
    workspace_root = Path(
        os.environ.get("DATAARENA_WORKSPACE_ROOT") or (Path.home() / ".dataarena" / "workspaces")
    ).resolve()

    raw_origins = os.environ.get("DATAARENA_ALLOWED_ORIGINS")
    allowed_origins = (
        tuple(origin.strip() for origin in raw_origins.split(",") if origin.strip())
        if raw_origins
        else ("http://localhost:5174", "http://127.0.0.1:5174")
    )

    raw_modelarena = (os.environ.get("DATAARENA_MODELARENA_URL") or "").strip()

    return Settings(
        workspace_root=workspace_root,
        max_upload_bytes=_read_int("DATAARENA_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MEGABYTES) * 1024 * 1024,
        allowed_origins=allowed_origins,
        modelarena_base_url=raw_modelarena or None,
    )
