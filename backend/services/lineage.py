"""Единственный сборщик происхождения: запись о создании + шаги конвейера = история.

Происхождение складывается из двух частей, и только одна из них хранится:

* **запись о создании** (`dataset_provenance`) — загрузка файлом или соединение вместе
  со снимком обоих родителей на момент сборки. Она пишется один раз;
* **шаги конвейера** датасета — они и так сохранены и остаются единственным источником
  истины о преобразованиях. Второго журнала нет: пользователь удалил бы шаг, а в журнале
  он остался бы навсегда, и раздел начал бы врать тем увереннее, чем дольше им пользуются.

Отсюда же берут происхождение интерфейс, пакет `.dapkg` и сама сборка, замораживающая
родителей. Своих реализаций у них нет.

Вывод делается по эффекту каждой операции, а не по угадыванию: операция сама говорит,
какие колонки меняет, создаёт, переименовывает или удаляет, а какие шаги меняют только
строки. Колонки, которые шаг лишь читает, в историю колонки не попадают — сортировка
по колонке не меняет в ней ни одного значения, — а видны отдельно как «читали».

Строки данных здесь не читаются: хватает схемы, шагов и записей хранилища.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import AppError
from backend.domain.dataset.models import Dataset
from backend.domain.lineage.effects import (
    ColumnCreate,
    ColumnDrop,
    ColumnEffect,
    ColumnMutation,
    ColumnRename,
    DatasetOnlyEffect,
)
from backend.domain.lineage.models import (
    ColumnLineage,
    ColumnLineageEvent,
    ColumnOrigin,
    ColumnRef,
    DatasetLineage,
    DatasetLineageEvent,
    EventKind,
    JoinColumn,
    JoinParents,
    JoinSide,
    ParentSnapshot,
    ParentView,
    Provenance,
    UploadSource,
)
from backend.domain.pipeline.models import PipelineError, Step
from backend.services.pipeline import engine
from backend.services.pipeline.operations import operation_for
from backend.services.workspace_access import get_dataset_of_workspace, open_dataset

#предел размера снимка одного родителя. История колонок растёт с цепочкой сборок,
#и без предела запись о происхождении однажды стала бы мегабайтами, которые читаются
#при каждом открытии раздела. Сверх предела история сохраняется без событий, и раздел
#говорит об этом прямо
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024

#фабрика событий одного шага: входы и выход — событие этого шага
_EventFactory = Callable[[tuple[str, ...], str], ColumnLineageEvent]

#пометки о неполноте ответа. Это коды, а не фразы: текст подбирает интерфейс
WARNING_UNKNOWN = "provenance_unknown"
WARNING_NO_COLUMN_MAP = "join_column_map_missing"
WARNING_NO_SNAPSHOTS = "join_parent_snapshots_missing"
WARNING_TRUNCATED = "parent_history_truncated"


def pipeline_fingerprint(steps: tuple[Step, ...]) -> str:
    """Отпечаток конвейера: одинаковые шаги — одинаковый отпечаток, при любом порядке ключей."""
    canonical = json.dumps(
        [step.to_dict() for step in steps],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str
) -> DatasetLineage:
    """Происхождение датасета: как создан, из чего, и что стало с каждой колонкой."""
    dataset = get_dataset_of_workspace(store, workspace_id, dataset_id)
    provenance = store.get_provenance(dataset_id)
    steps = store.get_pipeline(dataset_id).steps
    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    schema = lazy.collect_schema()
    #проверка шагов по порядку: сломанный конвейер — это `PipelineError` с номером шага,
    #то есть тот же ответ, что и в разделе подготовки, а не пятисотка
    engine.plan(schema, steps)
    result_schema = _result_schema(schema, steps)
    warnings: list[str] = []
    initial = _initial_columns(dataset, schema, provenance, warnings)
    events, columns = _fold(dataset.dataset_id, dataset.name, initial, result_schema, steps)

    return DatasetLineage(
        dataset_id=dataset.dataset_id,
        name=dataset.name,
        provenance=provenance,
        artifact=provenance.source or _artifact_of(dataset),
        parents=_parents(store, workspace_id, provenance),
        events=events,
        columns=columns,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def snapshot_parent(  # noqa: PLR0913 - снимок описывается сторона, датасет и колонки
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    *,
    role: JoinSide,
    columns: tuple[str, ...],
) -> ParentSnapshot:
    """Замораживает родителя в том виде, в каком его сейчас соединят.

    История колонок берётся тем же `resolve`, что отвечает интерфейсу, — второй логики
    вычисления здесь нет. Сохраняются только колонки, вошедшие в соединение.
    """
    lineage = resolve(store, workspace_root, workspace_id, dataset_id)
    wanted = set(columns)
    snapshot = ParentSnapshot(
        role=role,
        dataset_id=lineage.dataset_id,
        dataset_name=lineage.name,
        artifact_sha256=lineage.artifact.sha256,
        artifact_format=lineage.artifact.format,
        pipeline_fingerprint=pipeline_fingerprint(store.get_pipeline(dataset_id).steps),
        provenance_status=lineage.provenance.status,
        provenance_origin=lineage.provenance.origin,
        steps=lineage.events,
        columns=tuple(item for item in lineage.columns if item.name in wanted),
    )

    return _within_budget(snapshot)


def _within_budget(snapshot: ParentSnapshot) -> ParentSnapshot:
    if _size(snapshot) <= MAX_SNAPSHOT_BYTES:
        return snapshot

    #сначала уходят события колонок: откуда колонка пришла, важнее, чем что с ней делали
    #по дороге. Затем параметры шагов. Каждое сокращение помечается, а не проходит молча
    trimmed = replace(
        snapshot,
        columns=tuple(replace(item, events=(), read_by=()) for item in snapshot.columns),
        history_truncated=True,
    )

    if _size(trimmed) <= MAX_SNAPSHOT_BYTES:
        return trimmed

    trimmed = replace(trimmed, steps=tuple(replace(item, params={}) for item in trimmed.steps))

    if _size(trimmed) <= MAX_SNAPSHOT_BYTES:
        return trimmed

    return replace(trimmed, steps=(), columns=())


def _size(snapshot: ParentSnapshot) -> int:
    return len(json.dumps(snapshot.to_dict(), ensure_ascii=False).encode("utf-8"))


def _artifact_of(dataset: Dataset) -> UploadSource:
    #собственная запись датасета неизменна с момента импорта: для загрузки это файл,
    #для сборки — сохранённый результат соединения
    return UploadSource(
        file_name=dataset.source.file_name,
        format=dataset.source.format,
        sha256=dataset.source.sha256,
        bytes=dataset.source.bytes,
    )


def _result_schema(schema: pl.Schema, steps: tuple[Step, ...]) -> pl.Schema:
    #типы считаются на пустом кадре: данные для этого не нужны
    return engine.replay(pl.LazyFrame(schema=schema), schema, steps).collect_schema()


# ── родители ──────────────────────────────────────────────────────────────────


def _parents(
    store: WorkspaceStore, workspace_id: str, provenance: Provenance
) -> tuple[ParentView, ...]:
    """Родители — из снимка. Текущий родитель нужен только для двух пометок о нём самом."""
    join = provenance.join

    if join is None:
        return ()

    return tuple(_parent(store, workspace_id, join, side) for side in (JoinSide.LEFT, JoinSide.RIGHT))


def _parent(
    store: WorkspaceStore, workspace_id: str, join: JoinParents, side: JoinSide
) -> ParentView:
    snapshot = join.snapshot(side)
    identifier = join.parent_id(side)
    changed: bool | None = None

    try:
        get_dataset_of_workspace(store, workspace_id, identifier)
    except AppError:
        #родителя удалили или его идентификатор в записи повреждён. История ребёнка
        #от этого не меняется: она держится на снимке, а не на родителе
        available = False
    else:
        available = True

        if snapshot is not None:
            try:
                current = pipeline_fingerprint(store.get_pipeline(identifier).steps)
            except PipelineError:
                current = None

            changed = None if current is None else current != snapshot.pipeline_fingerprint

    return ParentView(
        role=side,
        dataset_id=identifier,
        dataset_name=snapshot.dataset_name if snapshot is not None else join.parent_name(side),
        snapshot=snapshot,
        available=available,
        changed_since_join=changed,
    )


# ── колонки ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Tracked:
    """Колонка по ходу свёртки: как появилась, откуда пришла и что с ней уже случилось."""

    origin: ColumnOrigin
    origins: tuple[ColumnRef, ...]
    events: list[ColumnLineageEvent] = field(default_factory=list)
    read_by: list[int] = field(default_factory=list)


def _initial_columns(
    dataset: Dataset, schema: pl.Schema, provenance: Provenance, warnings: list[str]
) -> dict[str, _Tracked]:
    """Колонки собственных байтов датасета — до его конвейера."""
    if provenance.is_upload:
        artifact = provenance.source or _artifact_of(dataset)

        return {
            name: _Tracked(
                origin=ColumnOrigin.SOURCE,
                origins=(
                    ColumnRef(
                        dataset_id=dataset.dataset_id,
                        dataset_name=dataset.name,
                        column=name,
                        source_file=artifact.file_name,
                        source_sha256=artifact.sha256,
                    ),
                ),
            )
            for name in schema.names()
        }

    join = provenance.join

    if join is None:
        warnings.append(WARNING_UNKNOWN)

        return {name: _Tracked(ColumnOrigin.UNKNOWN, ()) for name in schema.names()}

    if not join.columns:
        warnings.append(WARNING_NO_COLUMN_MAP)

    if len(join.parents) < 2:  # noqa: PLR2004 - у соединения ровно две стороны
        warnings.append(WARNING_NO_SNAPSHOTS)

    if any(item.history_truncated for item in join.parents):
        warnings.append(WARNING_TRUNCATED)

    #поиск по карте и по снимкам — словарями, построенными один раз: перебор списков
    #на каждую колонку давал бы квадрат от ширины на широких соединениях
    sides = {item.name: item for item in join.columns}
    frozen = {
        (snapshot.role, item.name): item for snapshot in join.parents for item in snapshot.columns
    }

    return {name: _joined(dataset, join, sides.get(name), frozen, name) for name in schema.names()}


def _joined(
    dataset: Dataset,
    join: JoinParents,
    entry: JoinColumn | None,
    frozen_columns: dict[tuple[JoinSide, str], ColumnLineage],
    name: str,
) -> _Tracked:
    """Колонка собранного датасета: снимок родителя, затем само соединение.

    Сторона берётся только из карты, сохранённой при сборке. Суффикс в имени этого
    не доказывает — левая колонка тоже может кончаться на «_2».
    """
    if entry is None:
        #карты нет (собран до её появления) или колонка ею не объяснена
        return _Tracked(ColumnOrigin.UNKNOWN, ())

    snapshot = join.snapshot(entry.side)
    parent_name = snapshot.dataset_name if snapshot is not None else join.parent_name(entry.side)
    join_event = ColumnLineageEvent(
        kind=EventKind.JOIN,
        dataset_id=dataset.dataset_id,
        dataset_name=dataset.name,
        position=0,
        step_id="",
        operation="join",
        description=(
            f"{join.kind.upper()} JOIN: «{parent_name}».{entry.source} → {name}"
        ),
        inputs=(entry.source,),
        output=name,
    )
    frozen = frozen_columns.get((entry.side, entry.source))

    if frozen is not None:
        return _Tracked(
            origin=frozen.origin,
            origins=frozen.origins,
            events=[*frozen.events, join_event],
        )

    #снимка нет: известно, какая колонка какого родителя стала этой, но не её прошлое
    #у родителя. Ссылка ведёт на родителя, а не на файл, которого никто не видел
    return _Tracked(
        origin=ColumnOrigin.SOURCE,
        origins=(
            ColumnRef(
                dataset_id=join.parent_id(entry.side),
                dataset_name=parent_name,
                column=entry.source,
                source_sha256=snapshot.artifact_sha256 if snapshot is not None else None,
            ),
        ),
        events=[join_event],
    )


def _fold(
    dataset_id: str,
    dataset_name: str,
    tracked: dict[str, _Tracked],
    result_schema: pl.Schema,
    steps: tuple[Step, ...],
) -> tuple[tuple[DatasetLineageEvent, ...], tuple[ColumnLineage, ...]]:
    """Проводит колонки через шаги конвейера.

    Колонки отслеживаются по текущему имени, и каждый шаг меняет это отображение ровно
    так, как говорит его эффект: переименование переносит запись под новое имя, удаление
    её уничтожает, создание заводит новую. Поэтому колонка, названная именем удалённой,
    начинает с чистого листа, а не с чужого прошлого.
    """
    dataset_events: list[DatasetLineageEvent] = []

    for position, step in enumerate(steps, start=1):
        operation = operation_for(step)
        description = operation.describe(step.params)
        effect = operation.effect(step.params)
        changes = _changes(effect)

        def event(
            inputs: tuple[str, ...],
            output: str,
            position: int = position,
            step: Step = step,
            description: str = description,
        ) -> ColumnLineageEvent:
            return ColumnLineageEvent(
                kind=EventKind.STEP,
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                position=position,
                step_id=step.step_id,
                operation=step.kind.value,
                description=description,
                inputs=inputs,
                output=output,
            )

        reads = tuple(
            name for name in dict.fromkeys(operation.reads(step.params)) if name not in changes
        )

        #«читали» отмечается до применения эффекта: колонку читают под её прежним именем
        for name in reads:
            if name in tracked:
                tracked[name].read_by.append(position)

        _apply(tracked, effect, event, dataset_id)
        dataset_events.append(
            DatasetLineageEvent(
                position=position,
                step_id=step.step_id,
                operation=step.kind.value,
                description=description,
                params=step.params,
                changes=changes,
                reads=reads,
            )
        )

    columns = []

    for name in result_schema.names():
        #колонки, которую свёртка не знает, быть не должно: схема результата получена
        #теми же шагами. Если всё же нет — честнее «неизвестно», чем выдуманный источник
        item = tracked.get(name) or _Tracked(ColumnOrigin.UNKNOWN, ())
        columns.append(
            ColumnLineage(
                name=name,
                dtype=str(result_schema[name]),
                origin=item.origin,
                origins=item.origins,
                events=tuple(item.events),
                read_by=tuple(item.read_by),
            )
        )

    return tuple(dataset_events), tuple(columns)


def _changes(effect: ColumnEffect) -> tuple[str, ...]:
    match effect:
        case ColumnMutation(columns=columns) | ColumnDrop(columns=columns):
            return tuple(dict.fromkeys(columns))
        case ColumnRename(source=source, target=target):
            return (source, target)
        case ColumnCreate(output=output):
            return (output,)
        case DatasetOnlyEffect():
            return ()


def _apply(
    tracked: dict[str, _Tracked],
    effect: ColumnEffect,
    event: _EventFactory,
    dataset_id: str,
) -> None:
    match effect:
        case ColumnMutation(columns=columns):
            for name in dict.fromkeys(columns):
                if name in tracked:
                    tracked[name].events.append(event((name,), name))
        case ColumnRename(source=source, target=target):
            moved = tracked.pop(source, None)

            if moved is not None:
                moved.events.append(event((source,), target))
                tracked[target] = moved
        case ColumnCreate(inputs=inputs, output=output):
            parents = [tracked[name] for name in inputs if name in tracked]
            tracked[output] = _Tracked(
                origin=ColumnOrigin.STEP,
                origins=tuple(dict.fromkeys(ref for parent in parents for ref in parent.origins)),
                #у новой колонки прошлое её входов: копия обрезанной колонки — тоже
                #обрезанная, и скрыть это значило бы потерять объяснение её значений
                events=[*_inherited(parents, dataset_id), event(inputs, output)],
            )
        case ColumnDrop(columns=columns):
            for name in columns:
                tracked.pop(name, None)
        case DatasetOnlyEffect():
            pass


def _inherited(parents: list[_Tracked], dataset_id: str) -> list[ColumnLineageEvent]:
    """Общее прошлое нескольких входов — в хронологическом порядке.

    Сортировать всё по номеру шага нельзя: номера у родителя и у самого датасета
    отсчитываются каждый от единицы, и шаг 3 родителя встал бы после шага 2 ребёнка.
    Поэтому сначала всё, что было до сборки, в порядке входов, затем соединение,
    затем собственные шаги по номеру.
    """
    events = list(dict.fromkeys(item for parent in parents for item in parent.events))
    before = [item for item in events if item.dataset_id != dataset_id]
    joins = [item for item in events if item.dataset_id == dataset_id and item.kind is EventKind.JOIN]
    own = sorted(
        (item for item in events if item.dataset_id == dataset_id and item.kind is EventKind.STEP),
        key=lambda item: item.position,
    )

    return [*before, *joins, *own]

