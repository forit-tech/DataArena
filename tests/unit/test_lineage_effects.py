"""Эффект операции для происхождения — отдельный контракт, а не производное от `reads`.

Таблица ниже перечисляет каждый вид шага явно. Новая операция без записи здесь роняет
тест: решение «меняет ли она значения колонки» должно быть принято человеком, а не
унаследовано по умолчанию.
"""

from __future__ import annotations

import polars as pl
import pytest

from backend.domain.dataset.identifiers import new_dataset_id
from backend.domain.lineage.effects import (
    ColumnCreate,
    ColumnDrop,
    ColumnMutation,
    ColumnRename,
    DatasetOnlyEffect,
)
from backend.domain.lineage.models import ColumnOrigin, ColumnRef
from backend.domain.pipeline.models import DESTRUCTIVE_STEPS, Step, StepKind
from backend.services.lineage import _fold, _result_schema, _Tracked
from backend.services.pipeline.operations import OPERATIONS

EXPECTED_EFFECT = {
    StepKind.DROP_COLUMNS: ColumnDrop,
    StepKind.RENAME_COLUMN: ColumnRename,
    StepKind.REORDER_COLUMNS: DatasetOnlyEffect,
    StepKind.DUPLICATE_COLUMN: ColumnCreate,
    #колонка по формуле читает колонки из формулы и создаёт новую
    StepKind.ADD_COLUMN: ColumnCreate,
    StepKind.CAST_COLUMN: ColumnMutation,
    StepKind.FILTER_ROWS: DatasetOnlyEffect,
    StepKind.DROP_ROWS: DatasetOnlyEffect,
    StepKind.SORT_ROWS: DatasetOnlyEffect,
    StepKind.REPLACE_VALUES: ColumnMutation,
    StepKind.FILL_MISSING: ColumnMutation,
    StepKind.NORMALIZE_NULLS: ColumnMutation,
    StepKind.DEDUPLICATE: DatasetOnlyEffect,
    StepKind.TRIM_WHITESPACE: ColumnMutation,
    StepKind.CHANGE_CASE: ColumnMutation,
    StepKind.PARSE_NUMBER: ColumnMutation,
    StepKind.PARSE_DATETIME: ColumnMutation,
}

SAMPLE_PARAMS = {"column": "a", "to": "b", "columns": ["a"], "order": ["a"]}


def test_every_step_kind_has_a_deliberate_effect() -> None:
    assert set(EXPECTED_EFFECT) == set(StepKind), "у нового вида шага не выбран эффект"


@pytest.mark.parametrize("kind", list(StepKind))
def test_the_operation_declares_the_expected_effect(kind: StepKind) -> None:
    effect = OPERATIONS[kind].effect(SAMPLE_PARAMS)

    assert type(effect) is EXPECTED_EFFECT[kind]


def test_steps_that_remove_rows_never_touch_column_values() -> None:
    #удаление строк разрушительно для датасета, но не для значений колонок
    row_steps = DESTRUCTIVE_STEPS - {StepKind.DROP_COLUMNS}

    for kind in row_steps:
        assert isinstance(OPERATIONS[kind].effect(SAMPLE_PARAMS), DatasetOnlyEffect), kind


def fold(schema: pl.Schema, steps: tuple[Step, ...]) -> dict:
    dataset_id = new_dataset_id()
    tracked = {
        name: _Tracked(ColumnOrigin.SOURCE, (ColumnRef(dataset_id, "d.parquet", name),))
        for name in schema.names()
    }
    _, columns = _fold(dataset_id, "d.parquet", tracked, _result_schema(schema, steps), steps)

    return {column.name: column for column in columns}


def test_a_copy_carries_the_past_of_its_input_and_the_input_keeps_its_own() -> None:
    #копия обрезанной колонки — тоже обрезанная: скрыть это значило бы потерять
    #объяснение её значений. Но шаг, изменивший копию, не принадлежит оригиналу
    schema = pl.Schema({"a": pl.Utf8})
    steps = (
        Step("s1", StepKind.TRIM_WHITESPACE, {"columns": ["a"]}),
        Step("s2", StepKind.DUPLICATE_COLUMN, {"column": "a", "to": "b"}),
        Step("s3", StepKind.CHANGE_CASE, {"columns": ["b"], "case": "upper"}),
    )

    columns = fold(schema, steps)

    assert columns["b"].origin is ColumnOrigin.STEP
    assert [ref.column for ref in columns["b"].origins] == ["a"]
    assert [event.position for event in columns["b"].events] == [1, 2, 3]
    assert columns["b"].events[1].inputs == ("a",)
    assert [event.position for event in columns["a"].events] == [1]


def test_events_follow_pipeline_order_across_a_rename() -> None:
    schema = pl.Schema({"a": pl.Utf8})
    steps = (
        Step("s1", StepKind.TRIM_WHITESPACE, {"columns": ["a"]}),
        Step("s2", StepKind.RENAME_COLUMN, {"column": "a", "to": "b"}),
        Step("s3", StepKind.CHANGE_CASE, {"columns": ["b"], "case": "lower"}),
    )

    column = fold(schema, steps)["b"]

    assert column.origin is ColumnOrigin.SOURCE
    assert [event.position for event in column.events] == [1, 2, 3]
    assert column.events[1].inputs == ("a",)
    assert column.events[1].output == "b"
