"""Сценарии работы с конвейером: показать, изменить, отменить, посмотреть результат.

Здесь собирается всё вместе: запись конвейера из хранилища, исходный датасет и движок
воспроизведения. Наружу уходит не «состояние UI», а проверенный конвейер: если шаги
нельзя выполнить на текущей схеме, сохранить их нельзя — иначе пользователь получил бы
конвейер, который выглядит сохранённым и не работает.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.domain.pipeline.models import Pipeline, PipelineError, Step
from backend.services.pipeline import engine
from backend.services.pipeline.operations import CASE_MODES, CAST_TARGETS, OPERATIONS
from backend.services.table_view import PageRequest, PageResult, build_page
from backend.services.workspace_access import open_dataset


@dataclass(frozen=True, slots=True)
class PipelineView:
    """Конвейер в том виде, в каком его показывают человеку."""

    pipeline: Pipeline
    steps: list[dict[str, Any]]
    source_columns: tuple[str, ...]
    result_columns: tuple[str, ...]
    result_types: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.pipeline.dataset_id,
            "steps": self.steps,
            "can_undo": bool(self.pipeline.undo),
            "can_redo": bool(self.pipeline.redo),
            "source_columns": list(self.source_columns),
            "result_columns": list(self.result_columns),
            "result_types": self.result_types,
        }


def describe_operations() -> list[dict[str, Any]]:
    """Список операций для интерфейса — из реестра, а не из второго списка в браузере.

    Второй список однажды разошёлся бы с первым, и в интерфейсе появилась бы операция,
    которой на сервере нет.
    """
    return [
        {"kind": operation.kind.value, "title": operation.title}
        for operation in OPERATIONS.values()
    ]


def vocabulary() -> dict[str, Any]:
    """Значения, из которых интерфейс строит формы параметров."""
    return {
        "operations": describe_operations(),
        "cast_targets": sorted(CAST_TARGETS),
        "case_modes": list(CASE_MODES),
    }


def get_view(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str
) -> PipelineView:
    pipeline = store.get_pipeline(dataset_id)

    return _view(store, workspace_root, workspace_id, dataset_id, pipeline)


def set_steps(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    steps: tuple[Step, ...],
) -> PipelineView:
    """Заменяет шаги целиком. Непроходимый конвейер не сохраняется."""
    with _editing(dataset_id):
        current = store.get_pipeline(dataset_id)
        updated = current.with_steps(steps)
        #проверка до записи: сохранённый конвейер, который не выполняется, — это ложь
        #интерфейсу, и обнаружится она позже и не здесь
        view = _view(store, workspace_root, workspace_id, dataset_id, updated)
        store.save_pipeline(updated)

    return view


def undo(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str
) -> PipelineView:
    with _editing(dataset_id):
        updated = store.get_pipeline(dataset_id).undone()
        view = _view(store, workspace_root, workspace_id, dataset_id, updated)
        store.save_pipeline(updated)

    return view


def redo(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str
) -> PipelineView:
    with _editing(dataset_id):
        updated = store.get_pipeline(dataset_id).redone()
        view = _view(store, workspace_root, workspace_id, dataset_id, updated)
        store.save_pipeline(updated)

    return view


def result_page(  # noqa: PLR0913 - адрес датасета и параметры запроса
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    request: PageRequest,
    *,
    steps: tuple[Step, ...] | None = None,
    prefilter: pl.Expr | None = None,
) -> PageResult:
    """Страница результата конвейера — та же таблица, что и у исходного датасета."""
    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    schema = lazy.collect_schema()
    applied = steps if steps is not None else store.get_pipeline(dataset_id).steps
    result = engine.replay(lazy, schema, applied)

    return build_page(result, request, result.collect_schema(), prefilter)


def step_impact(  # noqa: PLR0913 - адрес датасета и параметры запроса
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    step: Step,
    *,
    before: tuple[Step, ...] | None = None,
) -> dict[str, Any]:
    """Последствия шага: сколько строк и значений он унесёт, если его применить."""
    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    schema = lazy.collect_schema()
    existing = before if before is not None else store.get_pipeline(dataset_id).steps

    return engine.impact(lazy, schema, existing, step).to_dict()


def has_steps(store: WorkspaceStore, dataset_id: str) -> bool | None:
    """Есть ли у датасета шаги; `None` — конвейер не читается, и это неизвестно.

    Нужен окну SQL: прежде повреждённый конвейер одного датасета ронял весь список,
    и окно не открывалось ни для одного датасета рабочего пространства.
    """
    try:
        return bool(store.get_pipeline(dataset_id).steps)
    except PipelineError:
        return None


def replayed(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    *,
    steps: tuple[Step, ...] | None = None,
) -> tuple[pl.LazyFrame, pl.Schema]:
    """Результат конвейера как ленивый план — для экспорта, соединений и пакета.

    `steps` — уже прочитанные шаги: тот, кто по ним же ставит метку или пишет рецепт,
    обязан строить данные по тем же шагам, а не перечитывать конвейер второй раз.
    """
    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    schema = lazy.collect_schema()
    chosen = store.get_pipeline(dataset_id).steps if steps is None else steps
    result = engine.replay(lazy, schema, chosen)

    return result, result.collect_schema()


#правки одного конвейера идут по очереди. Чтение, изменение и запись — три шага,
#и без очереди две одновременные правки читали одно и то же состояние: из четырёх
#одновременных отмен срабатывала одна, хотя все четыре отвечали «успешно».
#Замок процесса: DataArena работает одним процессом на одну базу
_EDIT_LOCKS: dict[str, threading.Lock] = {}
_EDIT_LOCKS_GUARD = threading.Lock()


@contextmanager
def _editing(dataset_id: str) -> Iterator[None]:
    with _EDIT_LOCKS_GUARD:
        lock = _EDIT_LOCKS.setdefault(dataset_id, threading.Lock())

    with lock:
        yield


def _view(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    pipeline: Pipeline,
) -> PipelineView:
    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    schema = lazy.collect_schema()
    plans = engine.plan(schema, pipeline.steps)
    result_columns = plans[-1].columns_after if plans else tuple(schema.names())
    types = _result_types(schema, pipeline.steps)

    return PipelineView(
        pipeline=pipeline,
        steps=[item.to_dict() for item in plans],
        source_columns=tuple(schema.names()),
        result_columns=result_columns,
        result_types={name: types[name] for name in result_columns if name in types},
    )


def _result_types(schema: pl.Schema, steps: tuple[Step, ...]) -> dict[str, str]:
    #типы считаются на пустом кадре: данные для этого не нужны, а тип колонки
    #в интерфейсе нужен — без него «привести к числу» выглядит как угадывание
    try:
        empty = engine.replay(pl.LazyFrame(schema=schema), schema, steps)
    except PipelineError:
        return {name: str(dtype) for name, dtype in schema.items()}

    return {name: str(dtype) for name, dtype in empty.collect_schema().items()}
