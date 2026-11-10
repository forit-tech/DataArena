"""Схемы запросов и ответов сборки датасета."""

from __future__ import annotations

from pydantic import BaseModel, Field


class KeyPairPayload(BaseModel):
    left: str = Field(description="Колонка левого датасета.")
    right: str = Field(description="Колонка правого датасета.")


class JoinRequest(BaseModel):
    left_dataset_id: str
    right_dataset_id: str
    keys: list[KeyPairPayload] = Field(
        description="Пары колонок ключа. Составной ключ — несколько пар."
    )
    kind: str = Field(default="inner", description="inner, left, right или full.")
    left_columns: list[str] = Field(
        default_factory=list, description="Какие колонки взять слева. Пусто — все."
    )
    right_columns: list[str] = Field(
        default_factory=list, description="Какие колонки взять справа. Пусто — все."
    )
    right_suffix: str = Field(
        default="_2",
        description=(
            "Чем помечать одинаковые имена справа. Задаёт пользователь: «_file_2» ничего "
            "не говорит через месяц."
        ),
    )


class MaterializeRequest(JoinRequest):
    name: str = Field(description="Имя нового датасета.")


class KeyAnalysisResponse(BaseModel):
    pair: KeyPairPayload
    left_type: str
    right_type: str
    types_compatible: bool
    left_nulls: int
    right_nulls: int
    left_unique_ratio: float
    right_unique_ratio: float
    overlap_values: int
    left_values: int
    right_values: int


class JoinAnalysisResponse(BaseModel):
    keys: list[KeyAnalysisResponse]
    cardinality: str = Field(description="1:1, 1:N, N:1 или N:M — выведено из данных.")
    left_rows: int
    right_rows: int
    matched_left_rows: int
    matched_right_rows: int
    unmatched_left_rows: int
    unmatched_right_rows: int
    estimated_rows: int = Field(
        description="Точное число строк результата, посчитанное до выполнения."
    )
    row_explosion: bool = Field(
        description="Результат заметно больше обеих сторон: связь умножает строки."
    )
    warnings: list[str]


class SuggestionResponse(BaseModel):
    left: str
    right: str
    left_type: str
    right_type: str
    overlap_share: float
    left_unique_ratio: float
    right_unique_ratio: float
    expected_relation: str
    confidence: float
    strength: str
    reasons: list[str] = Field(description="Почему предложена именно эта пара.")


class SuggestionsResponse(BaseModel):
    suggestions: list[SuggestionResponse]
