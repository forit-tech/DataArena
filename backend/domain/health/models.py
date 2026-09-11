"""Модель диагностики датасета.

Находки независимы и объяснимы. Сводной оценки качества нет и не будет: одно число
скрывает, какой именно дефект важен, и подталкивает улучшать число вместо данных.

Каждая находка отвечает на четыре вопроса: что не так, насколько это масштабно,
почему это важно и что с этим можно сделать. Пятый ответ — «покажи мне эти строки» —
даёт `row_filter`, и он не список номеров, а описание предиката: находка на миллион
строк иначе означала бы миллион чисел в ответе, в базе и в браузере.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

#версия набора проверок. Меняется вместе с логикой любой проверки: отчёт, посчитанный
#прежней версией, нельзя показывать как актуальный
CHECKS_VERSION = 1


class Severity(StrEnum):
    """Насколько уверенно это дефект.

    Три уровня, а не число: сводная оценка скрыла бы разницу между «колонка пуста»
    и «значения похожи на идентификатор».
    """

    #почти наверняка ошибка в данных
    PROBLEM = "problem"
    #вероятно ошибка, надо посмотреть
    WARNING = "warning"
    #сигнал, может быть совершенно нормальным
    NOTICE = "notice"


#порядок для устойчивой сортировки: сначала то, что скорее всего сломано
SEVERITY_ORDER = {Severity.PROBLEM: 0, Severity.WARNING: 1, Severity.NOTICE: 2}


class Exactness(StrEnum):
    """Точное число или выборочное.

    Поле обязательное, а не необязательное, и в этом весь смысл: выборочное число,
    показанное как точное, приводит к неверному решению по данным. Понизить точность
    молча проверка не имеет права.
    """

    EXACT = "exact"
    SAMPLED = "sampled"


class Scope(StrEnum):
    DATASET = "dataset"
    COLUMN = "column"
    COLUMN_SET = "column_set"


class CheckCode(StrEnum):
    """Закрытый перечень проверок.

    Проверка, которая в текущей реализации не может сработать ни разу, сюда не входит.
    Так, `schema_drift` появится вместе с многочастными источниками: сегодня датасет —
    один артефакт, и код, который никогда не срабатывает, — это кнопка без действия.
    """

    MISSING_VALUES = "missing_values"
    ALL_NULL_COLUMN = "all_null_column"
    DUPLICATE_ROWS = "duplicate_rows"
    DUPLICATE_KEYS = "duplicate_keys"
    CONSTANT_COLUMN = "constant_column"
    NEAR_CONSTANT_COLUMN = "near_constant_column"
    HIGH_CARDINALITY = "high_cardinality"
    POTENTIAL_IDENTIFIER = "potential_identifier"
    MIXED_SEMANTIC_TYPES = "mixed_semantic_types"
    NAN_OR_INFINITY = "nan_or_infinity"
    WHITESPACE_ANOMALY = "whitespace_anomaly"
    EMPTY_STRING_VS_NULL = "empty_string_vs_null"
    CASE_VARIANT_CATEGORIES = "case_variant_categories"
    OUTLIER_VALUES = "outlier_values"


#Проверки, которые НИКОГДА не поднимаются выше «сигнала», сколько бы строк ни задели.
#Это свойства данных, а не дефекты: у адреса высокая кардинальность по природе,
#а уникальный столбец — это, скорее всего, идентификатор, и «чинить» его не надо.
#Правило закреплено тестом, перебирающим все коды
NOTICE_ONLY_CHECKS = frozenset(
    {
        CheckCode.HIGH_CARDINALITY,
        CheckCode.POTENTIAL_IDENTIFIER,
        CheckCode.OUTLIER_VALUES,
        CheckCode.CONSTANT_COLUMN,
        CheckCode.NEAR_CONSTANT_COLUMN,
    }
)


class RowFilterKind(StrEnum):
    """Как описать затронутые строки, не перечисляя их.

    Перечень закрыт: каждый вид отображается в одно выражение Polars, и других
    способов получить строки находки не существует.
    """

    IS_NULL = "is_null"
    IS_EMPTY_STRING = "is_empty_string"
    IS_NAN_OR_INF = "is_nan_or_inf"
    NOT_EQUALS = "not_equals"
    IN_VALUES = "in_values"
    NORMALISED_IN_VALUES = "normalised_in_values"
    HAS_SURROUNDING_WHITESPACE = "has_surrounding_whitespace"
    OUT_OF_RANGE = "out_of_range"
    DUPLICATED_BY = "duplicated_by"
    NOT_PARSEABLE_AS_NUMBER = "not_parseable_as_number"


@dataclass(frozen=True, slots=True)
class RowFilter:
    """Описание затронутых строк.

    Один и тот же объект используется и для подсчёта в находке, и для показа строк
    в таблице. Не «эквивалентная логика» — тот же объект: иначе однажды находка скажет
    «127 строк», а таблица покажет 119, и доверия к разделу не останется.
    """

    kind: RowFilterKind
    columns: tuple[str, ...]
    values: tuple[Any, ...] = ()
    low: float | None = None
    high: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "columns": list(self.columns),
            "values": list(self.values),
            "low": self.low,
            "high": self.high,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RowFilter:
        return cls(
            kind=RowFilterKind(payload["kind"]),
            columns=tuple(payload["columns"]),
            values=tuple(payload.get("values") or ()),
            low=payload.get("low"),
            high=payload.get("high"),
        )


class ActionKind(StrEnum):
    DROP_COLUMN = "drop_column"
    FILL_MISSING = "fill_missing"
    DROP_DUPLICATE_ROWS = "drop_duplicate_rows"
    TRIM_WHITESPACE = "trim_whitespace"
    NORMALISE_CASE = "normalise_case"
    REVIEW_MANUALLY = "review_manually"


@dataclass(frozen=True, slots=True)
class SuggestedAction:
    """Описание будущего шага преобразования, а не действие.

    Health ничего не исправляет и ничего не пишет. Когда появится этап преобразований,
    кнопка «исправить» будет создавать из этого описания шаг — с исходным снимком,
    самим шагом и производным снимком в происхождении, — а не менять исходный артефакт.
    До тех пор это текст рекомендации: кнопки, которая ничего не делает, не будет.
    """

    kind: ActionKind
    columns: tuple[str, ...] = ()
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "columns": list(self.columns), "hint": self.hint}


@dataclass(frozen=True, slots=True)
class Evidence:
    """Ограниченное подтверждение находки: примеры и счётчики."""

    values: tuple[Any, ...] = ()
    counts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"values": list(self.values), "counts": dict(self.counts)}


@dataclass(frozen=True, slots=True)
class Finding:
    code: CheckCode
    severity: Severity
    scope: Scope
    columns: tuple[str, ...]
    title: str
    explanation: str
    exactness: Exactness = Exactness.EXACT
    affected_rows: int | None = None
    affected_ratio: float | None = None
    sampled_rows: int | None = None
    evidence: Evidence = field(default_factory=Evidence)
    suggested_action: SuggestedAction | None = None
    row_filter: RowFilter | None = None

    def __post_init__(self) -> None:
        if self.code in NOTICE_ONLY_CHECKS and self.severity is not Severity.NOTICE:
            #не «поправить на месте», а возбудить: попытка выдать сигнал за дефект —
            #это ошибка в проверке, и она должна быть видна сразу
            raise ValueError(
                f"Проверка {self.code.value} описывает свойство данных, а не дефект, "
                "и не может быть серьёзнее сигнала."
            )

        if self.exactness is Exactness.SAMPLED and self.sampled_rows is None:
            raise ValueError(
                f"Проверка {self.code.value} объявлена выборочной, но не сказала, "
                "сколько строк просмотрено."
            )

    def identity(self, dataset_id: str, artifact_fingerprint: str) -> str:
        """Устойчивый идентификатор находки.

        Выводится, а не хранится: повторное открытие Health даёт те же идентификаторы,
        и ссылка на находку переживает перезагрузку страницы. Отпечаток артефакта входит
        в него намеренно — после пересборки артефакта старая ссылка перестаёт
        разрешаться, и это видно как отдельный ответ, а не как показ других строк.
        """
        material = "|".join(
            [dataset_id, artifact_fingerprint, str(CHECKS_VERSION), self.code.value, *self.columns]
        )

        return "fnd_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def to_dict(self, dataset_id: str, artifact_fingerprint: str) -> dict[str, Any]:
        return {
            "finding_id": self.identity(dataset_id, artifact_fingerprint),
            "code": self.code.value,
            "severity": self.severity.value,
            "scope": self.scope.value,
            "columns": list(self.columns),
            "title": self.title,
            "explanation": self.explanation,
            "exactness": self.exactness.value,
            "affected_rows": self.affected_rows,
            "affected_ratio": self.affected_ratio,
            "sampled_rows": self.sampled_rows,
            "evidence": self.evidence.to_dict(),
            "suggested_action": (
                self.suggested_action.to_dict() if self.suggested_action else None
            ),
            #интерфейс показывает кнопку «Показать строки» только когда здесь не пусто:
            #находка, задевающая все строки, такой кнопки не получает — «показать все»
            #означает просто открыть датасет
            "has_affected_rows": self.row_filter is not None,
        }


def finding_to_storage(finding: Finding) -> dict[str, Any]:
    """Полный вид находки для хранения.

    Отличается от вида для API одним: здесь есть `row_filter`. Наружу он не отдаётся —
    интерфейсу незачем знать устройство предиката, и отдать его значило бы предложить
    собирать такой предикат самостоятельно. Но сохранить его надо: иначе после
    перезапуска провал в строки пришлось бы пересчитывать целиком.
    """
    return {
        "code": finding.code.value,
        "severity": finding.severity.value,
        "scope": finding.scope.value,
        "columns": list(finding.columns),
        "title": finding.title,
        "explanation": finding.explanation,
        "exactness": finding.exactness.value,
        "affected_rows": finding.affected_rows,
        "affected_ratio": finding.affected_ratio,
        "sampled_rows": finding.sampled_rows,
        "evidence": finding.evidence.to_dict(),
        "suggested_action": (
            finding.suggested_action.to_dict() if finding.suggested_action else None
        ),
        "row_filter": finding.row_filter.to_dict() if finding.row_filter else None,
    }


def finding_from_storage(payload: dict[str, Any]) -> Finding:
    action = payload.get("suggested_action")
    evidence = payload.get("evidence") or {}

    return Finding(
        code=CheckCode(payload["code"]),
        severity=Severity(payload["severity"]),
        scope=Scope(payload["scope"]),
        columns=tuple(payload["columns"]),
        title=payload["title"],
        explanation=payload["explanation"],
        exactness=Exactness(payload["exactness"]),
        affected_rows=payload.get("affected_rows"),
        affected_ratio=payload.get("affected_ratio"),
        sampled_rows=payload.get("sampled_rows"),
        evidence=Evidence(
            values=tuple(evidence.get("values") or ()), counts=dict(evidence.get("counts") or {})
        ),
        suggested_action=(
            SuggestedAction(
                kind=ActionKind(action["kind"]),
                columns=tuple(action.get("columns") or ()),
                hint=action.get("hint", ""),
            )
            if action
            else None
        ),
        row_filter=(
            RowFilter.from_dict(payload["row_filter"]) if payload.get("row_filter") else None
        ),
    )


@dataclass(frozen=True, slots=True)
class HealthReport:
    dataset_id: str
    artifact_fingerprint: str
    checks_version: int
    computed_at: datetime
    total_rows: int
    findings: tuple[Finding, ...]

    @property
    def summary(self) -> dict[str, int]:
        #сводка по уровням, а не оценка: она отвечает «сколько чего найдено»,
        #а не «насколько датасет хорош»
        counts = {level.value: 0 for level in Severity}

        for finding in self.findings:
            counts[finding.severity.value] += 1

        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "artifact_fingerprint": self.artifact_fingerprint,
            "checks_version": self.checks_version,
            "computed_at": self.computed_at.isoformat(),
            "total_rows": self.total_rows,
            "summary": self.summary,
            "findings": [
                finding.to_dict(self.dataset_id, self.artifact_fingerprint)
                for finding in self.findings
            ],
        }

    def find(self, finding_id: str) -> Finding | None:
        return next(
            (
                finding
                for finding in self.findings
                if finding.identity(self.dataset_id, self.artifact_fingerprint) == finding_id
            ),
            None,
        )


def order_findings(findings: list[Finding]) -> list[Finding]:
    """Устойчивый порядок находок.

    Серьёзность, затем масштаб, затем имя колонки, затем код. Последние два нужны
    не для красоты: без них две находки с одинаковой долей менялись бы местами
    между открытиями, и отчёт выглядел бы меняющимся, хотя данные те же.
    """
    return sorted(
        findings,
        key=lambda finding: (
            SEVERITY_ORDER[finding.severity],
            -(finding.affected_ratio or 0.0),
            -(finding.affected_rows or 0),
            finding.columns,
            finding.code.value,
        ),
    )
