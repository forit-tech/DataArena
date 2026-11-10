"""Формулы для новой колонки: безопасный язык выражений поверх Polars.

Формула — это не код пользователя. Строка разбирается синтаксическим анализатором Python
(`ast.parse(mode="eval")`), после чего каждый узел дерева сверяется с белым списком
и переводится в выражение Polars. Ничего не исполняется: нет `eval`, нет доступа
к атрибутам, импортам, индексам, лямбдам и любым функциям вне списка ниже.

Синтаксис:

* колонки — по имени (`price * 1.2`, кириллица разрешена) или в квадратных скобках,
  если в имени есть пробелы и знаки: `[Price ($)] / 90`;
* числа, строки в кавычках, `True`, `False`, `None`;
* арифметика `+ - * / // % **`, сравнения `== != < <= > >=`, `and`, `or`, `not`;
* условие: `iif(условие, если_да, если_нет)` или `a if условие else b`;
* функции: round, abs, sqrt, log, exp, min, max, coalesce, upper, lower, strip, len,
  concat, contains, startswith, year, month, day, num, int, str.

Ограничения длины и глубины выражения не дают построить выражение, которое повесит сервер.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from typing import Any

import polars as pl

from backend.domain.pipeline.models import PipelineError

MAX_LENGTH = 500
MAX_NODES = 200
MAX_DEPTH = 30

_BRACKETS = re.compile(r"\[([^\[\]]+)\]")


def _num(expr: pl.Expr) -> pl.Expr:
    return expr.cast(pl.Float64, strict=False)


FUNCTIONS: dict[str, tuple[int, int | None, Callable[..., pl.Expr]]] = {
    #имя: (минимум аргументов, максимум, построитель)
    "round": (1, 2, lambda x, n=None: x.round(int(_literal(n)) if n is not None else 0)),
    "abs": (1, 1, lambda x: x.abs()),
    "sqrt": (1, 1, lambda x: _num(x).sqrt()),
    "log": (1, 1, lambda x: _num(x).log()),
    "exp": (1, 1, lambda x: _num(x).exp()),
    "min": (2, None, pl.min_horizontal),
    "max": (2, None, pl.max_horizontal),
    "coalesce": (2, None, pl.coalesce),
    "upper": (1, 1, lambda x: x.cast(pl.Utf8).str.to_uppercase()),
    "lower": (1, 1, lambda x: x.cast(pl.Utf8).str.to_lowercase()),
    "strip": (1, 1, lambda x: x.cast(pl.Utf8).str.strip_chars()),
    "len": (1, 1, lambda x: x.cast(pl.Utf8).str.len_chars()),
    "concat": (2, None, lambda *xs: pl.concat_str([x.cast(pl.Utf8) for x in xs], ignore_nulls=True)),
    "contains": (2, 2, lambda x, s: x.cast(pl.Utf8).str.contains(_literal(s), literal=True)),
    "startswith": (2, 2, lambda x, s: x.cast(pl.Utf8).str.starts_with(_literal(s))),
    "year": (1, 1, lambda x: x.dt.year()),
    "month": (1, 1, lambda x: x.dt.month()),
    "day": (1, 1, lambda x: x.dt.day()),
    "num": (1, 1, _num),
    "int": (1, 1, lambda x: _num(x).cast(pl.Int64, strict=False)),
    "str": (1, 1, lambda x: x.cast(pl.Utf8)),
    "iif": (3, 3, lambda c, a, b: pl.when(c).then(a).otherwise(b)),
}

_BINARY: dict[type[ast.operator], Callable[[pl.Expr, pl.Expr], pl.Expr]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_COMPARE: dict[type[ast.cmpop], Callable[[pl.Expr, pl.Expr], pl.Expr]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


class _Literal:
    """Литерал, который нужен функции как значение (число знаков, подстрока), а не как колонка."""

    def __init__(self, value: Any) -> None:
        self.value = value


def _literal(value: Any) -> Any:
    if isinstance(value, _Literal):
        return value.value
    raise PipelineError("Этот аргумент функции должен быть числом или строкой в кавычках, а не колонкой.")


def _expr(value: Any) -> pl.Expr:
    return pl.lit(value.value) if isinstance(value, _Literal) else value


def compile_formula(expression: str, schema: pl.Schema) -> tuple[pl.Expr, tuple[str, ...]]:  # noqa: PLR0915
    """Перевести формулу в выражение Polars. Возвращает выражение и колонки, которые оно читает."""
    text = (expression or "").strip()
    if not text:
        raise PipelineError("Формула пуста.")
    if len(text) > MAX_LENGTH:
        raise PipelineError(f"Формула длиннее {MAX_LENGTH} символов.")
    #[Имя колонки] → col("Имя колонки"): так можно сослаться на колонку с пробелами и знаками
    prepared = _BRACKETS.sub(lambda m: f"col({m.group(1).strip()!r})", text)
    try:
        tree = ast.parse(prepared, mode="eval")
    except SyntaxError as error:
        raise PipelineError(f"В формуле синтаксическая ошибка: {error.msg}.") from error
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise PipelineError("Формула слишком сложная.")

    reads: list[str] = []

    def column(name: str) -> pl.Expr:
        if name not in schema:
            known = ", ".join(list(schema.names())[:8])
            raise PipelineError(
                f"Колонки «{name}» нет. Имена с пробелами пишите в квадратных скобках. Есть, например: {known}."
            )
        if name not in reads:
            reads.append(name)
        return pl.col(name)

    def build(node: ast.AST, depth: int = 0) -> Any:  # noqa: PLR0911, PLR0912
        if depth > MAX_DEPTH:
            raise PipelineError("Формула слишком глубоко вложена.")
        if isinstance(node, ast.Expression):
            return build(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool | int | float | str) or node.value is None:
                return _Literal(node.value)
            raise PipelineError("Недопустимое значение в формуле.")
        if isinstance(node, ast.Name):
            if node.id in ("True", "False", "None"):
                return _Literal({"True": True, "False": False, "None": None}[node.id])
            return column(node.id)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](_expr(build(node.left, depth + 1)), _expr(build(node.right, depth + 1)))
        if isinstance(node, ast.UnaryOp):
            operand = build(node.operand, depth + 1)
            if isinstance(node.op, ast.USub):
                return _Literal(-operand.value) if isinstance(operand, _Literal) else -operand
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.Not):
                return ~_expr(operand)
        if isinstance(node, ast.BoolOp):
            parts = [_expr(build(value, depth + 1)) for value in node.values]
            combined = parts[0]
            for part in parts[1:]:
                combined = combined & part if isinstance(node.op, ast.And) else combined | part
            return combined
        if isinstance(node, ast.Compare):
            left = _expr(build(node.left, depth + 1))
            chain: pl.Expr | None = None
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if type(op) not in _COMPARE:
                    raise PipelineError("Недопустимое сравнение в формуле.")
                right = _expr(build(comparator, depth + 1))
                part = _COMPARE[type(op)](left, right)
                chain = part if chain is None else chain & part
                left = right
            return chain
        if isinstance(node, ast.IfExp):
            return (
                pl.when(_expr(build(node.test, depth + 1)))
                .then(_expr(build(node.body, depth + 1)))
                .otherwise(_expr(build(node.orelse, depth + 1)))
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name == "col":
                if (
                    len(node.args) != 1
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    raise PipelineError("col() принимает имя колонки в кавычках.")
                return column(node.args[0].value)
            if name not in FUNCTIONS:
                allowed = ", ".join(sorted(FUNCTIONS))
                raise PipelineError(f"Функции «{name}» нет. Доступны: {allowed}.")
            low, high, builder = FUNCTIONS[name]
            if len(node.args) < low or (high is not None and len(node.args) > high):
                raise PipelineError(f"Неверное число аргументов у {name}().")
            args = [build(arg, depth + 1) for arg in node.args]
            #функции, которым нужен литерал (round, contains), получают его как есть
            if name in ("round", "contains", "startswith"):
                return builder(_expr(args[0]), *args[1:])
            return builder(*[_expr(arg) for arg in args])
        raise PipelineError(f"В формуле недопустимая конструкция: {type(node).__name__}.")

    result = build(tree)
    return _expr(result), tuple(reads)
