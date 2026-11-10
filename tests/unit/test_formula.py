"""Формулы новой колонки: вычисления верны, всё вне белого списка отвергается."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from backend.domain.pipeline.models import PipelineError, Step, StepKind
from backend.services.pipeline import engine
from backend.services.pipeline.formula import compile_formula

FRAME = pl.DataFrame(
    {
        "price": [100.0, 250.0, None],
        "qty": [2, 4, 1],
        "Price ($)": [1.1, 2.2, 3.3],
        "город": [" Москва", "Казань", "Омск"],
        "date": [dt.date(2024, 1, 5), dt.date(2025, 6, 1), dt.date(2023, 12, 31)],
    }
)


def evaluate(expression: str) -> list:
    expr, _ = compile_formula(expression, FRAME.collect_schema())
    return FRAME.select(expr.alias("r"))["r"].to_list()


def test_arithmetic_and_reads() -> None:
    expr, reads = compile_formula("price * qty + 1", FRAME.collect_schema())
    assert FRAME.select(expr.alias("r"))["r"].to_list() == [201.0, 1001.0, None]
    assert reads == ("price", "qty")


def test_bracketed_names_and_cyrillic() -> None:
    assert evaluate("[Price ($)] * 10") == pytest.approx([11.0, 22.0, 33.0])
    assert evaluate("upper(strip(город))") == ["МОСКВА", "КАЗАНЬ", "ОМСК"]


def test_functions_and_conditions() -> None:
    assert evaluate("round(price / 3, 1)") == [33.3, 83.3, None]
    assert evaluate("iif(qty > 2, 'много', 'мало')") == ["мало", "много", "мало"]
    assert evaluate("'да' if qty >= 2 and price > 150 else 'нет'") == ["нет", "да", "нет"]
    assert evaluate("coalesce(price, 0)") == [100.0, 250.0, 0.0]
    assert evaluate("year(date)") == [2024, 2025, 2023]
    assert evaluate("concat(город, '-', str(qty))") == [" Москва-2", "Казань-4", "Омск-1"]
    assert evaluate("max(qty, 3)") == [3, 4, 3]


def test_unknown_column_is_explained() -> None:
    with pytest.raises(PipelineError, match="Колонки «цена» нет"):
        compile_formula("цена * 2", FRAME.collect_schema())


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('dir')",
        "price.__class__",
        "(lambda x: x)(price)",
        "price[0]",
        "open('x')",
        "[x for x in qty]",
        "{'a': 1}",
        "eval('1')",
        "round(price, qty)",
    ],
)
def test_anything_outside_the_whitelist_is_refused(expression: str) -> None:
    with pytest.raises(PipelineError):
        compile_formula(expression, FRAME.collect_schema())


def test_limits() -> None:
    with pytest.raises(PipelineError, match="длиннее"):
        compile_formula("qty + " * 200 + "1", FRAME.collect_schema())
    with pytest.raises(PipelineError, match="пуста"):
        compile_formula("  ", FRAME.collect_schema())


def test_add_column_step_is_planned_and_type_errors_are_caught() -> None:
    schema = FRAME.collect_schema()
    step = Step("s1", StepKind.ADD_COLUMN, {"name": "total", "expression": "price * qty"})
    plans = engine.plan(schema, (step,))
    assert plans[-1].columns_after[-1] == "total"
    result = engine.replay(FRAME.lazy(), schema, (step,)).collect()
    assert result["total"].to_list() == [200.0, 1000.0, None]

    bad = Step("s2", StepKind.ADD_COLUMN, {"name": "oops", "expression": "город * date"})
    with pytest.raises(PipelineError, match="не вычисляется"):
        engine.plan(schema, (bad,))

    clash = Step("s3", StepKind.ADD_COLUMN, {"name": "price", "expression": "qty"})
    with pytest.raises(PipelineError):
        engine.plan(schema, (clash,))
