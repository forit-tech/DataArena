"""Воспроизведение конвейера поверх исходного датасета.

Здесь нет ни одного изменения данных на месте. Результат — это `LazyFrame`, полученный
применением шагов к плану чтения исходника: тот же список шагов, применённый заново,
даёт тот же результат, а исходный файл не открывается на запись ни разу.

Проверка зависимостей делается не отдельной моделью «кто что читает», а самим
воспроизведением: шаги применяются к **пустому кадру той же схемы**, и шаг, которому
не хватает колонки, отказывается ровно там, где отказался бы на настоящих данных.
Это важнее, чем кажется: вторая модель зависимостей однажды разошлась бы с первой,
и перестановка шагов молча ломала бы конвейер.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import polars as pl

from backend.domain.pipeline.models import (
    DESTRUCTIVE_STEPS,
    PipelineError,
    Step,
)
from backend.services.pipeline.operations import operation_for

#пределы конвейера. Двести шагов — на порядок больше, чем составляет человек; параметры
#шага — это имена колонок и значения из формы, а не данные
MAX_STEPS = 200
MAX_PARAMS_CHARS = 64 * 1024


@dataclass(frozen=True, slots=True)
class StepPlan:
    """Шаг вместе с тем, что о нём известно до выполнения."""

    step: Step
    title: str
    description: str
    #колонки на входе и на выходе шага: по ним видно, что он добавил или убрал
    columns_before: tuple[str, ...]
    columns_after: tuple[str, ...]
    destructive: bool

    def to_dict(self) -> dict:
        return {
            "step_id": self.step.step_id,
            "kind": self.step.kind.value,
            "params": self.step.params,
            "title": self.title,
            "description": self.description,
            "columns_before": list(self.columns_before),
            "columns_after": list(self.columns_after),
            "added_columns": [
                name for name in self.columns_after if name not in self.columns_before
            ],
            "removed_columns": [
                name for name in self.columns_before if name not in self.columns_after
            ],
            "destructive": self.destructive,
        }


def plan(schema: pl.Schema, steps: tuple[Step, ...]) -> list[StepPlan]:
    """Проверяет шаги по порядку и говорит, что каждый из них сделает со схемой.

    Возбуждает `PipelineError` с идентификатором шага, на котором конвейер ломается:
    именно это сообщение видит пользователь, переставивший шаг выше того, который
    создаёт нужную ему колонку.
    """
    require_within_limits(steps)
    current = schema
    plans: list[StepPlan] = []

    for position, step in enumerate(steps):
        operation = operation_for(step)

        try:
            operation.validate(step.params, current)
            after = _schema_after(operation, step, current)
        except PipelineError as error:
            raise PipelineError(
                f"Шаг {position + 1} «{operation.title}» не может быть выполнен: {error.message}",
                details={
                    "step_id": step.step_id,
                    "position": position,
                    **(error.details or {}),
                },
            ) from error
        except Exception as error:
            #любой отказ плана на этом шаге — это ошибка шага, а не сервера. Пользователю
            #нужно знать, какой шаг и почему, а не «внутренняя ошибка»
            raise PipelineError(
                f"Шаг {position + 1} «{operation.title}» не может быть выполнен.",
                details={"step_id": step.step_id, "position": position, "reason": str(error)[:200]},
            ) from error

        plans.append(
            StepPlan(
                step=step,
                title=operation.title,
                description=operation.describe(step.params),
                columns_before=tuple(current.names()),
                columns_after=tuple(after.names()),
                destructive=step.kind in DESTRUCTIVE_STEPS,
            )
        )
        current = after

    return plans


def _schema_after(operation: Any, step: Step, schema: pl.Schema) -> pl.Schema:
    """Схема после шага — на пустом кадре с текущей схемой, а не на всей цепочке.

    Прежде схема выводилась из цепочки всех предыдущих шагов, и каждый следующий шаг
    стоил дороже предыдущего: сто шагов проверялись за 0,06 с, двести — за 0,5 с,
    а две тысячи держали поток сервера минутами. Шагу нужна только схема на входе.
    """
    return operation.apply(pl.LazyFrame(schema=schema), step.params, schema).collect_schema()


def require_within_limits(steps: tuple[Step, ...]) -> None:
    """Конвейер обозрим: ограничен по числу шагов и размеру параметров, шаги различимы."""
    if len(steps) > MAX_STEPS:
        raise PipelineError(
            f"В конвейере не может быть больше {MAX_STEPS} шагов.",
            details={"steps": len(steps), "limit": MAX_STEPS},
        )

    identifiers = [step.step_id for step in steps]

    if len(set(identifiers)) != len(identifiers):
        #шаг без собственного идентификатора нельзя ни удалить, ни переставить отдельно
        repeated = sorted({item for item in identifiers if identifiers.count(item) > 1})
        raise PipelineError(
            f"Идентификаторы шагов повторяются: {', '.join(repeated)}.",
            details={"step_ids": repeated},
        )

    for step in steps:
        size = len(json.dumps(step.params, ensure_ascii=False))

        if size > MAX_PARAMS_CHARS:
            raise PipelineError(
                f"Параметры шага «{step.step_id}» слишком велики.",
                details={"step_id": step.step_id, "size": size, "limit": MAX_PARAMS_CHARS},
            )


def replay(lazy: pl.LazyFrame, schema: pl.Schema, steps: tuple[Step, ...]) -> pl.LazyFrame:
    """Применяет шаги к плану чтения исходника. Данные при этом не читаются."""
    current = schema

    for position, step in enumerate(steps):
        operation = operation_for(step)

        try:
            operation.validate(step.params, current)
            lazy = operation.apply(lazy, step.params, current)
            current = _schema_after(operation, step, current)
        except PipelineError as error:
            raise PipelineError(
                f"Шаг {position + 1} «{operation.title}» не может быть выполнен: {error.message}",
                details={"step_id": step.step_id, "position": position},
            ) from error
        except Exception as error:
            #как и в `plan`: отказ на шаге — ошибка шага, а не сервера. Прежде здесь ловилась
            #только ошибка конвейера, и параметры неверного типа давали пятисотку
            raise PipelineError(
                f"Шаг {position + 1} «{operation.title}» не может быть выполнен.",
                details={"step_id": step.step_id, "position": position, "reason": str(error)[:200]},
            ) from error

    return lazy


@dataclass(frozen=True, slots=True)
class StepImpact:
    """Что именно изменит шаг, если его применить к текущему состоянию."""

    rows_before: int
    rows_after: int
    columns_before: int
    columns_after: int
    #число значений, которые шаг превратит в пропуски: смена типа и разбор делают это
    #молча, и пользователь обязан увидеть цену до применения, а не после
    values_lost: int

    def to_dict(self) -> dict:
        return {
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_removed": max(self.rows_before - self.rows_after, 0),
            "columns_before": self.columns_before,
            "columns_after": self.columns_after,
            "values_lost": self.values_lost,
        }


def impact(lazy: pl.LazyFrame, schema: pl.Schema, steps: tuple[Step, ...], step: Step) -> StepImpact:
    """Считает последствия одного шага поверх результата предыдущих.

    Считается по данным, а не по догадке: «удалит 1 240 строк» — это выполненный подсчёт.

    Шаги сначала проверяются тем же `plan`, что и при сохранении: оценка не должна
    принимать шаг, который сохранение отвергнет, — и не должна падать на нём пятисоткой.
    """
    plan(schema, (*steps, step))
    before = replay(lazy, schema, steps)
    schema_before = before.collect_schema()
    after = replay(before, schema_before, (step,))
    schema_after = after.collect_schema()

    rows_before = int(before.select(pl.len()).collect().item())
    rows_after = int(after.select(pl.len()).collect().item())
    lost = _values_lost(before, after, schema_before, schema_after)

    return StepImpact(
        rows_before=rows_before,
        rows_after=rows_after,
        columns_before=len(schema_before),
        columns_after=len(schema_after),
        values_lost=lost,
    )


def _values_lost(
    before: pl.LazyFrame,
    after: pl.LazyFrame,
    schema_before: pl.Schema,
    schema_after: pl.Schema,
) -> int:
    """Сколько значений станет пропусками там, где их не было.

    Считается только для колонок, доживших до конца шага и не изменивших число строк:
    иначе «потеря значения» и «удаление строки» смешались бы в одно число.
    """
    shared = [name for name in schema_after.names() if name in schema_before]

    if not shared:
        return 0

    nulls_before = before.select(
        [pl.col(name).null_count().alias(name) for name in shared]
    ).collect()
    nulls_after = after.select(
        [pl.col(name).null_count().alias(name) for name in shared]
    ).collect()

    return sum(
        max(int(nulls_after[name][0]) - int(nulls_before[name][0]), 0) for name in shared
    )
