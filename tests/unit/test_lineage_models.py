"""Модель происхождения: запись переживает сохранение без потерь, а мусор не становится ответом."""

from __future__ import annotations

import json

import pytest

from backend.domain.lineage.models import (
    ColumnLineage,
    ColumnLineageEvent,
    ColumnOrigin,
    ColumnRef,
    DatasetLineageEvent,
    EventKind,
    JoinColumn,
    JoinParents,
    JoinSide,
    Origin,
    ParentSnapshot,
    Provenance,
    ProvenanceError,
    ProvenanceStatus,
    UnknownReason,
    UploadSource,
)
from backend.services.lineage import MAX_SNAPSHOT_BYTES, _within_budget

SOURCE = UploadSource("клиенты.csv", "csv", "a" * 64, 120)


def column(name: str, events: int = 1, text: str = "шаг") -> ColumnLineage:
    return ColumnLineage(
        name=name,
        dtype="String",
        origin=ColumnOrigin.SOURCE,
        origins=(ColumnRef("ds_p", "клиенты.csv", name, "клиенты.csv", "a" * 64),),
        events=tuple(
            ColumnLineageEvent(
                kind=EventKind.STEP,
                dataset_id="ds_p",
                dataset_name="клиенты.csv",
                position=index + 1,
                step_id=f"s{index}",
                operation="trim_whitespace",
                description=text,
                inputs=(name,),
                output=name,
            )
            for index in range(events)
        ),
        read_by=(2,),
    )


def snapshot(columns: tuple[ColumnLineage, ...] = (column("email"),)) -> ParentSnapshot:
    return ParentSnapshot(
        role=JoinSide.LEFT,
        dataset_id="ds_p",
        dataset_name="клиенты.csv",
        artifact_sha256="a" * 64,
        artifact_format="csv",
        pipeline_fingerprint="f" * 64,
        provenance_status=ProvenanceStatus.KNOWN,
        provenance_origin=Origin.UPLOAD,
        steps=(
            DatasetLineageEvent(1, "s0", "trim_whitespace", "шаг", {"columns": ["email"]}, ("email",), ()),
        ),
        columns=columns,
    )


def joined() -> Provenance:
    return Provenance.joined(
        JoinParents(
            left_dataset_id="ds_p",
            right_dataset_id="ds_q",
            left_name="клиенты.csv",
            right_name="заказы.csv",
            kind="left",
            keys=(("a", "x"), ("b", "y")),
            right_suffix="_right",
            columns=(JoinColumn("email", JoinSide.LEFT, "email"),),
            parents=(snapshot(),),
        )
    )


@pytest.mark.parametrize(
    "provenance",
    [Provenance.upload(SOURCE), joined(), Provenance.unknown(UnknownReason.MISSING_RECORD)],
    ids=["upload", "join", "unknown"],
)
def test_a_record_survives_storage_unchanged(provenance: Provenance) -> None:
    stored = json.loads(json.dumps(provenance.to_dict(), ensure_ascii=False))

    assert Provenance.from_dict(stored) == provenance


@pytest.mark.parametrize(
    "build",
    [
        #известное без способа создания
        lambda: Provenance(status=ProvenanceStatus.KNOWN),
        #неизвестное с подробностями — то есть выдуманное
        lambda: Provenance(
            status=ProvenanceStatus.LEGACY_UNKNOWN,
            origin=Origin.UPLOAD,
            unknown_reason=UnknownReason.MISSING_RECORD,
        ),
        #соединение без родителей
        lambda: Provenance(status=ProvenanceStatus.KNOWN, origin=Origin.JOIN),
        #файл загрузки у соединения
        lambda: Provenance(
            status=ProvenanceStatus.KNOWN,
            origin=Origin.JOIN,
            join=joined().join,
            source=SOURCE,
        ),
    ],
    ids=["known-without-origin", "unknown-with-origin", "join-without-parents", "join-with-file"],
)
def test_an_inconsistent_record_cannot_even_be_built(build: object) -> None:
    with pytest.raises(ProvenanceError):
        build()  # type: ignore[operator]


def test_the_same_side_twice_is_corruption() -> None:
    document = joined().to_dict()
    document["join"]["parents"] = [snapshot().to_dict(), snapshot().to_dict()]

    with pytest.raises(ProvenanceError):
        Provenance.from_dict(document)


def test_an_oversized_snapshot_keeps_origins_and_says_it_was_truncated() -> None:
    #история колонок родителя не помещается в предел: откуда колонки пришли, важнее,
    #чем что с ними делали по дороге, и сокращение не проходит молча
    heavy = tuple(column(f"c{index}", events=40, text="ш" * 400) for index in range(80))
    oversized = snapshot(heavy)

    assert len(json.dumps(oversized.to_dict(), ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES

    trimmed = _within_budget(oversized)

    assert trimmed.history_truncated
    assert [item.name for item in trimmed.columns] == [item.name for item in heavy]
    assert all(item.events == () and item.origins for item in trimmed.columns)
    assert len(json.dumps(trimmed.to_dict(), ensure_ascii=False).encode()) <= MAX_SNAPSHOT_BYTES


def test_a_snapshot_within_budget_is_left_alone() -> None:
    assert _within_budget(snapshot()) == snapshot()
