"""Окно SQL: выполнение, отмена, история, сохранённые запросы."""

from __future__ import annotations

from contextlib import nullcontext

from fastapi import APIRouter, Query

from backend.api.schemas.sql import (
    DatasetBindingsResponse,
    QueryHistoryResponse,
    QueryRequest,
    QueryResponse,
    SavedQueryListResponse,
    SavedQueryRequest,
    SavedQueryResponse,
)
from backend.domain.dataset.models import DatasetStatus
from backend.services.context import get_running_queries, get_workspace_root, get_workspace_store
from backend.services.running_queries import ensure_query_token
from backend.services.sql_workspace import SqlWorkspace, limits_for

router = APIRouter(prefix="/workspaces/{workspace_id}/sql", tags=["sql"])


def _workspace(workspace_id: str) -> SqlWorkspace:
    return SqlWorkspace(
        store=get_workspace_store(),
        workspace_root=get_workspace_root(),
        workspace_id=workspace_id,
    )


@router.get(
    "/datasets",
    response_model=DatasetBindingsResponse,
    summary="Датасеты, доступные в запросах",
    description=(
        "Псевдонимы, которые можно написать в FROM. Пути к файлам в запрос не передаются: "
        "связывание псевдонима с артефактом выполняет backend."
    ),
)
def list_bindings(workspace_id: str) -> dict:
    store = get_workspace_store()
    store.get_workspace(workspace_id)

    return {
        "datasets": [
            {
                "alias": dataset.alias,
                "dataset_id": dataset.dataset_id,
                "name": dataset.name,
                "row_count": dataset.row_count,
                "column_count": dataset.column_count,
                "columns": list(dataset.schema.names),
                "queryable": dataset.status is DatasetStatus.READY,
            }
            for dataset in store.list_datasets(workspace_id)
        ]
    }


@router.post(
    "/queries",
    response_model=QueryResponse,
    summary="Выполнить запрос",
    description=(
        "Выполняет один SELECT над датасетами рабочего пространства. Запрос попадает "
        "в историю независимо от исхода. Результат ограничен по числу строк и по объёму; "
        "усечение всегда обозначается явно."
    ),
)
def run_query(workspace_id: str, request: QueryRequest) -> dict:
    workspace = _workspace(workspace_id)
    registry = get_running_queries()
    limits = limits_for(request.row_limit, request.timeout_seconds)

    #место в реестре занимается, только если клиент прислал метку. Без метки отменять
    #нечем, и делать вид, что запрос управляем, нельзя
    slot = (
        registry.slot(workspace_id, ensure_query_token(request.query_token))
        if request.query_token
        else nullcontext(None)
    )

    with slot as handle:
        outcome = workspace.run(request.sql, limits=limits, handle=handle)

    return {"result": outcome.result.to_dict(), "run": outcome.run.to_dict()}


@router.delete(
    "/queries/{query_token}",
    status_code=204,
    response_model=None,
    summary="Отменить выполняющийся запрос",
    description=(
        "Прерывает запрос с указанной меткой. Если запрос уже завершился, возвращается 404: "
        "отменять нечего, и это не ошибка сервера."
    ),
)
def cancel_query(workspace_id: str, query_token: str) -> None:
    get_workspace_store().get_workspace(workspace_id)
    get_running_queries().cancel(workspace_id, query_token)


@router.get(
    "/history",
    response_model=QueryHistoryResponse,
    summary="История запросов",
    description=(
        "Что запускалось и чем закончилось. Строки результата не хранятся: данные меняются, "
        "и показать вчерашний результат под сегодняшним запросом значило бы соврать."
    ),
)
def list_history(workspace_id: str, limit: int = Query(default=50, ge=1, le=500)) -> dict:
    store = get_workspace_store()
    store.get_workspace(workspace_id)

    return {"runs": [run.to_dict() for run in store.list_query_runs(workspace_id, limit=limit)]}


@router.get(
    "/saved",
    response_model=SavedQueryListResponse,
    summary="Сохранённые запросы",
)
def list_saved(workspace_id: str) -> dict:
    store = get_workspace_store()
    store.get_workspace(workspace_id)

    return {"queries": [item.to_dict() for item in store.list_saved_queries(workspace_id)]}


@router.post(
    "/saved",
    response_model=SavedQueryResponse,
    status_code=201,
    summary="Сохранить запрос",
    description=(
        "Текст проверяется разбором до сохранения. Датасеты при этом не проверяются: "
        "они могут появиться позже, и заготовку сохранить нужно уметь."
    ),
)
def save_query(workspace_id: str, request: SavedQueryRequest) -> dict:
    saved = _workspace(workspace_id).save(request.name, request.sql, request.description)

    return saved.to_dict()


@router.put(
    "/saved/{saved_query_id}",
    response_model=SavedQueryResponse,
    summary="Изменить сохранённый запрос",
)
def update_saved(workspace_id: str, saved_query_id: str, request: SavedQueryRequest) -> dict:
    updated = _workspace(workspace_id).update(
        saved_query_id, request.name, request.sql, request.description
    )

    return updated.to_dict()


@router.delete(
    "/saved/{saved_query_id}",
    status_code=204,
    response_model=None,
    summary="Удалить сохранённый запрос",
)
def delete_saved(workspace_id: str, saved_query_id: str) -> None:
    get_workspace_store().delete_saved_query(workspace_id, saved_query_id)
