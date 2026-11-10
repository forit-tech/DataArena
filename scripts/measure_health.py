"""Измерение стоимости диагностики.

Печатает время и пик памяти на разных размерах. Числа отсюда попадают в отчёт: без них
утверждение о расходе памяти было бы обещанием, а не фактом.

**Чем мерить.** Здесь мерится пик рабочего набора процесса, а не `tracemalloc`. Это не
придирка: `tracemalloc` видит только кучу Python, а данные Polars живут в буферах Arrow
вне её. Прежняя версия этого скрипта печатала «пик 0,0 МБ» на миллионе строк, и это
число попало в проект как доказательство, что память не зависит от числа строк. При
измерении рабочего набора видно обратное: на 0,1 / 1 / 4 млн строк расчёт добавляет
примерно 91 / 499 / 1592 МБ — рост линейный по строкам. Прибор показывал не свойство
кода, а собственную слепоту.

**Почему по процессу на размер.** Аллокатор не возвращает память системе сразу, поэтому
второй замер в том же процессе наследует пик первого и показывает завышенное число.
По той же причине датасет готовится отдельным процессом: построение таблицы на четыре
миллиона строк само занимает под гигабайт, и в общем процессе этот пик засчитался бы
расчёту, которого он не касается.

Запуск: python scripts/measure_health.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import polars as pl
import psutil

#запуск по пути кладёт в начало пути каталог скрипта, а не корень проекта
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.health.checks import analyse

CASES: dict[str, tuple[int, int]] = {
    "сто тысяч": (100_000, 6),
    "миллион": (1_000_000, 6),
    "четыре миллиона": (4_000_000, 6),
    "широкий": (20_000, 500),
    "очень широкий": (5_000, 1_000),
}


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


def peak_working_set_mb(process: psutil.Process) -> float:
    info = process.memory_info()
    #peak_wset есть только на Windows; на остальных системах берётся текущий rss,
    #и это честнее, чем печатать ноль
    return float(getattr(info, "peak_wset", info.rss)) / 1024 / 1024


def generate_case(label: str, path: Path) -> None:
    """Готовит датасет. Отдельный процесс: его пик не должен попасть в замер."""
    rows, columns = CASES[label]
    messy(rows, columns).write_parquet(path)


def measure_case(label: str, path: Path) -> None:
    """Один замер в чистом процессе: он только читает готовый файл и считает."""
    rows, columns = CASES[label]
    process = psutil.Process()
    size = path.stat().st_size / 1024 / 1024

    lazy = pl.scan_parquet(path)
    schema = lazy.collect_schema()
    before = peak_working_set_mb(process)

    started = time.perf_counter()
    total_rows = int(lazy.select(pl.len()).collect().item())
    findings = analyse(lazy, schema, total_rows)
    elapsed = time.perf_counter() - started
    peak = peak_working_set_mb(process)

    print(
        f"  {label:18} {rows:>9} строк × {columns:>5} кол.  файл {size:6.1f} МБ  "
        f"{elapsed:6.2f} с  пик {peak:7.1f} МБ  расчёт добавил {peak - before:7.1f} МБ  "
        f"находок {len(findings)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Стоимость диагностики")
    parser.add_argument("--case", choices=list(CASES), help="внутренний вызов")
    parser.add_argument("--generate", choices=list(CASES), help="внутренний вызов")
    parser.add_argument("--path", type=Path, help="внутренний вызов")
    arguments = parser.parse_args()

    if arguments.generate is not None and arguments.path is not None:
        generate_case(arguments.generate, arguments.path)
        return 0

    if arguments.case is not None and arguments.path is not None:
        measure_case(arguments.case, arguments.path)
        return 0

    print("Стоимость диагностики (пик рабочего набора процесса, по процессу на замер)")
    directory = Path(tempfile.mkdtemp(prefix="dataarena_health_"))
    here = str(Path(__file__).resolve())

    try:
        for label in CASES:
            path = directory / f"{label}.parquet"

            for stage in ("--generate", "--case"):
                completed = subprocess.run(  # noqa: S603 - запуск самого себя известным путём
                    [sys.executable, "-X", "utf8", here, stage, label, "--path", str(path)],
                    check=False,
                )

                if completed.returncode != 0:
                    print(f"  {label}: замер не выполнен (код {completed.returncode})")
                    return 1

            path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
