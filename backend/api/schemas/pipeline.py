"""Схемы запросов и ответов конвейера преобразований."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StepPayload(BaseModel):
    step_id: str = Field(description="Идентификатор шага, устойчивый между сохранениями.")
    kind: str = Field(description="Вид операции из закрытого перечня.")
    params: dict[str, Any] = Field(default_factory=dict, description="Параметры операции.")


class StepsRequest(BaseModel):
    steps: list[StepPayload] = Field(
        default_factory=list,
        description=(
            "Полный список шагов. Конвейер заменяется целиком, а прежнее состояние "
            "уходит в историю отмены: так отмена работает после перезагрузки страницы."
        ),
    )


class ImpactRequest(BaseModel):
    step: StepPayload = Field(description="Шаг, последствия которого нужно посчитать.")
    before: list[StepPayload] | None = Field(
        default=None,
        description=(
            "Шаги, поверх которых считать. По умолчанию — текущий сохранённый конвейер."
        ),
    )


class StepView(BaseModel):
    step_id: str
    kind: str
    params: dict[str, Any]
    title: str
    description: str
    columns_before: list[str]
    columns_after: list[str]
    added_columns: list[str]
    removed_columns: list[str]
    destructive: bool = Field(
        description="Шаг убирает данные: интерфейс обязан показать последствия до применения."
    )


class PipelineResponse(BaseModel):
    dataset_id: str
    steps: list[StepView]
    can_undo: bool
    can_redo: bool
    source_columns: list[str]
    result_columns: list[str]
    result_types: dict[str, str]


class ImpactResponse(BaseModel):
    rows_before: int
    rows_after: int
    rows_removed: int
    columns_before: int
    columns_after: int
    values_lost: int = Field(
        description=(
            "Сколько значений станет пропусками: смена типа и разбор делают это молча, "
            "и цену видно до применения, а не после."
        )
    )


class VocabularyResponse(BaseModel):
    operations: list[dict[str, str]]
    cast_targets: list[str]
    case_modes: list[str]
