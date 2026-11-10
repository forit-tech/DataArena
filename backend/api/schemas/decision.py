"""Схемы раздела Decision Lab."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CriterionIn(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    direction: Literal["max", "min"]
    weight: float = Field(ge=0, description="Вес в любом масштабе; 0 — критерий только для ограничений.")
    unit: str = Field(default="", max_length=20)
    group: str = Field(default="", max_length=20)


class MeasurementIn(BaseModel):
    value: float | None = Field(default=None, description="None — не измерено (это не ноль).")
    std: float | None = Field(default=None, ge=0, description="Стандартная неопределённость значения.")
    note: str = Field(default="", max_length=300)


class AlternativeIn(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    values: dict[str, MeasurementIn]
    meta: dict[str, str] = Field(default_factory=dict)


class ConstraintIn(BaseModel):
    criterion: str = Field(min_length=1, max_length=80)
    op: Literal["<=", ">="]
    bound: float


class ScaleIn(BaseModel):
    criterion: str = Field(min_length=1, max_length=80)
    kind: Literal["linear", "log"]
    worst: float = Field(description="Значение, которому соответствует полезность 0.")
    best: float = Field(description="Значение, которому соответствует полезность 1.")


class SensitivityIn(BaseModel):
    samples: int = Field(default=2000, ge=0, le=20_000)
    seed: int = Field(default=20261109, ge=0, le=2**31 - 1)
    concentration: float = Field(default=50.0, gt=0, le=10_000)
    grid_step: float = Field(default=0.01, ge=0.001, le=0.25)
    stability_threshold: float = Field(default=0.6, gt=0, le=1)


class EvaluateRequest(BaseModel):
    criteria: list[CriterionIn] = Field(min_length=1, max_length=30)
    alternatives: list[AlternativeIn] = Field(min_length=2, max_length=200)
    constraints: list[ConstraintIn] = Field(default_factory=list, max_length=30)
    normalization: Literal["vector", "minmax"] = "vector"
    primary: str | None = None
    sensitivity: SensitivityIn = Field(default_factory=SensitivityIn)
    scales: list[ScaleIn] = Field(default_factory=list, max_length=30, description="Шкалы режима D.")
