"""Диагностика датасета: счёт, кэш и разрешение находки в предикат.

Health ничего не изменяет. Он читает артефакт, считает находки и складывает их рядом
с датасетом — исходный файл при этом не открывается на запись ни разу.

Отчёт пересчитывается сам, когда перестаёт соответствовать данным: сменился артефакт
или сменилась версия проверок. Показывать старый отчёт как актуальный нельзя — он
описывает то, чего уже нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import AppError
from backend.core.logging import get_logger
from backend.domain.health.models import (
    CHECKS_VERSION,
    Finding,
    HealthReport,
)
from backend.services.health.checks import analyse
from backend.services.health.predicates import row_filter_expression
from backend.services.workspace_access import open_dataset


class FindingNotFoundError(AppError):
    #находка не принадлежит текущему отчёту: либо её никогда не было, либо артефакт
    #пересобран и прежние идентификаторы больше не разрешаются. Показывать вместо неё
    #другие строки нельзя — это была бы тихая подмена
    status_code = 404
    code = "finding_not_found"


@dataclass(frozen=True, slots=True)
class ResolvedFinding:
    """Находка вместе с готовым выражением для отбора её строк."""

    finding: Finding
    expression: pl.Expr


def get_report(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
) -> HealthReport:
    """Возвращает актуальный отчёт, пересчитывая его при необходимости."""
    dataset, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    fingerprint = dataset.artifact_fingerprint
    stored = store.get_health(dataset_id)

    if (
        stored is not None
        and stored.artifact_fingerprint == fingerprint
        and stored.checks_version == CHECKS_VERSION
    ):
        return stored

    return store.save_health(_compute(dataset_id, fingerprint, lazy))


def resolve_finding(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    finding_id: str,
) -> ResolvedFinding:
    """Находит находку по идентификатору и превращает её описание в выражение.

    Выражение строится той же функцией, которой считались затронутые строки при
    составлении отчёта. Это и есть обещание «в таблице ровно те строки, о которых
    говорит находка»: не похожая логика, а один и тот же путь.
    """
    report = get_report(store, workspace_root, workspace_id, dataset_id)
    finding = report.find(finding_id)

    if finding is None:
        raise FindingNotFoundError(
            "Находка не найдена. Возможно, датасет изменился и диагностика пересчитана.",
            details={"finding_id": finding_id},
        )

    if finding.row_filter is None:
        raise FindingNotFoundError(
            "Эта находка относится ко всему датасету, отдельных строк у неё нет.",
            details={"finding_id": finding_id, "code": finding.code.value},
        )

    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)

    return ResolvedFinding(
        finding=finding,
        expression=row_filter_expression(finding.row_filter, lazy.collect_schema()),
    )


def _compute(dataset_id: str, fingerprint: str, lazy: pl.LazyFrame) -> HealthReport:
    started = datetime.now(UTC)
    schema = lazy.collect_schema()
    total_rows = int(lazy.select(pl.len()).collect().item())
    findings = analyse(lazy, schema, total_rows)

    get_logger().info(
        "Диагностика посчитана",
        extra={
            "dataset_id": dataset_id,
            "rows": total_rows,
            "columns": len(schema),
            "findings": len(findings),
            "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
        },
    )

    return HealthReport(
        dataset_id=dataset_id,
        artifact_fingerprint=fingerprint,
        checks_version=CHECKS_VERSION,
        computed_at=started,
        total_rows=total_rows,
        findings=tuple(findings),
    )
