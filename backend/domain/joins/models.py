"""Соединение датасетов: описание, анализ ключей и прогноз размера.

Соединить два датасета — это не вызвать `join()`. Прежде чем выполнять, нужно ответить
на вопросы, из-за которых соединения обычно и портят данные:

* совпадают ли типы ключей;
* сколько строк вообще найдут пару;
* уникален ли ключ с каждой стороны — то есть это `1:1`, `1:N`, `N:1` или `N:M`;
* **сколько строк получится**.

Последнее важнее всего. `N:M` по неуникальному ключу даёт произведение, и датасет
на сто тысяч строк превращается в десять миллионов молча. Запрещать такое соединение
нельзя — оно бывает законным, — но пользователь обязан увидеть число до выполнения,
а не после.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from backend.core.errors import AppError


class JoinError(AppError):
    status_code = 422
    code = "join_invalid"


class JoinKind(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"


class Cardinality(StrEnum):
    """Тип связи, выведенный из данных, а не из намерения."""

    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"
    MANY_TO_MANY = "N:M"


#во сколько раз результат должен превысить большую сторону, чтобы это считалось взрывом.
#Не «больше, чем было»: левое соединение справочника законно оставляет столько же строк,
#а вот утроение — это уже неожиданность, о которой надо сказать вслух
ROW_EXPLOSION_FACTOR = 2.0


def types_compatible(left: str, right: str) -> bool:
    """Сравнимы ли типы ключа. Строковые имена типов: домен не знает про Polars.

    Проверка нужна и анализу, и выполнению. Пока она была только в анализе, соединение
    числа с текстом падало необработанной ошибкой движка: предупреждение показывалось,
    а нажатие «Создать датасет» давало пятисотку.
    """
    if left == right:
        return True

    numeric = ("Int", "UInt", "Float", "Decimal")
    textual = ("String", "Utf8", "Categorical", "Enum")

    def kind(name: str) -> str | None:
        if name.startswith(numeric):
            return "число"

        if name.startswith(textual):
            return "текст"

        return None

    left_kind, right_kind = kind(left), kind(right)

    return left_kind is not None and left_kind == right_kind


@dataclass(frozen=True, slots=True)
class KeyPair:
    left: str
    right: str

    def to_dict(self) -> dict[str, str]:
        return {"left": self.left, "right": self.right}


@dataclass(frozen=True, slots=True)
class KeyAnalysis:
    """Измеренные свойства одной пары ключей."""

    pair: KeyPair
    left_type: str
    right_type: str
    types_compatible: bool
    left_nulls: int
    right_nulls: int
    left_unique_ratio: float
    right_unique_ratio: float
    #сколько различных значений ключа встречается с обеих сторон
    overlap_values: int
    left_values: int
    right_values: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair.to_dict(),
            "left_type": self.left_type,
            "right_type": self.right_type,
            "types_compatible": self.types_compatible,
            "left_nulls": self.left_nulls,
            "right_nulls": self.right_nulls,
            "left_unique_ratio": self.left_unique_ratio,
            "right_unique_ratio": self.right_unique_ratio,
            "overlap_values": self.overlap_values,
            "left_values": self.left_values,
            "right_values": self.right_values,
        }


@dataclass(frozen=True, slots=True)
class JoinAnalysis:
    """Что известно о соединении до его выполнения."""

    keys: tuple[KeyAnalysis, ...]
    cardinality: Cardinality
    left_rows: int
    right_rows: int
    matched_left_rows: int
    matched_right_rows: int
    #точное число строк результата, посчитанное по данным, а не прикинутое
    estimated_rows: int
    warnings: tuple[str, ...]

    @property
    def row_explosion(self) -> bool:
        largest = max(self.left_rows, self.right_rows, 1)

        return self.estimated_rows > largest * ROW_EXPLOSION_FACTOR

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys": [item.to_dict() for item in self.keys],
            "cardinality": self.cardinality.value,
            "left_rows": self.left_rows,
            "right_rows": self.right_rows,
            "matched_left_rows": self.matched_left_rows,
            "matched_right_rows": self.matched_right_rows,
            "unmatched_left_rows": self.left_rows - self.matched_left_rows,
            "unmatched_right_rows": self.right_rows - self.matched_right_rows,
            "estimated_rows": self.estimated_rows,
            "row_explosion": self.row_explosion,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class JoinSpec:
    """Описание соединения, достаточное для повторения."""

    left_dataset_id: str
    right_dataset_id: str
    keys: tuple[KeyPair, ...]
    kind: JoinKind = JoinKind.INNER
    #какие колонки взять с каждой стороны; пусто означает «все»
    left_columns: tuple[str, ...] = ()
    right_columns: tuple[str, ...] = ()
    #как разводить одинаковые имена. Суффикс задаёт пользователь: «_file_2» ничего
    #не говорит через месяц, а «_из_заказов» говорит
    right_suffix: str = "_2"

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_dataset_id": self.left_dataset_id,
            "right_dataset_id": self.right_dataset_id,
            "keys": [key.to_dict() for key in self.keys],
            "kind": self.kind.value,
            "left_columns": list(self.left_columns),
            "right_columns": list(self.right_columns),
            "right_suffix": self.right_suffix,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JoinSpec:
        keys = tuple(
            KeyPair(left=str(item["left"]), right=str(item["right"]))
            for item in payload.get("keys") or []
        )

        if not keys:
            raise JoinError("Не задан ни один ключ соединения.", details={"keys": []})

        try:
            kind = JoinKind(str(payload.get("kind") or "inner"))
        except ValueError as error:
            raise JoinError(
                f"Неизвестный вид соединения: {payload.get('kind')}.",
                details={"allowed": [item.value for item in JoinKind]},
            ) from error

        suffix = str(payload.get("right_suffix") or "_2")

        if not suffix.strip():
            #пустой суффикс означал бы, что одинаковые имена молча схлопнутся в одно
            raise JoinError("Суффикс не может быть пустым.", details={"right_suffix": suffix})

        return cls(
            left_dataset_id=str(payload["left_dataset_id"]),
            right_dataset_id=str(payload["right_dataset_id"]),
            keys=keys,
            kind=kind,
            left_columns=tuple(payload.get("left_columns") or ()),
            right_columns=tuple(payload.get("right_columns") or ()),
            right_suffix=suffix,
        )
