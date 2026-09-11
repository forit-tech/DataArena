"""Схемы API раздела workspace.

Отделены от доменных моделей намеренно: внутреннее представление можно менять,
не ломая контракт с frontend.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Имя рабочего пространства.")


class WorkspaceResponse(BaseModel):
    workspace_id: str
    name: str
    created_at: str
    dataset_count: int


class ColumnSchemaResponse(BaseModel):
    name: str
    position: int
    physical_type: str = Field(description="Тип хранения, как его называет движок.")
    logical_type: str = Field(description="Логический тип из списка контракта.")
    semantic_type: str = Field(description="Смысл колонки: id, numeric, categorical, datetime, boolean, text.")
    nullable: bool


class DatasetSchemaResponse(BaseModel):
    columns: list[ColumnSchemaResponse]


class DatasetSourceResponse(BaseModel):
    file_name: str = Field(description="Имя, под которым файл загрузил пользователь.")
    format: str
    bytes: int
    sha256: str


class ConversionResponse(BaseModel):
    source_format: str
    derived_format: str
    converter: str
    converter_version: int
    created_at: str
    row_count: int
    warnings: list[str] = Field(description="Что могло потеряться при конвертации.")


class DatasetResponse(BaseModel):
    dataset_id: str
    workspace_id: str
    name: str
    source: DatasetSourceResponse
    schema_: DatasetSchemaResponse = Field(alias="schema")
    row_count: int | None = Field(
        description="None означает, что точное число строк ещё не считалось: для потоковых "
        "форматов это полный проход по файлу."
    )
    column_count: int
    status: str
    status_reason: str | None
    original_format: str = Field(description="Формат файла, который загрузил пользователь.")
    working_format: str = Field(description="Формат, из которого backend фактически читает.")
    normalized: bool
    conversion: ConversionResponse | None
    created_at: str

    model_config = {"populate_by_name": True}


class DatasetListResponse(BaseModel):
    datasets: list[DatasetResponse]


class PageResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_ordinals: list[int] = Field(
        description=(
            "Номер строки в файле, по одному на строку страницы. Не номер на странице: "
            "после сортировки «третья сверху» — каждый раз другая строка."
        )
    )
    offset: int
    limit: int
    total_rows: int | None = Field(
        description="Число строк после фильтров. Может быть срезано сверху — см. total_is_exact."
    )
    total_is_exact: bool = Field(
        description="False означает, что строк больше total_rows: точный подсчёт за этим "
        "пределом стоит полного прохода по данным."
    )


class ColumnStatisticsResponse(BaseModel):
    name: str
    dtype: str
    row_count: int
    null_count: int
    null_ratio: float
    unique_count: int
    unique_ratio: float
    numeric: dict[str, Any] | None
    top_values: list[dict[str, Any]] | None
    histogram: dict[str, Any] | None
    examples: list[Any]
