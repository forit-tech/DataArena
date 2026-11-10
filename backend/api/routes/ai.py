"""ИИ-советник на локальной модели: статус, совет по находке, совет по сборке."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.services import ai_advisor
from backend.services.context import get_workspace_root, get_workspace_store

router = APIRouter(tags=["ai"])


class ModelChoice(BaseModel):
    model: str | None = Field(default=None, max_length=200, description="Модель из списка локального сервера.")


class PairRequest(ModelChoice):
    left_dataset_id: str = Field(min_length=1, max_length=64)
    right_dataset_id: str = Field(min_length=1, max_length=64)


@router.get(
    "/ai/status",
    summary="Состояние локальной модели",
    description="Включён ли ИИ, отвечает ли локальный сервер, какие модели доступны и что передаётся модели.",
)
def get_status() -> dict[str, Any]:
    return ai_advisor.status()


@router.post(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/health/findings/{finding_id}/ai",
    summary="Совет локальной модели по находке",
    description=(
        "Объяснение и выбор одного из проверенных вариантов исправления. Модель ничего не меняет; "
        "вариант вне списка отбрасывается. Без модели работают советы по правилам."
    ),
)
def post_finding_advice(workspace_id: str, dataset_id: str, finding_id: str, request: ModelChoice) -> dict[str, Any]:
    return ai_advisor.advise_finding(
        get_workspace_store(), get_workspace_root(), workspace_id, dataset_id, finding_id, request.model
    )


@router.post(
    "/workspaces/{workspace_id}/joins/ai",
    summary="Совет локальной модели по сборке пары датасетов",
    description=(
        "Объяснение связи и рекомендация (соединять, агрегировать, не соединять, проверить) "
        "поверх посчитанных приложением связей. Ссылки на несуществующие колонки отбрасываются."
    ),
)
def post_assembly_advice(workspace_id: str, request: PairRequest) -> dict[str, Any]:
    return ai_advisor.advise_assembly(
        get_workspace_store(),
        get_workspace_root(),
        workspace_id,
        request.left_dataset_id,
        request.right_dataset_id,
        request.model,
    )
