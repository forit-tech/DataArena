"""Producer-проверка: вся матрица фикстур §H одним запуском.

Это условие J-4 критерия freeze со стороны владельца контракта. Ожидаемое поведение
каждой фикстуры задано здесь и обязано совпадать с таблицей в README.md.

    python run_matrix.py [каталог контракта]

Код возврата 0 — все фикстуры повели себя как обещано.

Фикстура, появившаяся в каталоге и не описанная в EXPECTED, показывается как FAIL:
иначе пополнение набора прошло бы мимо проверки незамеченным.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_package import validate_package  # noqa: E402

ACCEPTED = "ПРИНЯТ"

#ожидаемое поведение: либо ПРИНЯТ, либо код отказа
EXPECTED: dict[str, str] = {
    "golden/customers_golden.dapkg": ACCEPTED,
    "golden/customers_golden.dapkg.zip": ACCEPTED,
    "golden/negative/minimal.dapkg": ACCEPTED,
    "golden/negative/future-minor.dapkg": ACCEPTED,
    "golden/negative/sampled-profile.dapkg": ACCEPTED,
    "golden/negative/future-major.dapkg": "package_version_unsupported",
    "golden/negative/corrupted-part.dapkg": "package_integrity_failed",
    "golden/negative/fingerprint-mismatch.dapkg": "package_fingerprint_mismatch",
    "golden/negative/malformed-schema.dapkg": "package_part_invalid",
    "golden/negative/missing-artifact.dapkg": "package_part_missing",
    "golden/negative/duplicate-entry.dapkg": "package_duplicate_entry",
    "golden/negative/schema-mismatch.dapkg": "package_schema_mismatch",
    "golden/negative/traversal.dapkg.zip": "package_unsafe_path",
    "golden/negative/zip-bomb.dapkg.zip": "package_limit_exceeded",
}


def discover(root: Path) -> list[str]:
    #эта функция находит все фикстуры на диске, а не только описанные в EXPECTED
    found = []

    for path in sorted(root.glob("golden/*.dapkg")) + sorted(root.glob("golden/*.dapkg.zip")):
        found.append(path.relative_to(root).as_posix())

    for path in sorted(root.glob("golden/negative/*")):
        if path.name.endswith(".dapkg") or path.name.endswith(".dapkg.zip"):
            found.append(path.relative_to(root).as_posix())

    return found


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    failures = 0

    for relative in discover(root):
        expected = EXPECTED.get(relative)

        if expected is None:
            print(f"[FAIL] {relative:<48} фикстура не описана в матрице ожиданий")
            failures += 1
            continue

        report = validate_package(root / relative)
        actual = ACCEPTED if report.ok else report.errors[0][0]
        marker = "OK " if actual == expected else "FAIL"
        failures += actual != expected
        suffix = "" if actual == expected else f"  (ожидалось {expected})"
        print(f"[{marker}] {relative:<48} {actual}{suffix}")

    missing = set(EXPECTED) - set(discover(root))
    for relative in sorted(missing):
        print(f"[FAIL] {relative:<48} описана в матрице, но отсутствует в каталоге")
        failures += 1

    print()
    print("PRODUCER SUITE: PASS" if not failures else f"PRODUCER SUITE: FAIL, расхождений {failures}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
