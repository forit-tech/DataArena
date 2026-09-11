"""Схемы API диагностики.

Описание предиката наружу не отдаётся. Интерфейсу незачем знать его устройство, а отдать
значило бы предложить собирать такой предикат самостоятельно — и рано или поздно
собранный в браузере отбор разошёлся бы с тем, что посчитала находка. Вместо предиката
отдаётся признак `has_affected_rows`: есть ли вообще осмысленное подмножество строк.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SuggestedActionResponse(BaseModel):
    kind: str = Field(description="Вид будущего шага преобразования.")
    columns: list[str]
    hint: str = Field(description="Что именно предлагается сделать, словами.")


class EvidenceResponse(BaseModel):
    values: list[Any] = Field(description="Примеры значений; число и длина ограничены.")
    counts: dict[str, Any] = Field(description="Счётчики, подтверждающие находку.")


class FindingResponse(BaseModel):
    finding_id: str = Field(description="Устойчив: выводится из датасета, артефакта и кода.")
    code: str
    severity: str = Field(description="problem, warning или notice.")
    scope: str = Field(description="dataset, column или column_set.")
    columns: list[str]
    title: str
    explanation: str
    exactness: str = Field(
        description=(
            "exact или sampled. Поле обязательное: выборочное число, показанное "
            "как точное, приводит к неверному решению по данным."
        )
    )
    affected_rows: int | None = Field(
        description="None, когда понятие «затронутые строки» к находке неприменимо."
    )
    affected_ratio: float | None
    sampled_rows: int | None = Field(description="Сколько строк просмотрено, если выборочно.")
    evidence: EvidenceResponse
    suggested_action: SuggestedActionResponse | None
    has_affected_rows: bool = Field(
        description=(
            "Есть ли осмысленное подмножество строк. Ложь у находок, затрагивающих весь "
            "датасет: «показать все строки» — это просто открыть датасет, и кнопки "
            "для этого быть не должно."
        )
    )


class HealthSummaryResponse(BaseModel):
    problem: int
    warning: int
    notice: int


class HealthReportResponse(BaseModel):
    dataset_id: str
    artifact_fingerprint: str = Field(
        description="Отпечаток артефакта, по которому посчитан отчёт."
    )
    checks_version: int
    computed_at: str
    total_rows: int
    summary: HealthSummaryResponse = Field(
        description="Сколько находок каждого уровня. Это не оценка качества: её нет."
    )
    findings: list[FindingResponse]
