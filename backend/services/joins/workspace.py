"""Сценарии сборки: анализ, предпросмотр, выполнение и подсказка ключей."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import ValidationError
from backend.core.logging import get_logger
from backend.domain.dataset.models import Dataset
from backend.domain.joins.models import JoinError, JoinSpec, KeyPair
from backend.domain.lineage.models import JoinParents, JoinSide, ParentSnapshot, Provenance
from backend.services import lineage
from backend.services.dataset_import import import_dataset
from backend.services.joins import analysis as key_analysis
from backend.services.joins import suggestions as key_suggestions
from backend.services.joins.execute import build, layout, normalized_keys, prepared, result_of
from backend.services.table_view import PageRequest, PageResult, build_page
from backend.services.upload import safe_display_name

#сколько строк показывать в предпросмотре. Предпросмотр не должен выполнять соединение
#целиком: его смысл — увидеть, что получится, а не получить результат
PREVIEW_ROWS = 50


def analyse(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, spec: JoinSpec
) -> dict[str, Any]:
    left, right = _sides(store, workspace_root, workspace_id, spec)

    return key_analysis.analyse(left, right, spec).to_dict()


def suggest(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    left_dataset_id: str,
    right_dataset_id: str,
) -> dict[str, Any]:
    left, _ = prepared(store, workspace_root, workspace_id, left_dataset_id)
    right, _ = prepared(store, workspace_root, workspace_id, right_dataset_id)

    return {
        "suggestions": [
            item.to_dict()
            for item in key_suggestions.suggest(left, right)
        ]
    }


def preview(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, spec: JoinSpec
) -> PageResult:
    left, right = _sides(store, workspace_root, workspace_id, spec)
    result = build(left, right, spec)

    return build_page(
        result, PageRequest(offset=0, limit=PREVIEW_ROWS), result.collect_schema()
    )


def materialize(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    spec: JoinSpec,
    name: str,
) -> Dataset:
    """Выполняет соединение и регистрирует результат как обычный датасет.

    Результат проходит тем же путём, что и загруженный файл: та же схема, тот же отпечаток,
    тот же псевдоним для SQL. Иначе собранный датасет был бы датасетом второго сорта,
    к которому половина разделов не применима.
    """
    #отпечатки конвейеров родителей — до того, как по ним построены данные: снимок
    #и сами данные обязаны описывать одно и то же состояние
    fingerprints = {
        identifier: lineage.pipeline_fingerprint(store.get_pipeline(identifier).steps)
        for identifier in (spec.left_dataset_id, spec.right_dataset_id)
    }
    left, right = _sides(store, workspace_root, workspace_id, spec)
    analysis = key_analysis.analyse(left, right, spec)

    if analysis.estimated_rows == 0:
        #пустой результат почти всегда означает неверный ключ. Создавать пустой датасет
        #молча — значит дать пользователю уйти с ним в следующий этап
        raise JoinError(
            "Ни одна пара не найдена: результат был бы пустым. Проверьте ключ соединения.",
            details={"estimated_rows": 0},
        )

    #то же решение о колонках, по которому строится результат, идёт в происхождение:
    #иначе карта колонок и сам результат считались бы порознь и разошлись бы
    plan = layout(left.collect_schema(), right.collect_schema(), spec)
    #родители замораживаются до соединения: история ребёнка описывает их такими, какими
    #их соединили, и не меняется, когда их конвейер потом правят или их удаляют
    snapshots = (
        lineage.snapshot_parent(
            store,
            workspace_root,
            workspace_id,
            spec.left_dataset_id,
            role=JoinSide.LEFT,
            columns=plan.left_columns,
        ),
        lineage.snapshot_parent(
            store,
            workspace_root,
            workspace_id,
            spec.right_dataset_id,
            role=JoinSide.RIGHT,
            columns=plan.right_columns,
        ),
    )
    result = result_of(*normalized_keys(left, right, spec), plan)
    staging = Path(tempfile.mkdtemp(prefix="dataarena_join_")) / "result.parquet"

    try:
        #sink_parquet пишет потоково: результат соединения не собирается в памяти целиком
        result.sink_parquet(staging)
        _require_unchanged(store, snapshots, fingerprints)

        #как датасет создан, известно только сейчас: потом это не восстановить ничем.
        #Запись делается вместе с регистрацией, одной транзакцией: отдельной записью после неё
        #происхождение терялось бы при сбое между ними. Сборка — событие, а не адрес
        #содержимого: совпадение байтов с существующим датасетом не делает её им
        provenance = Provenance.joined(
            JoinParents(
                left_dataset_id=spec.left_dataset_id,
                right_dataset_id=spec.right_dataset_id,
                left_name=snapshots[0].dataset_name,
                right_name=snapshots[1].dataset_name,
                kind=spec.kind.value,
                keys=tuple((key.left, key.right) for key in spec.keys),
                right_suffix=spec.right_suffix,
                columns=plan.origins(result.collect_schema().names()),
                parents=snapshots,
            )
        )
        created = import_dataset(
            store,
            workspace_root,
            workspace_id,
            staging,
            _file_name(name),
            provenance=lambda _: provenance,
            content_addressed=False,
        )
    finally:
        #импорт переносит файл к себе; всё, что осталось, — след неудачной сборки,
        #который иначе копился бы во временном каталоге
        shutil.rmtree(staging.parent, ignore_errors=True)

    get_logger().info(
        "Соединение выполнено",
        extra={
            "workspace_id": workspace_id,
            "left": spec.left_dataset_id,
            "right": spec.right_dataset_id,
            "kind": spec.kind.value,
            "estimated_rows": analysis.estimated_rows,
        },
    )

    return created


def _require_unchanged(
    store: WorkspaceStore,
    snapshots: tuple[ParentSnapshot, ...],
    before: dict[str, str],
) -> None:
    """Родителя не изменили, пока шло соединение.

    Данные строятся по конвейеру, прочитанному в начале, снимок делается чуть позже,
    результат пишется ещё позже. Если между этими моментами кто-то сохранил родителю другой
    конвейер, снимок описывал бы не те данные, из которых собран результат, — это ровно
    та ложь, от которой снимок защищает. Поэтому все три отпечатка обязаны совпасть.
    """
    for snapshot in snapshots:
        current = lineage.pipeline_fingerprint(store.get_pipeline(snapshot.dataset_id).steps)

        if not before[snapshot.dataset_id] == snapshot.pipeline_fingerprint == current:
            raise JoinError(
                f"Конвейер датасета «{snapshot.dataset_name}» изменился во время сборки. "
                "Повторите соединение.",
                details={"dataset_id": snapshot.dataset_id},
            )


def _file_name(name: str) -> str:
    """Имя собранного датасета — по тем же правилам, что имя загруженного файла.

    Прежде имя бралось как есть: «../../x» становилось именем датасета, а из него — путём
    архива пакета, и `.dapkg` записывался за пределы временного каталога. Имя только
    для показа: каталоги, управляющие символы и лишняя длина снимаются.
    """
    stem = name.strip() or "Соединение"
    stem = stem if stem.lower().endswith(".parquet") else f"{stem}.parquet"

    try:
        return safe_display_name(stem)
    except ValidationError:
        #имя из одних недопустимых символов: датасет всё равно создаётся, под именем по умолчанию
        return "Соединение.parquet"


def _sides(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, spec: JoinSpec
) -> tuple[pl.LazyFrame, pl.LazyFrame]:
    if spec.left_dataset_id == spec.right_dataset_id:
        raise JoinError(
            "Датасет нельзя соединить сам с собой в этом разделе.",
            details={"dataset_id": spec.left_dataset_id},
        )

    left, _ = prepared(store, workspace_root, workspace_id, spec.left_dataset_id)
    right, _ = prepared(store, workspace_root, workspace_id, spec.right_dataset_id)

    return left, right


def key_pairs(payload: list[dict[str, str]]) -> tuple[KeyPair, ...]:
    return tuple(KeyPair(left=item["left"], right=item["right"]) for item in payload)
