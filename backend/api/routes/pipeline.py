"""Конвейер преобразований датасета."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from backend.api.schemas.pipeline import (
    ImpactRequest,
    ImpactResponse,
    PipelineResponse,
    StepPayload,
    StepsRequest,
    VocabularyResponse,
)
from backend.api.schemas.workspaces import PageResponse
from backend.domain.pipeline.models import Step
from backend.services import table_view
from backend.services.context import get_workspace_root, get_workspace_store
from backend.services.health import report as health_report
from backend.services.pipeline import workspace as pipeline_service

router = APIRouter(tags=["pipeline"])


def _steps(payloads: list[StepPayload]) -> tuple[Step, ...]:
    #перевод в доменную модель здесь, а не в сервисе: сервис не обязан знать, что шаги
    #пришли по HTTP, а неизвестный вид шага обязан отклоняться на границе
    return tuple(Step.from_dict(payload.model_dump()) for payload in payloads)


@router.get(
    "/pipeline/operations",
    response_model=VocabularyResponse,
    summary="Доступные операции",
    description=(
        "Перечень операций и допустимых значений их параметров. Интерфейс строит формы "
        "по этому ответу, а не по собственному списку: второй список однажды разошёлся бы "
        "с первым, и в интерфейсе появилась бы операция, которой на сервере нет."
    ),
)
def get_operations() -> dict:
    return pipeline_service.vocabulary()


@router.get(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
    response_model=PipelineResponse,
    summary="Конвейер датасета",
    description=(
        "Список шагов вместе с тем, что каждый из них делает со схемой. Исходный датасет "
        "при этом не изменяется никогда: конвейер — это описание, а не изменённые данные."
    ),
)
def get_pipeline(workspace_id: str, dataset_id: str) -> dict:
    return pipeline_service.get_view(
        get_workspace_store(), get_workspace_root(), workspace_id, dataset_id
    ).to_dict()


@router.put(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
    response_model=PipelineResponse,
    summary="Заменить шаги конвейера",
    description=(
        "Конвейер заменяется целиком, прежнее состояние уходит в историю отмены. "
        "Шаги проверяются до записи: конвейер, который нельзя выполнить на текущей схеме, "
        "не сохраняется — иначе он выглядел бы сохранённым и не работал.\n\n"
        "Именно здесь отклоняется перестановка, ломающая зависимость: шаг, которому "
        "нужна колонка, созданная другим шагом, не может стоять раньше него."
    ),
)
def put_pipeline(workspace_id: str, dataset_id: str, payload: StepsRequest) -> dict:
    return pipeline_service.set_steps(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        dataset_id,
        _steps(payload.steps),
    ).to_dict()


@router.post(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/undo",
    response_model=PipelineResponse,
    summary="Отменить последнее изменение конвейера",
    description=(
        "Отмена — это перемещение по истории конвейера, а не откат правок в данных: "
        "данные вообще не менялись. История хранится на сервере и переживает "
        "перезагрузку страницы."
    ),
)
def post_undo(workspace_id: str, dataset_id: str) -> dict:
    return pipeline_service.undo(
        get_workspace_store(), get_workspace_root(), workspace_id, dataset_id
    ).to_dict()


@router.post(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/redo",
    response_model=PipelineResponse,
    summary="Повторить отменённое изменение",
)
def post_redo(workspace_id: str, dataset_id: str) -> dict:
    return pipeline_service.redo(
        get_workspace_store(), get_workspace_root(), workspace_id, dataset_id
    ).to_dict()


@router.post(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/impact",
    response_model=ImpactResponse,
    summary="Последствия шага до его применения",
    description=(
        "Считается по данным, а не по догадке: «удалит 1 240 строк» — это выполненный "
        "подсчёт. Для разрушающих шагов интерфейс обязан показать это до применения."
    ),
)
def post_impact(workspace_id: str, dataset_id: str, payload: ImpactRequest) -> dict:
    return pipeline_service.step_impact(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        dataset_id,
        Step.from_dict(payload.step.model_dump()),
        before=_steps(payload.before) if payload.before is not None else None,
    )


@router.get(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline/rows",
    response_model=PageResponse,
    summary="Страница результата конвейера",
    description=(
        "Та же таблица, что и у исходного датасета, но поверх результата шагов. "
        "Сортировка, фильтры и поиск выполняются на сервере и не материализуют результат "
        "целиком."
    ),
)
def get_pipeline_rows(  # noqa: PLR0913, PLR0917 - параметры повторяют контракт HTTP-запроса
    workspace_id: str,
    dataset_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = table_view.DEFAULT_PAGE_SIZE,
    sort: Annotated[list[str] | None, Query(description="Например column:desc")] = None,
    filter: Annotated[list[str] | None, Query(description="Например amount:gt:100")] = None,
    search: Annotated[str | None, Query()] = None,
    finding: Annotated[
        str | None, Query(description="Показать строки находки диагностики")
    ] = None,
) -> dict:
    store = get_workspace_store()
    workspace_root = get_workspace_root()
    _, schema = pipeline_service.replayed(store, workspace_root, workspace_id, dataset_id)

    request = table_view.parse_page_request(
        schema_names=tuple(schema.names()),
        offset=offset,
        limit=limit,
        sort=sort,
        filters=filter,
        search=search,
    )

    #диагностика проверяет результат конвейера, поэтому и строки находки берутся оттуда же:
    #иначе находка говорила бы об одном, а таблица показывала другое
    prefilter = (
        health_report.resolve_finding(
            store, workspace_root, workspace_id, dataset_id, finding
        ).expression
        if finding
        else None
    )

    return pipeline_service.result_page(
        store, workspace_root, workspace_id, dataset_id, request, prefilter=prefilter
    ).to_dict()
