"""Workspace, датасеты и постраничная выдача таблицы."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile

from backend.api.schemas.workspaces import (
    ColumnStatisticsResponse,
    DatasetListResponse,
    DatasetResponse,
    PageResponse,
    WorkspaceCreateRequest,
    WorkspaceResponse,
)
from backend.core.config import get_settings
from backend.core.errors import ValidationError
from backend.domain.dataset.identifiers import new_workspace_id
from backend.services import table_view
from backend.services.column_stats import compute_column_statistics
from backend.services.context import get_workspace_root, get_workspace_store
from backend.services.dataset_import import import_dataset
from backend.services.health import report as health_report
from backend.services.upload import staged_upload
from backend.services.workspace_access import get_dataset_of_workspace, open_dataset

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=201,
    summary="Создать рабочее пространство",
)
def create_workspace(request: WorkspaceCreateRequest) -> dict:
    store = get_workspace_store()
    workspace = store.create_workspace(new_workspace_id(), request.name.strip())
    return workspace.to_dict()


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    summary="Получить рабочее пространство",
)
def get_workspace(workspace_id: str) -> dict:
    return get_workspace_store().get_workspace(workspace_id).to_dict()


#response_model=None обязателен: при `from __future__ import annotations` аннотация
#`-> None` доходит до FastAPI строкой, он принимает её за модель ответа и падает на
#проверке «у 204 не бывает тела». Ошибка возникает при импорте модуля, то есть роняет
#всё приложение целиком, а не один запрос
@router.delete(
    "/{workspace_id}",
    status_code=204,
    response_model=None,
    summary="Удалить рабочее пространство",
)
def delete_workspace(workspace_id: str) -> None:
    get_workspace_store().delete_workspace(workspace_id)


@router.post(
    "/{workspace_id}/datasets",
    response_model=DatasetResponse,
    status_code=201,
    summary="Загрузить датасет",
    description=(
        "Принимает файл потоком и регистрирует его в рабочем пространстве. Лимит размера "
        "проверяется во время передачи, а не после неё. Повторная загрузка того же "
        "содержимого возвращает существующий датасет, а не создаёт второй."
    ),
)
def upload_dataset(workspace_id: str, file: Annotated[UploadFile, File()]) -> dict:
    store = get_workspace_store()
    workspace_root = get_workspace_root()
    settings = get_settings()

    #workspace проверяется до приёма файла: незачем принимать сотни мегабайт,
    #чтобы затем сообщить, что складывать их некуда
    store.get_workspace(workspace_id)

    with staged_upload(workspace_root, workspace_id, file, settings.max_upload_bytes) as (
        staged_path,
        display_name,
    ):
        dataset = import_dataset(store, workspace_root, workspace_id, staged_path, display_name)

    return dataset.to_dict()


@router.get(
    "/{workspace_id}/datasets",
    response_model=DatasetListResponse,
    summary="Список датасетов рабочего пространства",
)
def list_datasets(workspace_id: str) -> dict:
    store = get_workspace_store()
    store.get_workspace(workspace_id)
    return {"datasets": [dataset.to_dict() for dataset in store.list_datasets(workspace_id)]}


@router.get(
    "/{workspace_id}/datasets/{dataset_id}",
    response_model=DatasetResponse,
    summary="Карточка датасета",
)
def get_dataset(workspace_id: str, dataset_id: str) -> dict:
    return get_dataset_of_workspace(get_workspace_store(), workspace_id, dataset_id).to_dict()


@router.get(
    "/{workspace_id}/datasets/{dataset_id}/rows",
    response_model=PageResponse,
    summary="Страница строк",
    description=(
        "Возвращает одну страницу. Сортировка, фильтрация и поиск выполняются на сервере "
        "и не материализуют датасет целиком. Размер страницы ограничен сверху независимо "
        "от запрошенного значения: миллион строк в браузер не отправляется.\n\n"
        "Параметр `finding` сужает выдачу до строк находки диагностики. Без "
        "дополнительных фильтров это ровно то множество строк, о котором говорит "
        "находка: и подсчёт в ней, и отбор здесь идут через одно и то же описание."
    ),
)
def get_rows(  # noqa: PLR0913, PLR0917 - параметры повторяют контракт HTTP-запроса
    workspace_id: str,
    dataset_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = table_view.DEFAULT_PAGE_SIZE,
    sort: Annotated[list[str] | None, Query(description="Например column:desc")] = None,
    #имя filter совпадает со встроенной функцией, но это часть публичного контракта API
    filter: Annotated[list[str] | None, Query(description="Например amount:gt:100")] = None,
    search: Annotated[str | None, Query()] = None,
    finding: Annotated[str | None, Query(description="Показать строки находки диагностики")] = None,
) -> dict:
    store = get_workspace_store()
    workspace_root = get_workspace_root()
    dataset, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)

    request = table_view.parse_page_request(
        schema_names=dataset.schema.names,
        offset=offset,
        limit=limit,
        sort=sort,
        filters=filter,
        search=search,
    )

    prefilter = None

    if finding:
        prefilter = health_report.resolve_finding(
            store, workspace_root, workspace_id, dataset_id, finding
        ).expression

    return table_view.build_page(lazy, request, lazy.collect_schema(), prefilter).to_dict()


@router.get(
    "/{workspace_id}/datasets/{dataset_id}/columns/{column}",
    response_model=ColumnStatisticsResponse,
    summary="Статистика по колонке",
    description="Считается на сервере по одной колонке: стоимость не зависит от ширины таблицы.",
)
def get_column_statistics(workspace_id: str, dataset_id: str, column: str) -> dict:
    store = get_workspace_store()
    dataset, lazy = open_dataset(store, get_workspace_root(), workspace_id, dataset_id)

    if column not in dataset.schema.names:
        raise ValidationError(
            f"Колонка «{column[:100]}» отсутствует в датасете.",
            details={"column": column[:100]},
        )

    return compute_column_statistics(lazy, column).to_dict()
