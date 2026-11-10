"""Каталог контракта не расходится молча: `SHA256SUMS` и сравнение копий.

Скрипт запускается так же, как в CI, — отдельным процессом. Случаи расхождения
проверяются на копии каталога: правка без пересборки, лишний файл, пропавший файл,
две копии, разошедшиеся в одном байте.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "docs" / "contracts"
SCRIPT = CONTRACT_ROOT / "reference" / "contract_digest.py"


def run(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    #вывод по-русски: без явной кодировки Windows отдала бы его в cp1251
    return subprocess.run(  # noqa: S603 - свой скрипт контракта, аргументы — пути теста
        [sys.executable, str(SCRIPT), *map(str, arguments)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    target = tmp_path / "contracts"
    shutil.copytree(CONTRACT_ROOT, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def test_contract_directory_matches_its_checksums() -> None:
    result = run(CONTRACT_ROOT)

    assert result.returncode == 0, result.stdout


def test_edit_without_regenerating_checksums_fails(copy: Path) -> None:
    schema = copy / "schema" / "lineage.schema.json"
    schema.write_bytes(schema.read_bytes() + b" ")

    result = run(copy)

    assert result.returncode == 1
    assert "изменён: schema/lineage.schema.json" in result.stdout


def test_extra_and_missing_files_fail(copy: Path) -> None:
    (copy / "schema" / "extra.schema.json").write_bytes(b"{}")
    (copy / "reference" / "validate_package.py").unlink()

    result = run(copy)

    assert result.returncode == 1
    assert "лишний: schema/extra.schema.json" in result.stdout
    assert "отсутствует: reference/validate_package.py" in result.stdout


def test_regenerated_checksums_accept_the_edit(copy: Path) -> None:
    (copy / "README.md").write_bytes(b"changed\n")

    assert run("--write", copy).returncode == 0
    assert run(copy).returncode == 0


def test_copies_differing_in_one_fixture_byte_fail(copy: Path) -> None:
    assert run("--compare", CONTRACT_ROOT, copy).returncode == 0

    fixture = copy / "golden" / "lineage" / "join-lineage.dapkg" / "lineage.json"
    fixture.write_bytes(fixture.read_bytes().replace(b"join", b"JOIN", 1))
    #пересобранный SHA256SUMS не спасает: копия всё равно разошлась с оригиналом
    run("--write", copy)

    result = run("--compare", CONTRACT_ROOT, copy)

    assert result.returncode == 1
    assert "изменён: golden/lineage/join-lineage.dapkg/lineage.json" in result.stdout
