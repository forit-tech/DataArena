"""Схемы выгрузки."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FormatResponse(BaseModel):
    key: str
    label: str
    extensions: list[str]
    can_read: bool
    can_write: bool
    supports_lazy_scan: bool
    preserves_schema: bool = Field(
        description="Переживают ли типы round-trip. Подтверждено тестом, а не намерением."
    )
    preserves_nulls: bool
    row_limit: int | None = Field(
        default=None, description="Жёсткий предел формата: у XLSX он есть и приводит к потере."
    )
    warnings: list[str] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class FormatsResponse(BaseModel):
    formats: list[dict[str, Any]]
