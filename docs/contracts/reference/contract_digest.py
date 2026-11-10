"""Каталог контракта не расходится молча: ни внутри репозитория, ни между репозиториями.

Каталог контракта лежит в двух репозиториях (DataArena — `docs/contracts`, ModelArena —
`contracts/dataset-package/v1`) и обязан быть побайтово одинаковым. Проверка golden package
по sha256 его частей этого не гарантирует: схемы, валидатор, фикстуры и спецификация
могли разойтись, а golden остаться прежним.

Файл `SHA256SUMS` в корне каталога перечисляет **каждый** файл контракта с его sha256,
в порядке путей. Отпечаток каталога — sha256 самого `SHA256SUMS`.

    python contract_digest.py [каталог]                  # каталог совпадает с SHA256SUMS
    python contract_digest.py --write [каталог]          # пересобрать SHA256SUMS после правки
    python contract_digest.py --compare КАТАЛОГ КАТАЛОГ  # две копии контракта побайтово равны

Код возврата 0 — совпало, 1 — расхождение (с перечнем файлов). Только стандартная
библиотека: скрипт запускается в CI до установки зависимостей.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

SUMS_FILE = "SHA256SUMS"
#то, что создаёт запуск эталонных скриптов, а не автор контракта
IGNORED_PARTS = frozenset({"__pycache__", ".pytest_cache"})
IGNORED_SUFFIXES = frozenset({".pyc"})


def contract_files(root: Path) -> list[str]:
    files = []

    for path in root.rglob("*"):
        relative = path.relative_to(root)

        if not path.is_file() or relative.as_posix() == SUMS_FILE:
            continue

        if IGNORED_PARTS.intersection(relative.parts) or path.suffix in IGNORED_SUFFIXES:
            continue

        files.append(relative.as_posix())

    #порядок — по байтам пути в UTF-8: не зависит ни от локали, ни от файловой системы
    return sorted(files, key=lambda name: name.encode("utf-8"))


def listing(root: Path) -> bytes:
    lines = [f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n" for name in contract_files(root)]
    return "".join(lines).encode("utf-8")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parse(content: bytes) -> dict[str, str]:
    entries = {}

    for line in content.decode("utf-8").splitlines():
        checksum, _, name = line.partition("  ")
        entries[name] = checksum

    return entries


def differences(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    report = [f"изменён: {name}" for name in sorted(expected.keys() & actual.keys()) if expected[name] != actual[name]]
    report += [f"отсутствует: {name}" for name in sorted(expected.keys() - actual.keys())]
    report += [f"лишний: {name}" for name in sorted(actual.keys() - expected.keys())]
    return report


def check(root: Path) -> int:
    sums = root / SUMS_FILE

    if not sums.exists():
        print(f"{sums}: нет файла. Соберите его: python contract_digest.py --write {root}")
        return 1

    recorded = sums.read_bytes()
    actual = listing(root)

    if recorded == actual:
        print(f"контракт {root}: {len(parse(actual))} файлов, отпечаток {digest(actual)}")
        return 0

    report = differences(parse(recorded), parse(actual)) or ["SHA256SUMS записан не в каноническом виде"]
    print(f"контракт {root} не совпадает с {SUMS_FILE}:")
    print("\n".join(f"  {line}" for line in report))
    print("Правка контракта — версионированное событие: пересоберите SHA256SUMS и перенесите каталог в оба репозитория.")
    return 1


def compare(first: Path, second: Path) -> int:
    left, right = listing(first), listing(second)

    if left == right:
        print(f"копии контракта совпадают: {len(parse(left))} файлов, отпечаток {digest(left)}")
        return 0

    print(f"копии контракта разошлись ({first} → {second}):")
    print("\n".join(f"  {line}" for line in differences(parse(left), parse(right))))
    return 1


def main(arguments: list[str]) -> int:
    default = Path(__file__).resolve().parents[1]

    if arguments[:1] == ["--compare"] and len(arguments) == 3:
        return compare(Path(arguments[1]).resolve(), Path(arguments[2]).resolve())

    if arguments[:1] == ["--write"]:
        root = Path(arguments[1]).resolve() if len(arguments) > 1 else default
        content = listing(root)
        (root / SUMS_FILE).write_bytes(content)
        print(f"{root / SUMS_FILE}: {len(parse(content))} файлов, отпечаток {digest(content)}")
        return 0

    return check(Path(arguments[0]).resolve() if arguments else default)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
