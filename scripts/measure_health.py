"""Измерение стоимости диагностики.

Печатает время и пик памяти на разных размерах. Числа отсюда попадают в отчёт: без них
утверждение «расход памяти ограничен» было бы обещанием, а не фактом.

Запуск: python scripts/measure_health.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import polars as pl

#запуск по пути кладёт в начало пути каталог скрипта, а не корень проекта
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.health.checks import analyse


def messy(rows: int, columns: int) -> pl.DataFrame:
    """Датасет, на котором срабатывает как можно больше проверок."""
    data: dict[str, list] = {
        "id": list(range(rows)),
        "город": [["Москва", "москва", "Казань ", None, "Омск"][n % 5] for n in range(rows)],
        "сумма": [None if n % 20 == 0 else float(n % 50) for n in range(rows)],
        "пусто": [None] * rows,
        "одно": ["одинаково"] * rows,
        "дробь": [float("nan") if n % 50 == 0 else float(n) for n in range(rows)],
    }

    for index in range(len(data), columns):
        #остальные колонки — обычные числовые, чтобы измерять именно ширину
        data[f"c{index}"] = [float(n % 97) for n in range(rows)]

    return pl.DataFrame(data)


def measure(label: str, rows: int, columns: int, directory: Path) -> None:
    path = directory / f"{label}.parquet"
    messy(rows, columns).write_parquet(path)
    size = path.stat().st_size / 1024 / 1024

    lazy = pl.scan_parquet(path)
    schema = lazy.collect_schema()

    tracemalloc.start()
    started = time.perf_counter()
    total_rows = int(lazy.select(pl.len()).collect().item())
    findings = analyse(lazy, schema, total_rows)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(
        f"  {label:22} {rows:>9} строк × {columns:>5} кол.  файл {size:6.1f} МБ  "
        f"{elapsed:6.2f} с  пик {peak / 1024 / 1024:6.1f} МБ  находок {len(findings)}"
    )


def main() -> int:
    directory = Path(tempfile.mkdtemp(prefix="dataarena_health_"))

    try:
        print("Стоимость диагностики")
        measure("сто тысяч", 100_000, 6, directory)
        measure("миллион", 1_000_000, 6, directory)
        measure("широкий", 20_000, 500, directory)
        measure("очень широкий", 5_000, 1_000, directory)
        return 0
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
