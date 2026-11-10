"""Доступ к данным датасета с проверкой, что он всё ещё существует.

Между проверкой и чтением проходит время. Датасет может быть удалён другим запросом,
файл может исчезнуть, артефакт может быть пересобран. Наивный код проверяет наличие,
затем открывает файл — и падает с `FileNotFoundError`, то есть ответом 500 вместо 404.

Здесь окно между проверкой и использованием не устраняется — оно неустранимо в файловой
системе, — а **обрабатывается**: исчезнувший файл превращается в ту же доменную ошибку,
что и отсутствующая запись.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import polars as pl

from backend.adapters.formats.registry import scan_dataset
from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import DatasetNotFoundError, DatasetReadError
from backend.domain.dataset.identifiers import ensure_dataset_id, ensure_workspace_id
from backend.domain.dataset.models import Dataset, DatasetStatus
from backend.services.dataset_import import workspace_paths


def delete_workspace(store: WorkspaceStore, workspace_root: Path, workspace_id: str) -> None:
    """Удаляет рабочее пространство — и записи, и его файлы на диске.

    Прежде удалялись только записи: исходники и производные файлы удалённого пространства
    оставались на диске навсегда, хотя пользователь просил их удалить. Файлы, на которые
    по старым записям ссылается другое пространство, остаются — иначе оно лишилось бы данных.
    """
    ensure_workspace_id(workspace_id)
    keep = {Path(path).resolve() for path in store.paths_used_by_other_workspaces(workspace_id)}
    store.delete_workspace(workspace_id)
    directory = (workspace_root / workspace_id).resolve()

    if not directory.is_dir() or directory.parent != workspace_root.resolve():
        return

    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.resolve() in keep:
            continue

        if path.is_dir():
            #каталог удаляется, только если опустел: в нём мог остаться сохранённый файл
            with contextlib.suppress(OSError):
                path.rmdir()
        else:
            path.unlink(missing_ok=True)

    with contextlib.suppress(OSError):
        directory.rmdir()


def delete_dataset(store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str) -> None:
    """Удаляет датасет: запись (с каскадом) и его файлы в sources/ и derived/.

    Файл, на который по старым записям ссылается другое пространство, остаётся.
    Файл, занятый процессом SQL (Windows не даёт удалить открытый файл), тоже остаётся:
    запись уже удалена, и пользователь датасета больше не видит, а сирота на диске
    не ломает ничего.
    """
    get_dataset_of_workspace(store, workspace_id, dataset_id)
    keep = {Path(path).resolve() for path in store.paths_used_by_other_workspaces(workspace_id)}
    store.delete_dataset(workspace_id, dataset_id)
    base = (workspace_root / workspace_id).resolve()

    if base.parent != workspace_root.resolve():
        return

    for folder in ("sources", "derived"):
        for path in (base / folder).glob(f"{dataset_id}.*"):
            if path.resolve() in keep or not path.is_file():
                continue
            with contextlib.suppress(OSError):
                path.unlink()


def get_dataset_of_workspace(store: WorkspaceStore, workspace_id: str, dataset_id: str) -> Dataset:
    #принадлежность проверяется явно: датасет чужого workspace обязан выглядеть
    #ровно как несуществующий, иначе перебор идентификаторов сообщал бы о чужих данных
    ensure_workspace_id(workspace_id)
    ensure_dataset_id(dataset_id)

    dataset = store.get_dataset(dataset_id)

    if dataset.workspace_id != workspace_id:
        raise DatasetNotFoundError(
            f"Датасет {dataset_id} не найден.",
            details={"dataset_id": dataset_id, "workspace_id": workspace_id},
        )

    return dataset


def reading_path(workspace_root: Path, dataset: Dataset) -> Path:
    #выше по стеку никто не знает, читаем ли мы исходный файл или нормализованный
    if dataset.derived is not None:
        return Path(dataset.derived.path)

    from backend.adapters.formats.registry import capabilities_for_key

    paths = workspace_paths(
        workspace_root,
        dataset.workspace_id,
        dataset.dataset_id,
        capabilities_for_key(dataset.source.format),
    )
    return paths.source


def open_dataset(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str
) -> tuple[Dataset, pl.LazyFrame]:
    #эта функция — единственная точка, где датасет открывается для чтения
    dataset = get_dataset_of_workspace(store, workspace_id, dataset_id)

    if dataset.status is not DatasetStatus.READY:
        raise DatasetReadError(
            f"Датасет недоступен для чтения: {dataset.status_reason or dataset.status.value}.",
            details={"dataset_id": dataset_id, "status": dataset.status.value},
        )

    path = reading_path(workspace_root, dataset)

    if not path.exists():
        #запись есть, файла нет: датасет удалён между проверкой и чтением, либо хранилище
        #повреждено. И то и другое для клиента означает «этого датасета больше нет»,
        #а не внутреннюю ошибку сервера
        raise DatasetNotFoundError(
            f"Файл датасета {dataset_id} недоступен. Возможно, он был удалён.",
            details={"dataset_id": dataset_id},
        )

    return dataset, scan_dataset(path)
