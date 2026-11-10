"""Происхождение датасета."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas.lineage import LineageResponse
from backend.services import lineage as lineage_service
from backend.services.context import get_workspace_root, get_workspace_store

router = APIRouter(prefix="/workspaces/{workspace_id}/datasets/{dataset_id}", tags=["lineage"])


@router.get(
    "/lineage",
    response_model=LineageResponse,
    summary="Происхождение датасета",
    description=(
        "Откуда взялся датасет и что стало с каждой колонкой.\n\n"
        "История колонки **выводится** из сохранённых шагов, а не хранится отдельно: "
        "вторая запись о том же однажды разошлась бы с первой, и раздел начал бы врать "
        "тем увереннее, чем дольше им пользуются.\n\n"
        "У датасета, собранного соединением, в источниках перечислены оба родителя "
        "и ключ, по которому они сошлись."
    ),
)
def get_lineage(workspace_id: str, dataset_id: str) -> dict:
    return lineage_service.resolve(
        get_workspace_store(), get_workspace_root(), workspace_id, dataset_id
    ).to_dict()
