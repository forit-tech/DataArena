"""Подсказка пар ключей: измеримые признаки вместо магии.

Каждая подсказка обязана отвечать на вопрос «почему именно эти колонки» словами,
которые можно проверить: столько-то значений общих, ключ уникален с такой-то стороны,
типы такие-то. Формулировки вроде «модель считает это хорошим соединением» здесь
недопустимы — по ним нельзя принять решение и нельзя поспорить.

Уверенность — не вероятность, а сумма понятных признаков, каждый из которых показан
пользователю отдельно. Слабая подсказка так и называется слабой.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

import polars as pl

#сколько пар колонок вообще рассматривать. Перебор ста колонок на сто — это десять тысяч
#пересечений, а осмысленных среди них единицы
MAX_PAIRS_EXAMINED = 400
#сколько подсказок возвращать: больше десяти человек не читает, а выбирает из первых
MAX_SUGGESTIONS = 10
#доля общих значений, ниже которой пара не предлагается вовсе
MIN_OVERLAP_SHARE = 0.05


@dataclass(frozen=True, slots=True)
class Suggestion:
    left: str
    right: str
    left_type: str
    right_type: str
    overlap_share: float
    left_unique_ratio: float
    right_unique_ratio: float
    expected_relation: str
    confidence: float
    reasons: tuple[str, ...]

    @property
    def strength(self) -> str:
        #слово вместо числа: «0.62» ни о чём не говорит, а «средняя» говорит
        if self.confidence >= 0.75:  # noqa: PLR2004 - граница словесной оценки
            return "высокая"

        if self.confidence >= 0.45:  # noqa: PLR2004 - граница словесной оценки
            return "средняя"

        return "слабая"

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "left_type": self.left_type,
            "right_type": self.right_type,
            "overlap_share": self.overlap_share,
            "left_unique_ratio": self.left_unique_ratio,
            "right_unique_ratio": self.right_unique_ratio,
            "expected_relation": self.expected_relation,
            "confidence": self.confidence,
            "strength": self.strength,
            "reasons": list(self.reasons),
        }


def suggest(left: pl.LazyFrame, right: pl.LazyFrame) -> list[Suggestion]:
    left_schema = left.collect_schema()
    right_schema = right.collect_schema()

    pairs = [
        (name, other)
        for name in left_schema.names()
        for other in right_schema.names()
        if _comparable(left_schema[name], right_schema[other])
    ][:MAX_PAIRS_EXAMINED]

    if not pairs:
        return []

    left_values = _value_sets(left, {name for name, _ in pairs})
    right_values = _value_sets(right, {other for _, other in pairs})
    found: list[Suggestion] = []

    for name, other in pairs:
        mine = left_values[name]
        theirs = right_values[other]

        if not mine or not theirs:
            continue

        common = len(mine & theirs)
        share = common / min(len(mine), len(theirs))

        if share < MIN_OVERLAP_SHARE:
            continue

        found.append(
            _build(
                name,
                other,
                str(left_schema[name]),
                str(right_schema[other]),
                share=share,
                left_unique=_unique_ratio(left, name),
                right_unique=_unique_ratio(right, other),
            )
        )

    found.sort(key=lambda item: item.confidence, reverse=True)

    return found[:MAX_SUGGESTIONS]


def _build(  # noqa: PLR0913 - признаки подсказки считаются вместе
    left: str,
    right: str,
    left_type: str,
    right_type: str,
    *,
    share: float,
    left_unique: float,
    right_unique: float,
) -> Suggestion:
    reasons: list[str] = [f"Общих значений: {share * 100:.0f}% от меньшей стороны."]
    confidence = share * 0.6

    if _same_name(left, right):
        confidence += 0.2
        reasons.append("Имена колонок совпадают или различаются только написанием.")

    unique_left = left_unique >= 0.999  # noqa: PLR2004 - «уникален» с точностью до повтора
    unique_right = right_unique >= 0.999  # noqa: PLR2004 - то же для правой стороны

    if unique_left or unique_right:
        confidence += 0.2
        side = "слева" if unique_left else "справа"
        reasons.append(f"Значение ключа уникально {side} — это признак настоящего ключа.")
    else:
        reasons.append(
            "Ключ не уникален ни с одной стороны: соединение умножит строки."
        )

    if left_type != right_type:
        reasons.append(
            f"Типы различаются: {left_type} и {right_type}. Соединение возможно, "
            "но стоит убедиться, что значения означают одно и то же."
        )

    relation = (
        "1:1"
        if unique_left and unique_right
        else "1:N"
        if unique_left
        else "N:1"
        if unique_right
        else "N:M"
    )

    return Suggestion(
        left=left,
        right=right,
        left_type=left_type,
        right_type=right_type,
        overlap_share=share,
        left_unique_ratio=left_unique,
        right_unique_ratio=right_unique,
        expected_relation=relation,
        confidence=min(confidence, 1.0),
        reasons=tuple(reasons),
    )


def _comparable(left: pl.DataType, right: pl.DataType) -> bool:
    if left.is_numeric() and right.is_numeric():
        return True

    textual = (pl.Utf8, pl.Categorical, pl.Enum)

    return isinstance(left, textual) and isinstance(right, textual)


def _value_sets(lazy: pl.LazyFrame, columns: set[str]) -> dict[str, set[str]]:
    """Различные значения колонок — приведённые к тексту, чтобы сравнивать число с текстом.

    Берётся ограниченное число значений на колонку: пересечение множеств нужно для оценки,
    а не для подсчёта, и полный список значений высококардинальной колонки стоил бы памяти
    без пользы.
    """
    limit = 20_000
    values: dict[str, set[str]] = {}

    for name in columns:
        collected = (
            lazy.select(pl.col(name).cast(pl.Utf8, strict=False).drop_nulls().unique().head(limit))
            .collect()
            .to_series()
            .to_list()
        )
        values[name] = {str(item) for item in collected}

    return values


def _unique_ratio(lazy: pl.LazyFrame, column: str) -> float:
    row = lazy.select(
        pl.col(column).drop_nulls().n_unique().alias("unique"),
        pl.len().alias("rows"),
    ).collect().row(0)

    return float(row[0]) / float(row[1]) if row[1] else 0.0


def _same_name(left: str, right: str) -> bool:
    return _normalised(left) == _normalised(right)


def _normalised(name: str) -> str:
    #сравниваются имена, а не байты: «Client ID», «client_id» и «CLIENTID» — одно и то же
    folded = unicodedata.normalize("NFKC", name).casefold()

    return "".join(character for character in folded if character.isalnum())
