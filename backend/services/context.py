"""Сборка адаптеров для сценариев.

Здесь и только здесь `services` знает, каким именно адаптером реализовано хранилище.
Слой `api` обращается за ним сюда, а не к `adapters` напрямую: правило зависимостей
`api -> services -> domain`, `services -> adapters` проверяется тестом, и обход его
из роутера означал бы, что домен снова можно потрогать через транспорт.

Хранилище создаётся один раз на процесс: каждое обращение открывает собственное
соединение SQLite, поэтому объект безопасно переиспользовать, а пересоздавать его
на каждый запрос — значит каждый раз перепроверять схему.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.config import get_settings
from backend.services.running_queries import RunningQueries


@lru_cache(maxsize=1)
def get_workspace_store() -> WorkspaceStore:
    settings = get_settings()
    return WorkspaceStore(settings.workspace_root / "state.db")


@lru_cache(maxsize=1)
def get_running_queries() -> RunningQueries:
    #реестр общий на процесс: отмена приходит другим HTTP-запросом, и найти выполняющийся
    #запрос она может только там, где он зарегистрирован
    return RunningQueries()


def get_workspace_root() -> Path:
    return get_settings().workspace_root


def reset_context() -> None:
    #тестам нужен способ поднять чистое состояние: без сброса кеша второй тест получил бы
    #хранилище первого вместе с его временным каталогом
    get_workspace_store.cache_clear()
    get_running_queries.cache_clear()
