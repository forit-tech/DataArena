"""Реестр форматов: определение, round-trip, ленивое чтение.

Тесты написаны от смысла, а не от реализации. Главный из них проверяет не «writer вызван
с такими аргументами», а свойство, которое обещано пользователю: **объявленное значение
`preserves_schema` соответствует действительности**. Формат, у которого объявление расходится
с поведением, обязан уронить тест, а не тихо потерять типы при экспорте.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from backend.adapters.formats import capabilities as caps
from backend.adapters.formats.registry import (
    detect_format,
    ensure_supported_extension,
    read_dataset,
    scan_dataset,
    supported_extensions,
    write_dataset,
)
from backend.core.errors import UnsupportedFormatError


@pytest.fixture
def typed_frame() -> pl.DataFrame:
    #таблица намеренно содержит по одному значению каждого типа и null в каждой колонке:
    #потеря типа или пропуска при round-trip обязана быть видна
    return pl.DataFrame(
        {
            "i": [1, 2, None, 4],
            "f": [1.5, None, 3.25, -0.75],
            "s": ["a", "б", None, "текст"],
            "b": [True, False, None, True],
            "d": [dt.date(2024, 1, 1), None, dt.date(2024, 3, 5), dt.date(2024, 7, 9)],
        }
    )


def test_every_declared_format_is_reachable_by_extension() -> None:
    for capabilities in caps.ALL_FORMATS:
        for extension in capabilities.extensions:
            assert extension in supported_extensions()


@pytest.mark.parametrize(
    "capabilities",
    [item for item in caps.ALL_FORMATS if item.preserves_schema],
    ids=lambda item: item.key,
)
def test_schema_preserving_formats_keep_types_and_values_exactly(
    capabilities: caps.FormatCapabilities,
    typed_frame: pl.DataFrame,
    tmp_path: Path,
) -> None:
    #`preserves_schema=True` — это гарантия, поэтому проверяется точное совпадение
    #и схемы, и значений: сохранение типов без сохранения данных бесполезно
    path = tmp_path / f"exact{capabilities.extensions[0]}"
    write_dataset(typed_frame, path, capabilities.key)
    restored = read_dataset(path)

    assert restored.schema == typed_frame.schema, (
        f"Формат {capabilities.key} объявляет preserves_schema=True, "
        f"а схема изменилась: {dict(restored.schema)}"
    )
    assert restored.equals(typed_frame)


#`preserves_schema=False` — отсутствие гарантии, а не обещание терять типы всегда:
#на удачной таблице CSV возвращает ту же схему, и проверка «обязан потерять» проверяла бы
#везение. Поэтому каждое семейство проверяется на своей потере — той, о которой оно
#предупреждает пользователя. Потери у них разные, и один общий зонд их не покрывает.


@pytest.mark.parametrize("key,extension", [("csv", ".csv"), ("tsv", ".tsv")])
def test_text_formats_reinfer_types_from_values(key: str, extension: str, tmp_path: Path) -> None:
    #CSV и TSV не хранят типы вовсе, поэтому при обратном чтении они угадываются заново
    #строковый код, похожий на дату, возвращается датой — и пользователь получает другой датасет
    frame = pl.DataFrame({"код": ["2024-01-01", "2024-02-15", "2024-03-30"]})
    path = tmp_path / f"reinfer{extension}"

    write_dataset(frame, path, key)
    restored = read_dataset(path)

    assert frame.schema["код"] == pl.String
    assert restored.schema["код"] == pl.Date, (
        f"{key} объявляет preserves_schema=False, но строка, похожая на дату, осталась строкой — "
        "объявление стоит перепроверить"
    )


@pytest.mark.parametrize(
    "capabilities",
    [item for item in caps.ALL_FORMATS if item.supports_lazy_scan],
    ids=lambda item: item.key,
)
def test_lazy_scan_returns_a_plan_not_a_table(
    capabilities: caps.FormatCapabilities,
    typed_frame: pl.DataFrame,
    tmp_path: Path,
) -> None:
    #ленивое чтение — способ существования датасета по умолчанию: открытие файла
    #не должно материализовать таблицу
    path = tmp_path / f"lazy{capabilities.extensions[0]}"
    write_dataset(typed_frame, path, capabilities.key)

    lazy = scan_dataset(path)

    assert isinstance(lazy, pl.LazyFrame)
    assert lazy.collect().height == typed_frame.height


def test_scan_reads_only_the_requested_slice(tmp_path: Path) -> None:
    #смысл ленивого пути: страница из середины файла не требует чтения всего файла
    #проверяется свойство, а не время: результат обязан быть правильным срезом
    frame = pl.DataFrame({"n": range(100_000)})
    path = tmp_path / "big.parquet"
    write_dataset(frame, path, "parquet")

    page = scan_dataset(path).slice(50_000, 10).collect()

    assert page.height == 10
    assert page["n"].to_list() == list(range(50_000, 50_010))


def test_signature_wins_over_a_misleading_extension(tmp_path: Path, typed_frame: pl.DataFrame) -> None:
    #расширение — намерение того, кто назвал файл, а не доказательство содержимого
    #Parquet, названный .csv, обязан быть опознан как Parquet
    path = tmp_path / "disguised.csv"
    typed_frame.write_parquet(path)

    assert detect_format(path).key == "parquet"


def test_excel_disguised_as_csv_is_recognised(tmp_path: Path, typed_frame: pl.DataFrame) -> None:
    #выгрузка из Excel, переименованная в .csv, — обычная ситуация, а не экзотика
    path = tmp_path / "report.csv"
    typed_frame.write_excel(path)

    assert detect_format(path).key == "xlsx"


def test_unknown_extension_is_rejected_with_the_supported_list() -> None:
    with pytest.raises(UnsupportedFormatError) as error:
        ensure_supported_extension("data.docx")

    assert ".parquet" in error.value.message


def test_windows_1251_csv_is_read_without_mojibake(tmp_path: Path) -> None:
    #русские выгрузки приходят в cp1251, и формально они декодируются и как cp1252 —
    #только текст при этом превращается в нечитаемые символы
    path = tmp_path / "cp1251.csv"
    path.write_bytes("город,продажи\nМосква,120\nКазань,80\n".encode("cp1251"))

    frame = read_dataset(path)

    assert frame.columns == ["город", "продажи"]
    assert frame["город"].to_list() == ["Москва", "Казань"]


def test_semicolon_separator_is_detected(tmp_path: Path) -> None:
    #точка с запятой — разделитель по умолчанию в русской локали Excel
    path = tmp_path / "semicolon.csv"
    path.write_text("a;b\n1;2\n3;4\n", encoding="utf-8")

    assert read_dataset(path).columns == ["a", "b"]


def test_json_and_jsonl_lose_dates_as_declared(tmp_path: Path, typed_frame: pl.DataFrame) -> None:
    #это измеренное ограничение из AUDIT.md A-3, и Export Dialog обязан о нём предупреждать
    for key, extension in (("json", ".json"), ("jsonl", ".jsonl")):
        path = tmp_path / f"dates{extension}"
        write_dataset(typed_frame, path, key)

        assert read_dataset(path).schema["d"] == pl.String


def test_every_lossy_format_explains_what_it_loses() -> None:
    #предупреждение без объяснения бесполезно: пользователь должен понимать, чем рискует
    for capabilities in caps.ALL_FORMATS:
        if not capabilities.preserves_schema:
            assert capabilities.warnings, f"{capabilities.key} теряет схему, но ничего об этом не говорит"


def test_row_limited_formats_declare_the_limit() -> None:
    assert caps.XLSX.row_limit == 1_048_576
    assert any("1 048 576" in warning for warning in caps.XLSX.warnings)
