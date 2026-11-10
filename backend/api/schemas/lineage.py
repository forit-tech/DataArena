"""Схемы происхождения датасета."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ColumnRefResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    column: str
    source_file: str | None = Field(description="Имя загруженного файла, если колонка из файла.")
    source_sha256: str | None = Field(description="Отпечаток байтов, из которых прочитана колонка.")


class ColumnEventResponse(BaseModel):
    kind: str = Field(description="step — шаг конвейера, join — соединение.")
    dataset_id: str = Field(description="Датасет, в котором произошло событие.")
    dataset_name: str
    position: int = Field(description="Номер шага в конвейере своего датасета; у соединения 0.")
    step_id: str
    operation: str
    description: str
    inputs: list[str]
    output: str


class DatasetEventResponse(BaseModel):
    position: int
    step_id: str
    operation: str
    description: str
    params: dict[str, Any]
    changes: list[str] = Field(description="Колонки, значения или имя которых шаг изменил.")
    reads: list[str] = Field(description="Колонки, которые шаг только читал.")


class ColumnLineageResponse(BaseModel):
    name: str
    dtype: str
    origin: str = Field(
        description=(
            "source — колонка исходного файла, step — создана шагом, unknown — "
            "происхождение не установить, и оно не угадывается."
        )
    )
    origins: list[ColumnRefResponse] = Field(
        description="Колонки-источники. Пусто, если происхождение неизвестно."
    )
    events: list[ColumnEventResponse] = Field(
        description=(
            "События, изменившие значения или имя колонки, в хронологическом порядке: "
            "шаги родителя до сборки, соединение, шаги самого датасета. Шаги, менявшие "
            "только строки (отбор, сортировка, дедупликация), сюда не попадают."
        )
    )
    read_by: list[int] = Field(description="Номера шагов, которые колонку только читали.")
    source_name: str = Field(description="Имя, под которым колонка пришла, если источник один.")


class ArtifactResponse(BaseModel):
    file_name: str
    format: str
    sha256: str
    bytes: int


class JoinColumnResponse(BaseModel):
    name: str
    side: str
    source: str


class JoinResponse(BaseModel):
    kind: str
    keys: list[dict[str, str]] = Field(description="Пары ключа, все сразу: составной ключ — список.")
    right_suffix: str
    columns: list[JoinColumnResponse] = Field(
        description="Карта «колонка результата → сторона и имя на ней», сохранённая при сборке."
    )


class ParentSnapshotResponse(BaseModel):
    artifact_sha256: str
    artifact_format: str
    pipeline_fingerprint: str
    provenance_status: str
    provenance_origin: str | None
    steps: list[DatasetEventResponse]
    history_truncated: bool


class ParentCurrentResponse(BaseModel):
    available: bool = Field(description="Существует ли родитель сейчас.")
    changed_since_join: bool | None = Field(
        description="Изменён ли конвейер родителя после сборки; null — сравнить не с чем."
    )


class ParentResponse(BaseModel):
    role: str
    dataset_id: str
    dataset_name: str = Field(description="Имя на момент сборки.")
    snapshot: ParentSnapshotResponse | None = Field(
        description="Состояние родителя на момент сборки; null у датасетов, собранных раньше."
    )
    current: ParentCurrentResponse


class LineageResponse(BaseModel):
    dataset_id: str
    name: str
    status: str = Field(description="known или legacy_unknown.")
    unknown_reason: str | None
    origin: str | None = Field(description="upload, join или null, если неизвестно.")
    artifact: ArtifactResponse = Field(
        description="Собственные байты датасета: загруженный файл или сохранённый результат сборки."
    )
    join: JoinResponse | None
    parents: list[ParentResponse]
    events: list[DatasetEventResponse] = Field(description="Шаги собственного конвейера.")
    columns: list[ColumnLineageResponse]
    warnings: list[str] = Field(
        description=(
            "Коды неполноты ответа: provenance_unknown, join_column_map_missing, "
            "join_parent_snapshots_missing, parent_history_truncated."
        )
    )
