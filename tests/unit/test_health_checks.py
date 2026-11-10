"""Проверки диагностики на данных, которые обычно всё ломают.

Каждый случай здесь — не выдумка: пустой датасет, одна строка, тысяча колонок,
колонка из одних пропусков, значения, различающиеся только регистром, NaN, часовые
пояса, огромные строки. Всё это встречается в настоящих выгрузках.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from backend.domain.health.models import (
    CHECKS_VERSION,
    NOTICE_ONLY_CHECKS,
    CheckCode,
    Exactness,
    Finding,
    RowFilter,
    RowFilterKind,
    Scope,
    Severity,
    finding_from_storage,
    finding_to_storage,
)
from backend.services.health import report as health_report
from backend.services.health.checks import analyse
from backend.services.health.filter_values import (
    decode_filter_value,
    encode_filter_value,
)
from backend.services.health.predicates import count_affected, row_filter_expression
from backend.services.workspace_access import open_dataset


def run(frame: pl.DataFrame, tmp_path: Path) -> tuple[list, pl.LazyFrame, pl.Schema]:
    path = tmp_path / "d.parquet"
    frame.write_parquet(path)
    lazy = pl.scan_parquet(path)
    schema = lazy.collect_schema()
    return analyse(lazy, schema, frame.height), lazy, schema


def codes(findings: list) -> set[str]:
    return {finding.code.value for finding in findings}


def _prepared_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: pl.DataFrame | None = None
):
    """Настоящий датасет в настоящем хранилище — для проверок пересчёта отчёта."""
    from backend.core import config
    from backend.domain.dataset.identifiers import new_workspace_id
    from backend.services import context
    from backend.services.dataset_import import import_dataset, uploaded

    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    store = context.get_workspace_store()
    root = context.get_workspace_root()
    workspace_id = store.create_workspace(new_workspace_id(), "кэш").workspace_id

    staging = tmp_path / "upload.part"
    (frame if frame is not None else pl.DataFrame({"a": [None, 1, 2, 3] * 10})).write_parquet(
        staging
    )
    dataset = import_dataset(store, root, workspace_id, staging, "d.parquet", provenance=uploaded, content_addressed=True)

    return store, root, workspace_id, dataset.dataset_id


# ── вырожденные размеры ───────────────────────────────────────────────────────


def test_an_empty_dataset_produces_no_findings(tmp_path: Path) -> None:
    """Пустой датасет — не набор дефектов, а пустой датасет.

    Все доли здесь были бы делением на ноль, а все выводы — бессмысленными.
    """
    findings, _, _ = run(pl.DataFrame({"a": [], "b": []}), tmp_path)

    assert findings == []


def test_a_single_row_does_not_produce_nonsense(tmp_path: Path) -> None:
    #на одной строке «колонка постоянна» и «значения уникальны» верны одновременно
    #и не значат ничего: проверки формы значений ниже порога не срабатывают
    findings, _, _ = run(pl.DataFrame({"a": [1], "b": ["x"]}), tmp_path)

    assert not (
        codes(findings)
        & {"constant_column", "potential_identifier", "high_cardinality", "near_constant_column"}
    )


def test_a_column_of_only_nulls_is_a_defect_without_drill_down(tmp_path: Path) -> None:
    findings, _, _ = run(pl.DataFrame({"пусто": [None] * 50, "a": list(range(50))}), tmp_path)
    empty = next(item for item in findings if item.code is CheckCode.ALL_NULL_COLUMN)

    assert empty.severity is Severity.PROBLEM
    assert empty.affected_rows == 50
    #провала нет: затронуты все строки, и «показать их» — это открыть датасет
    assert empty.row_filter is None


def test_a_column_of_identical_values_is_only_a_signal(tmp_path: Path) -> None:
    findings, _, _ = run(pl.DataFrame({"одно": ["x"] * 50, "a": list(range(50))}), tmp_path)
    constant = next(item for item in findings if item.code is CheckCode.CONSTANT_COLUMN)

    assert constant.severity is Severity.NOTICE
    assert constant.row_filter is None


def test_a_dataset_of_a_thousand_columns_is_handled(tmp_path: Path) -> None:
    #план дробится по группам колонок: тысяча колонок на десяток показателей —
    #это десять тысяч выражений в одном запросе
    frame = pl.DataFrame({f"c{index}": [index, index, None] * 10 for index in range(1000)})
    findings, _, _ = run(frame, tmp_path)

    assert codes(findings)
    assert all(isinstance(item, Finding) for item in findings)


# ── типы, которые ломают наивные проверки ─────────────────────────────────────


#колонка каждого типа, почти постоянная: 199 одинаковых значений и одно другое —
#доля 99,5% выше порога near_constant_share. Именно
#на такой колонке рождается предикат со значением внутри — единственный вид предиката,
#которому есть что терять при хранении
_NEAR_CONSTANT_BY_TYPE: dict[str, pl.Series] = {
    "текст": pl.Series(["да"] * 199 + ["нет"], dtype=pl.Utf8),
    "целое": pl.Series([7] * 199 + [8], dtype=pl.Int64),
    "дробное": pl.Series([1.5] * 199 + [2.5], dtype=pl.Float64),
    "логика": pl.Series([True] * 199 + [False], dtype=pl.Boolean),
    "деньги": pl.Series([Decimal("1.2345")] * 199 + [Decimal("9.9999")], dtype=pl.Decimal(10, 4)),
    "дата": pl.Series([dt.date(2024, 1, 1)] * 199 + [dt.date(2024, 2, 2)], dtype=pl.Date),
    "момент": pl.Series(
        [dt.datetime(2024, 1, 1, 10, 30, 15, 123456)] * 199 + [dt.datetime(2024, 2, 2)],
        dtype=pl.Datetime("us"),
    ),
    "момент_с_поясом": pl.Series(
        [dt.datetime(2024, 1, 1, 10, tzinfo=dt.UTC)] * 199 + [dt.datetime(2024, 2, 2, tzinfo=dt.UTC)],
        dtype=pl.Datetime("us", "UTC"),
    ),
    "время_суток": pl.Series(
        [dt.time(10, 30, 15, 123456)] * 199 + [dt.time(11, 0)], dtype=pl.Time
    ),
    "длительность": pl.Series(
        [dt.timedelta(seconds=90, microseconds=7)] * 199 + [dt.timedelta(days=1)],
        dtype=pl.Duration("us"),
    ),
    "двоичное": pl.Series([b"\x01\x02\xff"] * 199 + [b"\xfe"], dtype=pl.Binary),
    "категория": pl.Series(["да"] * 199 + ["нет"], dtype=pl.Categorical),
    "перечисление": pl.Series(["да"] * 199 + ["нет"], dtype=pl.Enum(["да", "нет"])),
}


def test_a_finding_survives_storage_and_shows_the_same_rows_for_every_column_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт обязан сохраняться и разрешаться в строки для колонки любого типа.

    Проверка идёт через **настоящее хранилище**, а не через `analyse`: дефект, из-за
    которого раздел намертво падал с 500 на колонке с почти постоянной датой, жил
    ровно в сохранении отчёта. Проверка, не доходящая до `save_health`, его не видела.
    """
    frame = pl.DataFrame(dict(_NEAR_CONSTANT_BY_TYPE))
    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch, frame)

    report = health_report.get_report(store, root, workspace_id, dataset_id)

    covered = {
        finding.columns[0]
        for finding in report.findings
        if finding.code is CheckCode.NEAR_CONSTANT_COLUMN
    }
    assert covered == set(_NEAR_CONSTANT_BY_TYPE), (
        "находка о почти постоянной колонке пропала для типов: "
        f"{sorted(set(_NEAR_CONSTANT_BY_TYPE) - covered)}"
    )

    _, lazy = open_dataset(store, root, workspace_id, dataset_id)

    for finding in report.findings:
        if finding.row_filter is None:
            continue

        resolved = health_report.resolve_finding(
            store,
            root,
            workspace_id,
            dataset_id,
            finding.identity(dataset_id, report.artifact_fingerprint),
        )
        shown = int(lazy.filter(resolved.expression).select(pl.len()).collect().item())

        assert shown == finding.affected_rows, (
            f"{finding.code.value} по колонке {finding.columns}: "
            f"находка говорит {finding.affected_rows}, таблица показывает {shown}"
        )


def test_a_column_of_lists_produces_no_finding_instead_of_a_wrong_one(tmp_path: Path) -> None:
    #у колонки списков доминирующего значения нет: `mode()` отдаёт Series, и сравнение
    #с ним падает. Правильный ответ — промолчать, а не показать непроверенные строки
    frame = pl.DataFrame(
        {
            "списки": pl.Series([[1, 2]] * 199 + [[3]], dtype=pl.List(pl.Int64)),
            "текст": pl.Series(["да"] * 199 + ["нет"], dtype=pl.Utf8),
        }
    )
    findings, lazy, schema = run(frame, tmp_path)

    assert not [
        finding
        for finding in findings
        if finding.code is CheckCode.NEAR_CONSTANT_COLUMN and finding.columns == ("списки",)
    ]
    assert [
        finding
        for finding in findings
        if finding.code is CheckCode.NEAR_CONSTANT_COLUMN and finding.columns == ("текст",)
    ], "соседняя обычная колонка обязана остаться проверенной"

    for finding in findings:
        if finding.row_filter is not None:
            assert count_affected(lazy, finding.row_filter, schema) == finding.affected_rows


def test_a_stored_predicate_value_selects_the_same_rows_as_the_original_value(
    tmp_path: Path,
) -> None:
    """Значение после хранения обязано отбирать те же строки, что и исходное.

    Сверка идёт с **исходным значением Polars**, а не с другим предикатом: находка и
    провал в строки идут одним путём и согласятся друг с другом даже тогда, когда оба
    неверны. Измерено: `timedelta` через секунды и `bytes` через описание не падают,
    а отбирают 3 строки вместо 1.
    """
    for name, series in _NEAR_CONSTANT_BY_TYPE.items():
        frame = pl.DataFrame({name: series})
        dtype = frame.schema[name]
        native = frame.select(pl.col(name).drop_nulls().mode().first()).item()
        truth = frame.filter(pl.col(name).ne_missing(native)).height

        stored = json.loads(json.dumps(encode_filter_value(native, dtype)))
        restored = frame.filter(pl.col(name).ne_missing(decode_filter_value(stored, dtype))).height

        assert restored == truth, f"{name}: после хранения {restored} строк вместо {truth}"


def test_nan_and_infinity_are_reported_once_and_not_as_outliers(tmp_path: Path) -> None:
    """NaN не должен попадать одновременно в «не число» и в «выбросы».

    Измерено: без исключения неконечных значений выбросами объявлялись ровно те же
    строки, что NaN, и пользователь видел одну проблему под двумя заголовками.
    """
    values = [float("nan")] * 5 + [float("inf")] * 2 + [float(index) for index in range(43)]
    findings, lazy, schema = run(pl.DataFrame({"x": values}), tmp_path)

    nan_finding = next(item for item in findings if item.code is CheckCode.NAN_OR_INFINITY)
    assert nan_finding.affected_rows == 7

    outliers = [item for item in findings if item.code is CheckCode.OUTLIER_VALUES]

    for finding in outliers:
        rows = lazy.filter(row_filter_expression(finding.row_filter, schema)).collect()
        assert rows["x"].is_finite().all(), "в выбросы попали NaN или бесконечности"


def test_unicode_and_huge_strings_survive(tmp_path: Path) -> None:
    #огромное значение не должно ни попасть в примеры целиком, ни сломать сериализацию
    frame = pl.DataFrame(
        {
            "текст": ["Москва", "москва", "МОСКВА", "х" * 100_000, "🙂 emoji"] * 10,
        }
    )
    findings, _, _ = run(frame, tmp_path)
    variants = next(item for item in findings if item.code is CheckCode.CASE_VARIANT_CATEGORIES)

    for value in variants.evidence.values:
        assert len(str(value)) <= 201, "пример значения не обрезан"


def test_values_differing_only_by_case_or_whitespace_are_found(tmp_path: Path) -> None:
    frame = pl.DataFrame({"город": ["Москва", "москва", "Москва ", "Казань"] * 10})
    findings, lazy, schema = run(frame, tmp_path)
    variants = next(item for item in findings if item.code is CheckCode.CASE_VARIANT_CATEGORIES)

    rows = lazy.filter(row_filter_expression(variants.row_filter, schema)).collect()

    #в выборку попали три написания Москвы и не попала Казань
    assert rows.height == 30
    assert set(rows["город"].to_list()) == {"Москва", "москва", "Москва "}


def test_empty_string_is_distinguished_from_a_missing_value(tmp_path: Path) -> None:
    frame = pl.DataFrame({"a": ["", "x", None, "y"] * 10})
    findings, _, _ = run(frame, tmp_path)

    assert CheckCode.EMPTY_STRING_VS_NULL.value in codes(findings)
    assert CheckCode.MISSING_VALUES.value in codes(findings)


def test_non_numeric_marks_among_numbers_are_found(tmp_path: Path) -> None:
    #«н/д» и «-» внутри числовой колонки: из-за них колонка читается как текст
    frame = pl.DataFrame({"сумма": [str(index) for index in range(97)] + ["н/д", "-", "н/д"]})
    findings, lazy, schema = run(frame, tmp_path)
    mixed = next(item for item in findings if item.code is CheckCode.MIXED_SEMANTIC_TYPES)

    assert mixed.affected_rows == 3
    rows = lazy.filter(row_filter_expression(mixed.row_filter, schema)).collect()
    assert sorted(rows["сумма"].to_list()) == ["-", "н/д", "н/д"]


def test_duplicate_rows_are_counted_over_the_dataset_columns_only(tmp_path: Path) -> None:
    """Полные дубликаты считаются по колонкам датасета, а не по всему плану.

    Если считать через `pl.all()`, служебная колонка с номером строки попадёт в набор,
    каждая строка станет уникальной и дубликатов не найдётся никогда. Проверено
    измерением: с номером в наборе результат был ноль.
    """
    frame = pl.DataFrame({"a": [1, 1, 2, 3] * 10, "b": ["x", "x", "y", "z"] * 10})
    findings, lazy, schema = run(frame, tmp_path)
    duplicates = next(item for item in findings if item.code is CheckCode.DUPLICATE_ROWS)

    assert duplicates.affected_rows == 40
    #и тот же ответ через выражение, применённое к плану с номером строки
    with_ordinal = lazy.with_row_index("__ordinal__")
    assert (
        with_ordinal.filter(row_filter_expression(duplicates.row_filter, schema))
        .select(pl.len())
        .collect()
        .item()
        == 40
    )


def test_a_finding_can_cover_almost_every_row(tmp_path: Path) -> None:
    #находка на сотни тысяч строк не должна нести список номеров: она несёт предикат
    frame = pl.DataFrame({"a": [None] * 199_999 + [1]})
    findings, lazy, schema = run(frame, tmp_path)
    missing = next(item for item in findings if item.code is CheckCode.MISSING_VALUES)

    assert missing.affected_rows == 199_999
    assert missing.row_filter == RowFilter(kind=RowFilterKind.IS_NULL, columns=("a",))
    assert count_affected(lazy, missing.row_filter, schema) == 199_999


# ── инварианты модели ─────────────────────────────────────────────────────────


def test_the_count_in_a_finding_always_matches_its_predicate(tmp_path: Path) -> None:
    """Главный инвариант этапа, проверенный на смешанном датасете.

    Подсчёт внутри находки и отбор строк идут через одно и то же описание. Если бы это
    были две похожие реализации, они однажды разошлись бы — и находка сказала бы «127»,
    а таблица показала бы «119».
    """
    frame = pl.DataFrame(
        {
            "id": list(range(200)),
            "город": [["Москва", "москва", "Казань ", None, "Омск"][n % 5] for n in range(200)],
            "сумма": [None if n % 20 == 0 else float(n % 50) for n in range(200)],
            "дробь": [float("nan") if n % 50 == 0 else float(n) for n in range(200)],
            "почти": ["да"] * 198 + ["нет", "нет"],
        }
    )
    findings, lazy, schema = run(frame, tmp_path)
    checked = 0

    for finding in findings:
        if finding.row_filter is None:
            continue

        assert count_affected(lazy, finding.row_filter, schema) == finding.affected_rows, (
            finding.code.value
        )
        checked += 1

    assert checked >= 5


@pytest.mark.parametrize("code", sorted(NOTICE_ONLY_CHECKS))
def test_a_signal_cannot_be_declared_a_defect(code: CheckCode) -> None:
    """Попытка выдать свойство данных за дефект возбуждает ошибку, а не проходит тихо.

    Иначе пользователь пойдёт «чинить» нормальные данные: удалять идентификатор
    и срезать настоящие крупные значения.
    """
    with pytest.raises(ValueError, match="не может быть серьёзнее сигнала"):
        Finding(
            code=code,
            severity=Severity.PROBLEM,
            scope=Scope.COLUMN,
            columns=("a",),
            title="t",
            explanation="e",
        )


def test_a_sampled_finding_must_say_how_many_rows_it_saw() -> None:
    #выборочное число без указания выборки неотличимо от точного
    with pytest.raises(ValueError, match="сколько строк просмотрено"):
        Finding(
            code=CheckCode.MISSING_VALUES,
            severity=Severity.WARNING,
            scope=Scope.COLUMN,
            columns=("a",),
            title="t",
            explanation="e",
            exactness=Exactness.SAMPLED,
        )


def test_a_finding_survives_storage_unchanged() -> None:
    #описание предиката обязано пережить запись: иначе после перезапуска провал в строки
    #пришлось бы пересчитывать целиком
    original = Finding(
        code=CheckCode.MISSING_VALUES,
        severity=Severity.WARNING,
        scope=Scope.COLUMN,
        columns=("сумма",),
        title="t",
        explanation="e",
        affected_rows=10,
        affected_ratio=0.1,
        row_filter=RowFilter(kind=RowFilterKind.IS_NULL, columns=("сумма",)),
    )

    restored = finding_from_storage(finding_to_storage(original))

    assert restored == original


def test_the_identifier_changes_with_the_artifact() -> None:
    """Отпечаток артефакта входит в идентификатор находки намеренно.

    После пересборки артефакта прежняя ссылка перестаёт разрешаться — это видно
    как отдельный ответ, а не как показ других строк.
    """
    finding = Finding(
        code=CheckCode.MISSING_VALUES,
        severity=Severity.WARNING,
        scope=Scope.COLUMN,
        columns=("a",),
        title="t",
        explanation="e",
    )

    assert finding.identity("ds_1", "aaa") != finding.identity("ds_1", "bbb")
    assert finding.identity("ds_1", "aaa") != finding.identity("ds_2", "aaa")
    assert finding.identity("ds_1", "aaa") == finding.identity("ds_1", "aaa")


def test_a_large_dataset_says_that_its_cardinality_is_approximate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """За порогом объёма кардинальность считается приближённо — и это сказано.

    Порог понижен в тесте, чтобы не собирать датасет на двадцать миллионов клеток:
    проверяется не сам порог, а то, что за ним находка меняет свойство точности.
    Показать приближение как точное число — значит соврать.
    """
    from backend.services.health import checks

    monkeypatch.setattr(checks, "APPROXIMATE_CARDINALITY_CELLS", 10)

    frame = pl.DataFrame({"id": list(range(60))})
    findings, _, _ = run(frame, tmp_path)
    identifier = next(item for item in findings if item.code is CheckCode.POTENTIAL_IDENTIFIER)

    assert identifier.exactness is Exactness.SAMPLED
    assert identifier.sampled_rows == 60


def test_a_small_dataset_reports_exact_cardinality(tmp_path: Path) -> None:
    #обратная сторона: ниже порога число обязано быть точным и помеченным точным
    findings, _, _ = run(pl.DataFrame({"id": list(range(60))}), tmp_path)
    identifier = next(item for item in findings if item.code is CheckCode.POTENTIAL_IDENTIFIER)

    assert identifier.exactness is Exactness.EXACT
    assert identifier.sampled_rows is None


def test_repeats_in_an_almost_unique_column_are_found(tmp_path: Path) -> None:
    """Колонка, похожая на ключ, но с повторами.

    У почти уникального столбца повтор обычно означает ошибку выгрузки или случайное
    объединение — в отличие от повторов в обычной категории, которые нормальны.
    """
    identifiers = [*range(999), 0]
    findings, lazy, schema = run(pl.DataFrame({"код": identifiers}), tmp_path)
    duplicates = next(item for item in findings if item.code is CheckCode.DUPLICATE_KEYS)

    assert duplicates.severity is Severity.WARNING
    assert duplicates.affected_rows == 2
    assert duplicates.row_filter == RowFilter(
        kind=RowFilterKind.DUPLICATED_BY, columns=("код",)
    )

    rows = lazy.filter(row_filter_expression(duplicates.row_filter, schema)).collect()
    assert rows["код"].to_list() == [0, 0]


def test_a_fully_unique_column_has_no_duplicate_finding(tmp_path: Path) -> None:
    #обратная сторона: у настоящего ключа повторов нет, и находки быть не должно
    findings, _, _ = run(pl.DataFrame({"код": list(range(1000))}), tmp_path)

    assert CheckCode.DUPLICATE_KEYS.value not in codes(findings)
    assert CheckCode.POTENTIAL_IDENTIFIER.value in codes(findings)


# ── инвалидация отчёта ────────────────────────────────────────────────────────


def _stored_with(report, **changes):
    from dataclasses import replace

    return replace(report, **changes)


def test_the_report_is_recomputed_when_the_artifact_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт с чужим отпечатком не выдаётся за актуальный.

    Пробел найден мутацией: сама проверка работала, но её не проверял никто, и снятие
    сравнения отпечатков проходило незамеченным. Старые находки под новым артефактом —
    это диагностика данных, которых уже нет.
    """
    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch)
    first = health_report.get_report(store, root, workspace_id, dataset_id)

    #подменяем отпечаток в сохранённом отчёте: так выглядит отчёт, посчитанный
    #по другому артефакту
    store.save_health(_stored_with(first, artifact_fingerprint="0" * 64))
    again = health_report.get_report(store, root, workspace_id, dataset_id)

    assert again.artifact_fingerprint == first.artifact_fingerprint
    assert again.computed_at != first.computed_at, "отчёт не пересчитан"


def test_the_report_is_recomputed_when_the_checks_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #изменилась логика проверок — прежние находки описывают другое поведение
    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch)
    first = health_report.get_report(store, root, workspace_id, dataset_id)

    store.save_health(_stored_with(first, checks_version=first.checks_version - 1))
    again = health_report.get_report(store, root, workspace_id, dataset_id)

    assert again.checks_version == CHECKS_VERSION
    assert again.computed_at != first.computed_at, "отчёт не пересчитан"


def test_an_unchanged_dataset_is_not_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #обратная сторона: пересчёт без причины — это полный проход по датасету на каждое
    #открытие раздела
    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch)
    first = health_report.get_report(store, root, workspace_id, dataset_id)
    again = health_report.get_report(store, root, workspace_id, dataset_id)

    assert again.computed_at == first.computed_at


# ── находки, исправленные враждебным разбором ─────────────────────────────────


def test_categorical_and_enum_are_checked_like_text(tmp_path: Path) -> None:
    """Словарная колонка — тот же текст, и проверяться обязана так же.

    Parquet, записанный pandas или pyarrow, хранит категории словарём. Пока проверки
    смотрели только на `Utf8`, такая колонка молча минула разнописание регистра,
    краевые пробелы и пустые строки — а отчёт говорил «ничего не нашли».
    """
    values = ["Москва", "москва", "Казань ", "", "Омск", None]
    found: dict[str, set[str]] = {}

    for label, dtype in (
        ("текст", pl.Utf8),
        ("категория", pl.Categorical),
        ("перечисление", pl.Enum(["Москва", "москва", "Казань ", "", "Омск"])),
    ):
        frame = pl.DataFrame({"город": pl.Series(values * 50, dtype=dtype)})
        directory = tmp_path / label
        directory.mkdir()
        findings, lazy, schema = run(frame, directory)
        found[label] = codes(findings)

        for finding in findings:
            if finding.row_filter is not None:
                assert count_affected(lazy, finding.row_filter, schema) == finding.affected_rows

    assert found["категория"] == found["текст"], "категориальная колонка проверена не так же"
    assert found["перечисление"] == found["текст"], "перечисление проверено не так же"


def test_a_finding_about_repeats_is_not_produced_when_there_are_none(tmp_path: Path) -> None:
    """Предупреждение «0 строк повторяют значение» — это не находка, а шум.

    За порогом приближения кардинальность считается HyperLogLog, и он ошибается
    на доли процента. Недооценки хватало, чтобы уникальная колонка объявлялась
    колонкой с повторами: предупреждение, кнопка «Показать строки» и пустая таблица.
    """
    frame = pl.DataFrame({"ключ": list(range(300))})
    findings, _, _ = run(frame, tmp_path)

    repeats = [item for item in findings if item.code is CheckCode.DUPLICATE_KEYS]

    assert repeats == [], "находка о повторах выпущена при нуле повторов"


def test_a_constant_column_admits_it_was_judged_by_an_estimate(tmp_path: Path) -> None:
    #постоянство выводится из кардинальности, а за порогом она приближённая:
    #подпись «точный подсчёт» под такой находкой — это оценка, выданная за факт
    from backend.services.health import checks

    frame = pl.DataFrame({"одно": ["x"] * 60, "разное": list(range(60))})
    path = tmp_path / "d.parquet"
    frame.write_parquet(path)
    lazy = pl.scan_parquet(path)

    exact = checks.analyse(lazy, lazy.collect_schema(), frame.height)
    constant_exact = next(f for f in exact if f.code is CheckCode.CONSTANT_COLUMN)
    assert constant_exact.exactness is Exactness.EXACT

    #порог опускается так, чтобы кардинальность считалась приближённо
    original = checks.APPROXIMATE_CARDINALITY_CELLS
    checks.APPROXIMATE_CARDINALITY_CELLS = 1

    try:
        sampled = checks.analyse(lazy, lazy.collect_schema(), frame.height)
    finally:
        checks.APPROXIMATE_CARDINALITY_CELLS = original

    constant_sampled = next(f for f in sampled if f.code is CheckCode.CONSTANT_COLUMN)
    assert constant_sampled.exactness is Exactness.SAMPLED
    assert constant_sampled.sampled_rows == frame.height


def test_case_variants_report_all_groups_even_when_rows_are_shown_for_some(
    tmp_path: Path,
) -> None:
    """Находка обязана называть полное число расхождений, а не показанное.

    Прежде при пятистах расходящихся группах она сообщала «записаны по-разному:
    200 значений»: человек исправлял их, открывал заново — и видел следующие 200,
    не понимая, откуда они взялись.
    """
    groups = 500
    frame = pl.DataFrame(
        {"город": [f"Город{index}" if side == 0 else f"город{index}"
                   for index in range(groups) for side in (0, 1)]}
    )
    findings, lazy, schema = run(frame, tmp_path)
    finding = next(item for item in findings if item.code is CheckCode.CASE_VARIANT_CATEGORIES)

    assert finding.evidence.counts["groups"] == groups
    assert f"{groups}" in finding.explanation
    assert "Строки показаны для" in finding.explanation, "усечение не признано"
    #инвариант при этом держится: сколько сказано строк, столько и отберётся
    assert count_affected(lazy, finding.row_filter, schema) == finding.affected_rows


def test_the_report_is_recomputed_when_the_artifact_is_replaced_behind_our_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Артефакт — обычный файл, и его можно перезаписать мимо DataArena.

    Отпечаток хранится в записи датасета и при этом не меняется, поэтому отчёт
    оставался «актуальным»: находка обещала 25 строк, а в датасете их стало 100.
    """
    from backend.services.workspace_access import reading_path

    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch)
    first = health_report.get_report(store, root, workspace_id, dataset_id)

    path = reading_path(root, store.get_dataset(dataset_id))
    pl.DataFrame({"a": list(range(400))}).write_parquet(path)

    again = health_report.get_report(store, root, workspace_id, dataset_id)

    assert again.total_rows != first.total_rows, "отчёт описывает данные, которых больше нет"
    assert again.artifact_stamp != first.artifact_stamp


def test_a_huge_value_does_not_end_up_in_the_stored_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт на 300 строк весил два мегабайта, а ответ API — нет.

    Доминирующее значение уходило в предикат целиком. В ответе его не видно (пример
    обрезается), поэтому заметить это можно было только по размеру базы — то есть поздно.
    Находка при этом остаётся верной: исчезает только провал в строки.
    """
    frame = pl.DataFrame({"текст": ["ц" * 100_000] * 199 + ["другое"]})
    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch, frame)

    report = health_report.get_report(store, root, workspace_id, dataset_id)
    near = next(
        item for item in report.findings if item.code is CheckCode.NEAR_CONSTANT_COLUMN
    )

    assert near.affected_rows == 1, "находка обязана остаться верной"
    assert near.row_filter is None, "огромное значение не должно попасть в предикат"

    stored = json.dumps(
        [finding_to_storage(item) for item in report.findings], ensure_ascii=False
    )
    assert len(stored) < 20_000, f"сохранённый отчёт занял {len(stored)} символов"


def test_parallel_requests_compute_the_report_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Восемь вкладок считали один и тот же отчёт восемь раз.

    Целостность при этом не страдала — побеждала последняя запись, — но на датасете,
    где расчёт стоит секунды и сотни мегабайт, это восьмикратная плата за одно и то же.
    """
    import threading

    store, root, workspace_id, dataset_id = _prepared_dataset(tmp_path, monkeypatch)
    computed = 0
    original = health_report._compute

    def counting(*args, **kwargs):
        nonlocal computed
        computed += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(health_report, "_compute", counting)

    threads = [
        threading.Thread(
            target=lambda: health_report.get_report(store, root, workspace_id, dataset_id)
        )
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert computed == 1, f"отчёт посчитан {computed} раз вместо одного"
