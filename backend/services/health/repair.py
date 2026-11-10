"""Исправление находок диагностики шагами конвейера.

Диагностика сама ничего не меняет. Исправление — это **обычный шаг конвейера**,
предложенный по находке: исходный файл не трогается, результат строится заново,
а откат — это существующая отмена конвейера, которая возвращает точное прежнее
состояние шагов (и, значит, данных), а не «обратную операцию».

Для каждой находки строится набор вариантов:

* ``safe`` — меняет запись значений, ничего не теряя (пробелы по краям, одинаковое
  написание категорий, пометки «н/д» как пропуски);
* ``lossy`` — удаляет строки или превращает значения в пропуски; применяется только
  после того, как пользователь увидел, сколько уйдёт;
* ``decision`` — смысловой выбор человека (чем заполнить пропуски, удалять ли колонку,
  которая может быть частью внешней схемы).

Если безопасного действия нет (выбросы, идентификаторы, высокая кардинальность),
вариантов нет — только объяснение, что проверить вручную.

Предпросмотр и применение привязаны к отпечатку текущих шагов: если конвейер изменили
между предпросмотром и применением, применение отказывает, и устаревшее исправление
не ложится поверх других данных.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import AppError
from backend.domain.health.models import CheckCode, Finding
from backend.domain.pipeline.models import Step, StepKind
from backend.services.health.report import FindingNotFoundError, get_report
from backend.services.pipeline import engine
from backend.services.pipeline import workspace as pipeline_service
from backend.services.workspace_access import open_dataset

MAX_CANONICAL_REPLACEMENTS = 40
SAMPLE_PAIRS = 8
#категория с одним написанием не требует замен
MIN_VARIANTS = 2


class RepairConflictError(AppError):
    status_code = 409
    code = "repair_conflict"


class RepairOptionNotFoundError(AppError):
    status_code = 404
    code = "repair_option_not_found"


@dataclass(frozen=True)
class RepairOption:
    option_id: str
    title: str
    safety: str
    steps: tuple[Step, ...]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "title": self.title,
            "safety": self.safety,
            "note": self.note,
            "steps": [
                {"kind": step.kind.value, "params": step.params, "description": _describe(step)} for step in self.steps
            ],
        }


@dataclass(frozen=True)
class RepairAdvice:
    finding_id: str
    code: str
    columns: tuple[str, ...]
    cause: str
    consequence: str
    approach: str
    options: tuple[RepairOption, ...]
    steps_fingerprint: str
    manual_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "code": self.code,
            "columns": list(self.columns),
            "explanation": {"cause": self.cause, "consequence": self.consequence, "approach": self.approach},
            "options": [option.to_dict() for option in self.options],
            "manual_reason": self.manual_reason,
            "steps_fingerprint": self.steps_fingerprint,
            "source": "rules",
        }


#объяснения по правилам: работают без внешней модели и всегда одинаково
EXPLANATIONS: dict[CheckCode, tuple[str, str, str]] = {
    CheckCode.WHITESPACE_ANOMALY: (
        "Значения начинаются или заканчиваются пробелом — обычно след ручного ввода или выгрузки из таблицы.",
        "«Москва» и «Москва » считаются разными значениями при группировке, объединении и фильтрах.",
        "Убрать пробелы по краям. Содержимое значений не меняется.",
    ),
    CheckCode.CASE_VARIANT_CATEGORIES: (
        "Одна и та же категория записана по-разному: регистром или пробелами.",
        "Подсчёты по категориям дробятся, соединение по такой колонке теряет пары.",
        "Привести каждый вариант к самому частому написанию той же категории.",
    ),
    CheckCode.EMPTY_STRING_VS_NULL: (
        "Отсутствие значения записано то пустой строкой, то пропуском.",
        "Подсчёт пропусков занижается: пустая строка выглядит как значение.",
        "Считать пустые строки и пометки вроде «н/д» пропусками.",
    ),
    CheckCode.ALL_NULL_COLUMN: (
        "Колонка не заполнена ни в одной строке.",
        "Она не несёт сведений; для обучения модели это мусорный признак.",
        "Удалить колонку, если она не нужна внешней схеме или контракту.",
    ),
    CheckCode.CONSTANT_COLUMN: (
        "Во всех строках одно и то же значение.",
        "Колонка не различает строки и бесполезна как признак.",
        "Удалить колонку, если её значение не нужно как метаданные.",
    ),
    CheckCode.NEAR_CONSTANT_COLUMN: (
        "Почти во всех строках одно значение.",
        "Редкие значения могут быть важны, но для большинства моделей признак малоинформативен.",
        "Проверить редкие значения; удалить колонку можно, только если они не важны.",
    ),
    CheckCode.DUPLICATE_ROWS: (
        "Одинаковые строки повторяются.",
        "Повторы завышают подсчёты и искажают обучение: модель видит одни и те же примеры несколько раз.",
        "Оставить по одной строке из каждой группы полных повторов.",
    ),
    CheckCode.DUPLICATE_KEYS: (
        "Значение, похожее на ключ, повторяется в нескольких строках.",
        "Если это действительно ключ, соединения по нему размножат строки.",
        "Решить, какую строку оставить: удаление повторов по ключу теряет данные остальных строк.",
    ),
    CheckCode.MIXED_SEMANTIC_TYPES: (
        "В числовой по смыслу колонке встречаются нечисловые записи.",
        "Колонка читается как текст: считать по ней суммы и средние нельзя.",
        "Считать пометки вроде «н/д» пропусками и разобрать колонку как число; нечисловые значения станут пропусками.",
    ),
    CheckCode.MISSING_VALUES: (
        "В колонке есть пропуски.",
        "Многие модели не принимают пропуски; простые подсчёты тихо их пропускают.",
        "Выбор за вами: оставить пропуски (модель обработает их сама), заполнить или удалить строки.",
    ),
    CheckCode.OUTLIER_VALUES: (
        "Есть значения, сильно отстоящие от остальных.",
        "Выброс бывает ошибкой ввода, а бывает самым важным случаем — автоматически это не решить.",
        "Проверить строки вручную.",
    ),
    CheckCode.HIGH_CARDINALITY: (
        "Очень много разных значений.",
        "Это свойство данных, а не дефект; как признак такая колонка требует особого кодирования.",
        "Исправлять нечего.",
    ),
    CheckCode.POTENTIAL_IDENTIFIER: (
        "Значения почти уникальны — вероятно, это идентификатор.",
        "Идентификатор нельзя использовать как признак модели: он запоминает строки, а не закономерность.",
        "Исключить из признаков при обучении; в данных колонку можно оставить.",
    ),
    CheckCode.NAN_OR_INFINITY: (
        "В числовой колонке есть NaN или бесконечность.",
        "Средние и суммы становятся NaN или бесконечными.",
        "Проверить, откуда они взялись; заменить на пропуск можно после разбора причины.",
    ),
}


def steps_fingerprint(steps: tuple[Step, ...]) -> str:
    payload = json.dumps([step.to_dict() for step in steps], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def advise(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str, finding_id: str
) -> RepairAdvice:
    finding, steps, result = _context(store, workspace_root, workspace_id, dataset_id, finding_id)
    cause, consequence, approach = EXPLANATIONS.get(finding.code, ("", "", "Проверить вручную."))
    options = tuple(option for option in _options(finding, result) if _valid(result, option))
    manual = "" if options else "Безопасного автоматического исправления нет: решение зависит от смысла данных."
    return RepairAdvice(
        finding_id=finding_id,
        code=finding.code.value,
        columns=finding.columns,
        cause=cause,
        consequence=consequence,
        approach=approach,
        options=options,
        steps_fingerprint=steps_fingerprint(steps),
        manual_reason=manual,
    )


def preview(  # noqa: PLR0913, PLR0917 - адрес находки и вариант
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    finding_id: str,
    option_id: str,
) -> dict[str, Any]:
    """Что изменится: строки, колонки, потерянные значения и примеры «было → стало»."""
    finding, steps, result = _context(store, workspace_root, workspace_id, dataset_id, finding_id)
    option = _option(finding, result, option_id)
    schema = result.collect_schema()
    indexed = result.with_row_index("__row__")
    after = engine.replay(indexed, indexed.collect_schema(), option.steps)
    after_schema = after.collect_schema()

    rows_before = int(result.select(pl.len()).collect().item())
    rows_after = int(after.select(pl.len()).collect().item())
    shared = [name for name in after_schema.names() if name in schema and name != "__row__"]
    nulls_before = result.select([pl.col(c).null_count() for c in shared]).collect().row(0) if shared else ()
    nulls_after = after.select([pl.col(c).null_count() for c in shared]).collect().row(0) if shared else ()
    values_lost = sum(max(int(a) - int(b), 0) for a, b in zip(nulls_after, nulls_before, strict=True))

    touched = [c for c in finding.columns if c in schema and c in after_schema]
    samples: list[dict[str, Any]] = []
    changed_rows = 0
    if touched and rows_before == rows_after:
        column = touched[0]
        joined = indexed.select("__row__", pl.col(column).cast(pl.Utf8).alias("before")).join(
            after.select("__row__", pl.col(column).cast(pl.Utf8).alias("after")), on="__row__", how="inner"
        )
        differs = joined.filter(
            (pl.col("before") != pl.col("after")) | (pl.col("before").is_null() != pl.col("after").is_null())
        )
        changed_rows = int(differs.select(pl.len()).collect().item())
        pairs = (
            differs.group_by("before", "after")
            .agg(pl.len().alias("rows"))
            .sort("rows", descending=True)
            .head(SAMPLE_PAIRS)
            .collect()
        )
        samples = [
            {"column": column, "before": row["before"], "after": row["after"], "rows": int(row["rows"])}
            for row in pairs.iter_rows(named=True)
        ]

    return {
        "finding_id": finding_id,
        "option": option.to_dict(),
        "steps_fingerprint": steps_fingerprint(steps),
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_removed": max(rows_before - rows_after, 0),
        "columns_before": len(schema),
        "columns_after": len([n for n in after_schema.names() if n != "__row__"]),
        "columns_removed": [n for n in schema.names() if n not in after_schema],
        "values_lost": values_lost,
        "changed_rows": changed_rows,
        "samples": samples,
    }


def apply(  # noqa: PLR0913, PLR0917 - адрес находки, вариант и ожидаемое состояние
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    dataset_id: str,
    finding_id: str,
    option_id: str,
    expected_fingerprint: str,
) -> dict[str, Any]:
    finding, steps, result = _context(store, workspace_root, workspace_id, dataset_id, finding_id)
    if steps_fingerprint(steps) != expected_fingerprint:
        raise RepairConflictError(
            "Конвейер изменился после предпросмотра. Обновите предпросмотр и примените заново.",
            details={"finding_id": finding_id},
        )
    option = _option(finding, result, option_id)
    #у применённых шагов свои идентификаторы с меткой происхождения: в списке шагов
    #видно, что шаг добавлен исправлением находки, а не вручную
    added = tuple(
        Step(step_id=f"repair-{finding.code.value}-{secrets.token_hex(4)}", kind=step.kind, params=step.params)
        for step in option.steps
    )
    view = pipeline_service.set_steps(store, workspace_root, workspace_id, dataset_id, (*steps, *added))
    return {
        "applied": [step.to_dict() for step in added],
        "option": option.to_dict(),
        "pipeline": view.to_dict(),
    }


#── внутреннее ───────────────────────────────────────────────────────────────


def _context(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str, finding_id: str
) -> tuple[Finding, tuple[Step, ...], pl.LazyFrame]:
    report = get_report(store, workspace_root, workspace_id, dataset_id)
    finding = report.find(finding_id)
    if finding is None:
        raise FindingNotFoundError(
            "Находка не найдена. Возможно, данные изменились и диагностика пересчитана.",
            details={"finding_id": finding_id},
        )
    steps = store.get_pipeline(dataset_id).steps
    _, lazy = open_dataset(store, workspace_root, workspace_id, dataset_id)
    result = engine.replay(lazy, lazy.collect_schema(), steps)
    return finding, steps, result


def _option(finding: Finding, result: pl.LazyFrame, option_id: str) -> RepairOption:
    for option in _options(finding, result):
        if option.option_id == option_id and _valid(result, option):
            return option
    raise RepairOptionNotFoundError("Такого варианта исправления у находки нет.", details={"option_id": option_id})


def _valid(result: pl.LazyFrame, option: RepairOption) -> bool:
    #вариант проверяется тем же планировщиком, что и сохранение конвейера: предложение,
    #которое конвейер отвергнет, не показывается вовсе
    try:
        engine.plan(result.collect_schema(), option.steps)
    except AppError:
        return False
    return True


def _step(kind: StepKind, **params: Any) -> Step:
    #временный идентификатор: планировщик требует уникальных, а при применении
    #шаги получают постоянные идентификаторы с меткой происхождения
    return Step(step_id=f"preview-{secrets.token_hex(4)}", kind=kind, params=params)


def _describe(step: Step) -> str:
    from backend.services.pipeline.operations import OPERATIONS

    return OPERATIONS[step.kind].describe(step.params)


def _textual(result: pl.LazyFrame, column: str) -> bool:
    dtype = result.collect_schema().get(column)
    return dtype is not None and (dtype in (pl.Utf8, pl.String) or isinstance(dtype, pl.Categorical))


def _options(finding: Finding, result: pl.LazyFrame) -> list[RepairOption]:  # noqa: PLR0911
    columns = [c for c in finding.columns if c in result.collect_schema()]
    code = finding.code

    if code is CheckCode.WHITESPACE_ANOMALY and columns:
        return [
            RepairOption("trim", "Убрать пробелы по краям", "safe", (_step(StepKind.TRIM_WHITESPACE, columns=columns),))
        ]

    if code is CheckCode.CASE_VARIANT_CATEGORIES and columns:
        options: list[RepairOption] = []
        canonical = _canonical_replacements(result, columns[0])
        if canonical:
            options.append(
                RepairOption(
                    "canonical",
                    "Привести к самому частому написанию",
                    "safe",
                    (
                        _step(StepKind.TRIM_WHITESPACE, columns=[columns[0]]),
                        *(
                            _step(
                                StepKind.REPLACE_VALUES,
                                column=columns[0],
                                **{"from": source, "to": target},
                                mode="exact",
                            )
                            for source, target in canonical
                        ),
                    ),
                    note=f"Замен: {len(canonical)}. Вариант заменяется самым частым написанием той же категории.",
                )
            )
        options.append(
            RepairOption(
                "lower",
                "Привести всё к нижнему регистру",
                "decision",
                (
                    _step(StepKind.TRIM_WHITESPACE, columns=columns),
                    _step(StepKind.CHANGE_CASE, columns=columns, case="lower"),
                ),
                note="Меняется запись всех значений колонки, включая правильные.",
            )
        )
        return options

    if code is CheckCode.EMPTY_STRING_VS_NULL and columns:
        return [
            RepairOption(
                "nulls", "Считать пустые строки пропусками", "safe", (_step(StepKind.NORMALIZE_NULLS, columns=columns),)
            )
        ]

    if code in (CheckCode.ALL_NULL_COLUMN, CheckCode.CONSTANT_COLUMN, CheckCode.NEAR_CONSTANT_COLUMN) and columns:
        return [
            RepairOption(
                "drop",
                "Удалить колонку",
                "decision",
                (_step(StepKind.DROP_COLUMNS, columns=columns),),
                note="Колонка может быть частью схемы или внешнего контракта: удаляйте, только если она не нужна.",
            )
        ]

    if code is CheckCode.DUPLICATE_ROWS:
        return [RepairOption("dedupe", "Оставить по одной строке из повторов", "lossy", (_step(StepKind.DEDUPLICATE),))]

    if code is CheckCode.DUPLICATE_KEYS and columns:
        return [
            RepairOption(
                "dedupe_keys",
                "Оставить первую строку для каждого значения ключа",
                "lossy",
                (_step(StepKind.DEDUPLICATE, columns=columns),),
                note="Остальные строки с тем же ключом будут удалены вместе со всеми их значениями.",
            )
        ]

    if code is CheckCode.MIXED_SEMANTIC_TYPES and columns and _textual(result, columns[0]):
        return [
            RepairOption(
                "parse",
                "Пометки — в пропуски, колонку — в число",
                "lossy",
                (
                    _step(StepKind.NORMALIZE_NULLS, columns=[columns[0]]),
                    _step(StepKind.PARSE_NUMBER, column=columns[0]),
                ),
                note="Значения, которые не читаются как число, станут пропусками — их число видно в предпросмотре.",
            )
        ]

    if code is CheckCode.MISSING_VALUES and columns:
        return _fill_options(result, columns[0])

    return []


def _fill_options(result: pl.LazyFrame, column: str) -> list[RepairOption]:
    dtype = result.collect_schema()[column]
    leak = (
        "Значение посчитано по всему датасету. Для обучения модели надёжнее заполнять пропуски "
        "внутри ML-конвейера по обучающей части — иначе сведения о тестовой части просачиваются в обучение."
    )
    options: list[RepairOption] = []
    if dtype.is_numeric():
        median = result.select(pl.col(column).median()).collect().item()
        if median is not None:
            value = round(median) if dtype.is_integer() else round(float(median), 6)
            options.append(
                RepairOption(
                    "median",
                    f"Заполнить медианой ({value})",
                    "decision",
                    (_step(StepKind.FILL_MISSING, column=column, value=str(value)),),
                    note=leak,
                )
            )
    else:
        mode = (
            result.select(pl.col(column).drop_nulls().mode().first()).collect().item()
            if result.select(pl.col(column).drop_nulls().len()).collect().item()
            else None
        )
        if mode is not None:
            options.append(
                RepairOption(
                    "mode",
                    f"Заполнить самым частым значением («{mode}»)",
                    "decision",
                    (_step(StepKind.FILL_MISSING, column=column, value=str(mode)),),
                    note=leak,
                )
            )
    options.append(
        RepairOption(
            "drop_rows",
            "Удалить строки с пропусками",
            "lossy",
            (_step(StepKind.FILTER_ROWS, column=column, operator="is_not_null"),),
            note="Строки уходят целиком, вместе со значениями остальных колонок. Число — в предпросмотре.",
        )
    )
    return options


def _canonical_replacements(result: pl.LazyFrame, column: str) -> list[tuple[str, str]]:
    """Пары «вариант → самое частое написание той же категории» (без учёта регистра и пробелов)."""
    if not _textual(result, column):
        return []
    counts = (
        result.select(pl.col(column).cast(pl.Utf8).alias("value"))
        .drop_nulls()
        .with_columns(pl.col("value").str.strip_chars().alias("trimmed"))
        .with_columns(pl.col("trimmed").str.to_lowercase().alias("group"))
        .group_by("group", "trimmed")
        .agg(pl.len().alias("rows"))
        .collect()
    )
    pairs: list[tuple[str, str]] = []
    for group in counts.partition_by("group"):
        if group.height < MIN_VARIANTS:
            continue
        ordered = group.sort(["rows", "trimmed"], descending=[True, False])
        target = ordered["trimmed"][0]
        pairs.extend((variant, target) for variant in ordered["trimmed"][1:].to_list())
        if len(pairs) > MAX_CANONICAL_REPLACEMENTS:
            return []
    return pairs
