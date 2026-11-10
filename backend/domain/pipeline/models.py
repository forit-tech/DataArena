"""Шаг преобразования и конвейер — предметные модели.

Главное свойство здесь одно: **исходный датасет не меняется никогда**. Конвейер — это
не изменённые данные, а список шагов; результат получается повторным применением шагов
к исходнику. Отсюда всё остальное: отмена — это перемещение по списку, а не откат правок;
воспроизводимость — это тот же список, применённый заново; а «сломанный шаг» — это шаг,
чьи колонки исчезли, а не испорченный файл.

Слой не знает ни про Polars, ни про HTTP, ни про SQLite: здесь только описание.
Выполнение живёт в `services/pipeline`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from backend.core.errors import AppError


class PipelineError(AppError):
    #шаг нельзя построить, проверить или применить: это ошибка запроса, а не сервера
    status_code = 422
    code = "pipeline_step_invalid"


class StepKind(StrEnum):
    """Виды шагов.

    Перечень закрыт намеренно. Шаг — это не произвольный код пользователя, а операция
    из списка, у которой есть проверка, описание и оценка последствий. Иначе конвейер
    перестаёт быть воспроизводимым описанием и становится исполнением чужого кода.
    """

    #колонки
    DROP_COLUMNS = "drop_columns"
    RENAME_COLUMN = "rename_column"
    REORDER_COLUMNS = "reorder_columns"
    DUPLICATE_COLUMN = "duplicate_column"
    #новая колонка по формуле: формула — ограниченный язык выражений, а не код
    ADD_COLUMN = "add_column"
    CAST_COLUMN = "cast_column"
    #строки
    FILTER_ROWS = "filter_rows"
    DROP_ROWS = "drop_rows"
    SORT_ROWS = "sort_rows"
    #значения
    REPLACE_VALUES = "replace_values"
    FILL_MISSING = "fill_missing"
    NORMALIZE_NULLS = "normalize_nulls"
    #очистка
    DEDUPLICATE = "deduplicate"
    TRIM_WHITESPACE = "trim_whitespace"
    CHANGE_CASE = "change_case"
    PARSE_NUMBER = "parse_number"
    PARSE_DATETIME = "parse_datetime"


#шаги, которые убирают данные: перед ними интерфейс обязан показать, что именно исчезнет.
#Список нужен здесь, а не в интерфейсе: «разрушающая ли операция» — свойство операции,
#а не экрана
DESTRUCTIVE_STEPS = frozenset(
    {
        StepKind.DROP_COLUMNS,
        StepKind.DROP_ROWS,
        StepKind.FILTER_ROWS,
        StepKind.DEDUPLICATE,
    }
)

#предел длины списка отмены. Без предела история конвейера растёт без границы и однажды
#перестаёт помещаться в ответ; пятьдесят шагов назад — заведомо больше, чем нужно человеку
MAX_HISTORY_DEPTH = 50


@dataclass(frozen=True, slots=True)
class Step:
    """Один шаг: вид операции и её параметры."""

    step_id: str
    kind: StepKind
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "kind": self.kind.value, "params": self.params}

    @classmethod
    def from_dict(cls, payload: Any) -> Step:
        if not isinstance(payload, dict):
            #шаг — не объект: запись повреждена. Без этой проверки отказ приходил бы
            #как AttributeError, то есть пятисоткой, а не понятной ошибкой конвейера
            raise PipelineError("Шаг конвейера повреждён и не читается.")

        kind = str(payload.get("kind") or "")

        try:
            parsed = StepKind(kind)
        except ValueError as error:
            #шаг сохранён другой версией и больше не поддерживается. Молча пропустить его
            #нельзя: конвейер перестанет быть тем, что сохранял пользователь
            raise PipelineError(
                f"Неизвестный вид шага: {kind}.", details={"kind": kind}
            ) from error

        identifier = payload.get("step_id")

        if not identifier:
            #шаг без идентификатора нельзя ни удалить, ни переставить: он существовал бы
            #в списке и не поддавался бы управлению
            raise PipelineError("У шага нет идентификатора.", details={"kind": kind})

        params = payload.get("params") or {}

        if not isinstance(params, dict):
            raise PipelineError(
                "Параметры шага должны быть набором значений.",
                details={"step_id": str(identifier)},
            )

        return cls(step_id=str(identifier), kind=parsed, params=dict(params))


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Конвейер датасета вместе с историей отмены.

    История хранится на сервере, а не в браузере: конвейер переживает перезагрузку
    страницы, и отмена после неё обязана работать так же, как до.
    """

    dataset_id: str
    steps: tuple[Step, ...] = ()
    undo: tuple[tuple[Step, ...], ...] = ()
    redo: tuple[tuple[Step, ...], ...] = ()

    def with_steps(self, steps: tuple[Step, ...]) -> Pipeline:
        """Новое состояние конвейера: прежнее уходит в отмену, повтор обнуляется."""
        history = (*self.undo, self.steps)[-MAX_HISTORY_DEPTH:]

        return Pipeline(dataset_id=self.dataset_id, steps=steps, undo=history, redo=())

    def undone(self) -> Pipeline:
        if not self.undo:
            raise PipelineError("Отменять нечего.", details={"dataset_id": self.dataset_id})

        return Pipeline(
            dataset_id=self.dataset_id,
            steps=self.undo[-1],
            undo=self.undo[:-1],
            redo=(*self.redo, self.steps)[-MAX_HISTORY_DEPTH:],
        )

    def redone(self) -> Pipeline:
        if not self.redo:
            raise PipelineError("Повторять нечего.", details={"dataset_id": self.dataset_id})

        return Pipeline(
            dataset_id=self.dataset_id,
            steps=self.redo[-1],
            undo=(*self.undo, self.steps)[-MAX_HISTORY_DEPTH:],
            redo=self.redo[:-1],
        )

    def to_storage(self) -> dict[str, Any]:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "undo": [[step.to_dict() for step in state] for state in self.undo],
            "redo": [[step.to_dict() for step in state] for state in self.redo],
        }

    @classmethod
    def from_storage(cls, dataset_id: str, payload: dict[str, Any]) -> Pipeline:
        def listed(value: Any) -> list[Any]:
            if value is None:
                return []

            if not isinstance(value, list):
                raise PipelineError(
                    "Конвейер датасета повреждён и не читается. Шаги придётся задать заново.",
                    details={"dataset_id": dataset_id},
                )

            return value

        def states(key: str) -> tuple[tuple[Step, ...], ...]:
            return tuple(
                tuple(Step.from_dict(item) for item in listed(state))
                for state in listed(payload.get(key))
            )

        return cls(
            dataset_id=dataset_id,
            steps=tuple(Step.from_dict(item) for item in listed(payload.get("steps"))),
            undo=states("undo"),
            redo=states("redo"),
        )
