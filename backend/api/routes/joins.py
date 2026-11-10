"""Сборка датасета из нескольких источников."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas.joins import (
    JoinAnalysisResponse,
    JoinRequest,
    MaterializeRequest,
    SuggestionsResponse,
)
from backend.api.schemas.workspaces import DatasetResponse, PageResponse
from backend.domain.joins.models import JoinSpec
from backend.services import assembly
from backend.services.context import get_workspace_root, get_workspace_store
from backend.services.joins import workspace as joins_service

router = APIRouter(prefix="/workspaces/{workspace_id}/joins", tags=["joins"])


@router.post(
    "/analyse",
    response_model=JoinAnalysisResponse,
    summary="Анализ ключей и прогноз размера",
    description=(
        "Отвечает на вопросы, из-за которых соединения портят данные: совпадают ли типы "
        "ключей, сколько строк найдут пару, уникален ли ключ с каждой стороны и **сколько "
        "строк получится**.\n\n"
        "Число строк здесь посчитано, а не прикинуто: для каждого значения ключа известно, "
        "сколько раз оно встречается с каждой стороны. Связь N:M умножает строки, и увидеть "
        "это нужно до выполнения, а не после."
    ),
)
def post_analyse(workspace_id: str, payload: JoinRequest) -> dict:
    return joins_service.analyse(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        JoinSpec.from_dict(payload.model_dump()),
    )


@router.post(
    "/suggest",
    response_model=SuggestionsResponse,
    summary="Подсказка пар ключей",
    description=(
        "Пары колонок, которые похожи на ключ, с объяснением по каждой: доля общих "
        "значений, уникальность с каждой стороны, совпадение имён, совместимость типов. "
        "Слабая подсказка так и называется слабой — «модель считает это хорошим "
        "соединением» здесь не ответ."
    ),
)
def post_suggest(workspace_id: str, left_dataset_id: str, right_dataset_id: str) -> dict:
    return joins_service.suggest(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        left_dataset_id,
        right_dataset_id,
    )


@router.post(
    "/preview",
    response_model=PageResponse,
    summary="Предпросмотр результата",
    description="Первые строки результата. Соединение целиком при этом не выполняется.",
)
def post_preview(workspace_id: str, payload: JoinRequest) -> dict:
    return joins_service.preview(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        JoinSpec.from_dict(payload.model_dump()),
    ).to_dict()


@router.post(
    "",
    response_model=DatasetResponse,
    status_code=201,
    summary="Выполнить соединение и сохранить результат",
    description=(
        "Результат становится обычным датасетом рабочего пространства: с той же схемой, "
        "тем же псевдонимом для SQL и своим конвейером. Соединение выполняется поверх "
        "результатов конвейеров обеих сторон — то есть поверх почищенных данных, "
        "а не сырых файлов."
    ),
)
def post_join(workspace_id: str, payload: MaterializeRequest) -> dict:
    dataset = joins_service.materialize(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        JoinSpec.from_dict(payload.model_dump()),
        payload.name,
    )

    return dataset.to_dict()


@router.get(
    "/discover",
    summary="Подобрать сборку автоматически",
    description=(
        "Исследует все датасеты рабочего пространства: профиль колонок, связи по реальному "
        "пересечению значений, точный прогноз числа строк для каждого кандидата, составные ключи, "
        "вердикт (соединять, агрегировать, проверить, связи нет), гипотезы о назначении и кандидаты "
        "в целевую переменную с подозрениями на утечку. Ничего не создаёт и не меняет."
    ),
)
def get_discover(workspace_id: str) -> dict:
    return assembly.discover(get_workspace_store(), get_workspace_root(), workspace_id)
