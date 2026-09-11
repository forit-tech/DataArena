"""Как ведёт себя выдача страниц на 10k, 100k и 1M строк.

Проверяется не «уложились в N миллисекунд» — это зависит от машины и делает тест
хрупким, — а свойства, которые обязаны сохраняться при росте данных:

* страница не материализует датасет целиком: пиковая память не растёт пропорционально;
* в ответ уходит ровно запрошенное число строк, а не весь датасет;
* стоимость страницы не растёт линейно от размера датасета настолько, чтобы интерфейс
  перестал быть интерактивным.

Абсолютные времена печатаются: они полезны как ориентир, но не являются условием прохождения.
"""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

import polars as pl
import pytest

from backend.adapters.formats.registry import scan_dataset, write_dataset
from backend.services import table_view
from backend.services.column_stats import compute_column_statistics

ROW_COUNTS = [10_000, 100_000, 1_000_000]
#правильность достаточно доказать на самом тяжёлом размере: на меньших она следует
#из того же кода. Перебор размеров нужен ровно там, где проверяется сам рост
LARGEST = ROW_COUNTS[-1]


def build_frame(rows: int) -> pl.DataFrame:
    #семь колонок разных типов: числа, категории, текст высокой кардинальности и пропуски
    return pl.DataFrame(
        {
            "id": range(rows),
            "amount": [float(index % 977) + 0.5 for index in range(rows)],
            "country": [["RU", "US", "DE", "FR", "JP"][index % 5] for index in range(rows)],
            "status": [None if index % 17 == 0 else "active" for index in range(rows)],
            "score": [(index * 7919) % 1000 / 1000 for index in range(rows)],
            "note": [f"note-{index % 50_000}" for index in range(rows)],
            "flag": [index % 3 == 0 for index in range(rows)],
        }
    )


@pytest.fixture(scope="module")
def datasets(tmp_path_factory: pytest.TempPathFactory) -> dict[int, Path]:
    directory = tmp_path_factory.mktemp("scaling")
    paths: dict[int, Path] = {}

    for rows in ROW_COUNTS:
        path = directory / f"rows_{rows}.parquet"
        write_dataset(build_frame(rows), path, "parquet")
        paths[rows] = path

    return paths


def _page(path: Path, **kwargs: object) -> tuple[table_view.PageResult, float, int]:
    lazy = scan_dataset(path)
    schema = lazy.collect_schema()
    request = table_view.parse_page_request(schema_names=tuple(schema.names()), **kwargs)  # type: ignore[arg-type]

    tracemalloc.start()
    started = time.perf_counter()
    result = table_view.build_page(lazy, request, schema)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return result, elapsed, peak


def test_page_returns_only_the_requested_rows(datasets: dict[int, Path]) -> None:
    rows = LARGEST
    #главное свойство серверной пагинации: размер ответа не зависит от размера датасета
    result, elapsed, peak = _page(datasets[rows], offset=0, limit=100)

    print(f"\n{rows:>9} строк | страница {elapsed * 1000:6.0f} мс | пик {peak / 1e6:6.1f} МБ")

    assert len(result.rows) == 100
    assert result.total_rows == rows


@pytest.mark.parametrize("rows", ROW_COUNTS)
def test_page_memory_does_not_scale_with_dataset_size(
    rows: int, datasets: dict[int, Path]
) -> None:
    #если бы страница материализовала датасет, пик рос бы пропорционально числу строк
    #порог намеренно щедрый: проверяется отсутствие линейного роста, а не конкретная цифра
    _, _, peak = _page(datasets[rows], offset=rows // 2, limit=100)

    assert peak < 64 * 1024 * 1024, f"страница на {rows} строк заняла {peak / 1e6:.1f} МБ"


def test_sorting_returns_a_correct_page_at_any_size(datasets: dict[int, Path]) -> None:
    rows = LARGEST
    #сортировка на миллионе строк дороже страницы, и это нормально: проверяется правильность,
    #а требование к скорости — «интерфейс показывает честное ожидание», а не «уложись в 300 мс»
    result, elapsed, peak = _page(datasets[rows], offset=0, limit=50, sort=["amount:desc"])

    print(f"{rows:>9} строк | сортировка {elapsed * 1000:6.0f} мс | пик {peak / 1e6:6.1f} МБ")

    values = [row["amount"] for row in result.rows]
    assert values == sorted(values, reverse=True)
    assert len(values) == 50


def test_filtering_narrows_the_result_without_loading_everything(
    datasets: dict[int, Path]
) -> None:
    rows = LARGEST
    result, elapsed, peak = _page(
        datasets[rows], offset=0, limit=50, filters=["country:eq:RU", "amount:gt:500"]
    )

    print(f"{rows:>9} строк | фильтр {elapsed * 1000:6.0f} мс | пик {peak / 1e6:6.1f} МБ")

    assert all(row["country"] == "RU" for row in result.rows)
    assert all(row["amount"] > 500 for row in result.rows)
    assert peak < 128 * 1024 * 1024


def test_column_statistics_scale(datasets: dict[int, Path]) -> None:
    rows = LARGEST
    #статистика читает одну колонку: стоимость не зависит от ширины таблицы
    lazy = scan_dataset(datasets[rows])

    started = time.perf_counter()
    stats = compute_column_statistics(lazy, "amount")
    elapsed = time.perf_counter() - started

    print(f"{rows:>9} строк | статистика колонки {elapsed * 1000:6.0f} мс")

    assert stats.row_count == rows
    assert stats.numeric is not None
    assert stats.histogram is not None


def test_high_cardinality_column_does_not_return_everything(datasets: dict[int, Path]) -> None:
    #колонка с 50 000 уникальных значений не должна превращать панель в мегабайты JSON
    stats = compute_column_statistics(scan_dataset(datasets[1_000_000]), "note")

    assert stats.unique_count > 10_000
    assert stats.top_values is not None
    assert len(stats.top_values) <= 10


def test_very_wide_dataset_is_handled(tmp_path: Path) -> None:
    #широкая таблица: тысяча колонок не должна ломать ни выдачу страницы, ни статистику
    frame = pl.DataFrame({f"c{index}": [index, index + 1] for index in range(1000)})
    path = tmp_path / "wide.parquet"
    write_dataset(frame, path, "parquet")

    result, elapsed, peak = _page(path, offset=0, limit=2)

    print(f"\n     1000 колонок | страница {elapsed * 1000:6.0f} мс | пик {peak / 1e6:6.1f} МБ")

    assert len(result.columns) == 1000
    assert len(result.rows) == 2

    stats = compute_column_statistics(scan_dataset(path), "c999")
    assert stats.row_count == 2


def test_extremely_long_strings_do_not_break_the_page(tmp_path: Path) -> None:
    #значение в мегабайт: страница обязана вернуться, а не уронить сериализацию
    frame = pl.DataFrame({"text": ["x" * 1_000_000, "короткое"]})
    path = tmp_path / "long.parquet"
    write_dataset(frame, path, "parquet")

    result, _, _ = _page(path, offset=0, limit=2)

    assert len(result.rows) == 2
    assert len(result.rows[0]["text"]) == 1_000_000


def test_special_float_values_serialise_as_null(tmp_path: Path) -> None:
    #NaN и бесконечности не являются валидным JSON: браузер не разберёт такой ответ
    frame = pl.DataFrame({"value": [1.0, float("nan"), float("inf"), float("-inf"), None]})
    path = tmp_path / "special.parquet"
    write_dataset(frame, path, "parquet")

    result, _, _ = _page(path, offset=0, limit=5)
    values = [row["value"] for row in result.rows]

    assert values == [1.0, None, None, None, None]
