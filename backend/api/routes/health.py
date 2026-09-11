"""Диагностика датасета."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas.health import FindingResponse, HealthReportResponse
from backend.services.context import get_workspace_root, get_workspace_store
from backend.services.health import report as health_report

router = APIRouter(prefix="/workspaces/{workspace_id}/datasets/{dataset_id}", tags=["health"])


@router.get(
    "/health",
    response_model=HealthReportResponse,
    summary="Диагностика датасета",
    description=(
        "Независимые находки: что не так, насколько это масштабно, почему это важно "
        "и что можно сделать. Сводной оценки качества нет — одно число скрывает, какой "
        "именно дефект важен.\n\n"
        "Отчёт пересчитывается сам, если артефакт датасета или версия проверок "
        "изменились: старый отчёт описывает то, чего уже нет, и показывать его "
        "как актуальный нельзя."
    ),
)
def get_health(workspace_id: str, dataset_id: str) -> dict:
    return health_report.get_report(
        get_workspace_store(), get_workspace_root(), workspace_id, dataset_id
    ).to_dict()


@router.get(
    "/health/findings/{finding_id}",
    response_model=FindingResponse,
    summary="Одна находка",
    description=(
        "Отдельная находка текущего отчёта. Если датасет изменился и отчёт пересчитан, "
        "прежний идентификатор не разрешается: вместо подмены строк возвращается 404."
    ),
)
def get_finding(workspace_id: str, dataset_id: str, finding_id: str) -> dict:
    store = get_workspace_store()
    workspace_root = get_workspace_root()
    report = health_report.get_report(store, workspace_root, workspace_id, dataset_id)
    finding = report.find(finding_id)

    if finding is None:
        raise health_report.FindingNotFoundError(
            "Находка не найдена. Возможно, датасет изменился и диагностика пересчитана.",
            details={"finding_id": finding_id},
        )

    return finding.to_dict(report.dataset_id, report.artifact_fingerprint)
