"""Диагностика датасета: счёт, кэш и разрешение находки в предикат.

Health ничего не изменяет. Он читает артефакт, считает находки и складывает их рядом
с датасетом — исходный файл при этом не открывается на запись ни разу.

Отчёт пересчитывается сам, когда перестаёт соответствовать данным: сменился артефакт
или сменилась версия проверок. Показывать старый отчёт как актуальный нельзя — он
описывает то, чего уже нет.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
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
from backend.services.pipeline.workspace import replayed
from backend.services.workspace_access import get_dataset_of_workspace, reading_path

#по одному замку на датасет: восемь вкладок, открытых одновременно, считали отчёт
#восемь раз. Целостность при этом не страдала — побеждала последняя запись, — но на
#датасете, где расчёт стоит секунды и сотни мегабайт, это восьмикратная плата за одно
#и то же. Замок берётся только на время расчёта; чтение готового отчёта его не касается
_COMPUTE_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _compute_lock(dataset_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _COMPUTE_LOCKS.setdefault(dataset_id, threading.Lock())


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


def state_stamp(path: Path, steps_document: str) -> str:
    """Метка состояния данных: файл плюс шаги конвейера.

    Диагностика описывает то, что пользователь видит, — результат шагов. Значит, ключ
    кэша обязан меняться и при смене файла, и при смене шагов: иначе человек, убравший
    разнописание регистра, снова увидел бы находку о разнописании и перестал бы верить
    разделу.

    Про файл: отпечаток артефакта хранится в записи датасета и не меняется, если файл
    подменили мимо DataArena.

    Отпечаток артефакта хранится в записи датасета и не меняется, если файл подменили
    мимо DataArena — а подменить его можно, это обычный файл на диске. Тогда отчёт
    описывал бы данные, которых больше нет, и провал в строки показывал бы другие строки,
    чем обещает находка. Проверено измерением: отчёт переживал полную подмену содержимого.

    Хэшировать файл на каждый запрос нельзя — это проход по гигабайтам ради проверки.
    Размер и время правки меняются при любой перезаписи и стоят один системный вызов.
    """
    digest = sha256(steps_document.encode("utf-8")).hexdigest()[:16]

    try:
        info = path.stat()
    except OSError:
        #файла нет — открытие датасета отдаст доменную ошибку раньше, чем метка
        #понадобится. Пустая метка не совпадёт ни с одной сохранённой
        return ""

    return f"{info.st_size}:{info.st_mtime_ns}:{digest}"


def get_report(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
) -> HealthReport:
    """Возвращает актуальный отчёт, пересчитывая его при необходимости.

    Проверяется **результат конвейера**, а не исходный файл: раздел отвечает на вопрос
    «что не так с данными, с которыми я работаю», и после исправления шагом находка
    обязана исчезнуть.
    """
    dataset = get_dataset_of_workspace(store, workspace_id, dataset_id)
    #конвейер читается один раз: по нему строятся и данные, и метка отчёта. Прежде это
    #были два чтения, и правка между ними сохраняла отчёт по старым шагам под новой
    #меткой — он считался актуальным до следующей правки
    pipeline = store.get_pipeline(dataset_id)
    lazy, _ = replayed(store, workspace_root, workspace_id, dataset_id, steps=pipeline.steps)
    fingerprint = dataset.artifact_fingerprint
    stamp = state_stamp(
        reading_path(workspace_root, dataset),
        json.dumps(pipeline.to_storage()["steps"], ensure_ascii=False),
    )
    stored = store.get_health(dataset_id)

    if _is_current(stored, fingerprint, stamp):
        return stored  # type: ignore[return-value]

    with _compute_lock(dataset_id):
        #пока мы ждали замок, отчёт мог посчитать сосед: проверяем ещё раз, иначе
        #очередь из восьми запросов посчитала бы одно и то же восемь раз подряд
        fresh = store.get_health(dataset_id)

        if _is_current(fresh, fingerprint, stamp):
            return fresh  # type: ignore[return-value]

        return store.save_health(_compute(dataset_id, fingerprint, stamp, lazy))


def _is_current(stored: HealthReport | None, fingerprint: str, stamp: str) -> bool:
    return (
        stored is not None
        and stored.artifact_fingerprint == fingerprint
        and stored.checks_version == CHECKS_VERSION
        and stored.artifact_stamp == stamp
    )


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

    lazy, _ = replayed(store, workspace_root, workspace_id, dataset_id)

    return ResolvedFinding(
        finding=finding,
        expression=row_filter_expression(finding.row_filter, lazy.collect_schema()),
    )


def _compute(dataset_id: str, fingerprint: str, stamp: str, lazy: pl.LazyFrame) -> HealthReport:
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
        artifact_stamp=stamp,
    )
