"""Конфигурация и structured logging."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from backend.core import config
from backend.core.logging import JsonFormatter, configure_logging, get_logger


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_application_works_without_any_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    #требование: приложение обязано полностью стартовать без единой переменной окружения
    for name in [key for key in dict(__import__("os").environ) if key.startswith("DATAARENA_")]:
        monkeypatch.delenv(name, raising=False)

    settings = config.get_settings()

    assert settings.max_upload_bytes > 0
    assert settings.workspace_root.is_absolute()
    assert settings.allowed_origins
    assert settings.modelarena_base_url is None


def test_garbage_in_environment_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    #опечатка в .env не должна ронять старт: это делает локальный запуск хрупким без всякой пользы
    monkeypatch.setenv("DATAARENA_MAX_UPLOAD_MB", "не число")

    assert config.get_settings().max_upload_bytes == config.DEFAULT_MAX_UPLOAD_MEGABYTES * 1024 * 1024


def test_non_positive_value_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    #ноль и отрицательные значения бессмысленны для лимита и трактуются как отсутствие настройки
    monkeypatch.setenv("DATAARENA_MAX_UPLOAD_MB", "0")

    assert config.get_settings().max_upload_bytes == config.DEFAULT_MAX_UPLOAD_MEGABYTES * 1024 * 1024


def test_every_documented_variable_is_actually_read() -> None:
    #описанная, но не читаемая переменная выглядит работающей и молча ни на что не влияет,
    #поэтому .env.example и код обязаны совпадать
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and "=" in line
    }
    source = (Path(__file__).resolve().parents[2] / "backend" / "core" / "config.py").read_text(
        encoding="utf-8"
    )

    unread = {name for name in documented if name not in source}

    assert not unread, f"переменные документированы, но нигде не читаются: {sorted(unread)}"


def test_modelarena_url_is_optional_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAARENA_MODELARENA_URL", "  http://127.0.0.1:5175  ")
    settings = config.get_settings()

    assert settings.modelarena_base_url == "http://127.0.0.1:5175"
    assert settings.modelarena_configured is True


def test_log_record_is_one_json_line_with_extra_fields() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dataarena.test")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info("Датасет открыт", extra={"dataset_id": "ds_1", "rows": 1000})

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "Датасет открыт"
    assert payload["level"] == "info"
    assert payload["dataset_id"] == "ds_1"
    assert payload["rows"] == 1000
    assert payload["timestamp"].endswith("+00:00")


def test_configure_logging_does_not_duplicate_handlers() -> None:
    #считаем только свои обработчики: pytest подмешивает в тот же логгер несколько собственных,
    #и проверка «ровно один обработчик» ловила бы окружение, а не поведение кода
    configure_logging()
    configure_logging()

    own_handlers = [
        handler for handler in get_logger().handlers if isinstance(handler.formatter, JsonFormatter)
    ]

    assert len(own_handlers) == 1
