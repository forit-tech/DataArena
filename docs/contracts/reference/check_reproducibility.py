"""Проверка J-6: отпечаток воспроизводится независимо от версии библиотек.

Скрипт читает golden package и пересчитывает `content_fingerprint` по данным.
Запускается в CI обеих сторон **минимум на двух версиях Polars**: утверждение
«побайтово воспроизводимо» без такой проверки остаётся утверждением, а не фактом.

    python check_reproducibility.py [путь к .dapkg]

Код возврата 0 — отпечаток совпал, 1 — расхождение.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fingerprint import ALGORITHM, content_fingerprint  # noqa: E402


def main() -> None:
    package = (
        Path(sys.argv[1]).resolve()
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1] / "golden" / "customers_golden.dapkg"
    )
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    fingerprint = manifest["data"].get("content_fingerprint")

    if not fingerprint:
        print("В манифесте нет content_fingerprint — проверять нечего.")
        sys.exit(0)

    if fingerprint["algorithm"] != ALGORITHM:
        print(f"Алгоритм пакета {fingerprint['algorithm']} не поддерживается этой реализацией ({ALGORITHM}).")
        sys.exit(1)

    table = pl.concat(
        [pl.read_parquet(package / relative) for relative in manifest["data"]["files"]],
        how="vertical",
    )
    actual = content_fingerprint(table)

    print(f"polars {pl.__version__}, алгоритм {ALGORITHM}")
    print(f"  в манифесте: {fingerprint['value']}")
    print(f"  пересчитан:  {actual}")
    print("  СОВПАЛ" if actual == fingerprint["value"] else "  РАСХОЖДЕНИЕ")
    sys.exit(0 if actual == fingerprint["value"] else 1)


if __name__ == "__main__":
    main()
