"""Контракт Dataset Package заморожен и обязан оставаться проверяемым.

Эти тесты не проверяют формат заново — для этого есть `docs/contracts/reference/run_matrix.py`.
Они стерегут другое: чтобы версия, которую объявляет API, не разошлась со спецификацией,
и чтобы каталог контракта случайно не изменили без пересборки фикстур.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.api.routes.system import FINGERPRINT_ALGORITHM, PACKAGE_FORMAT, PACKAGE_VERSION

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "docs" / "contracts"
GOLDEN_PACKAGE = CONTRACT_ROOT / "golden" / "customers_golden.dapkg"


@pytest.fixture(scope="module")
def golden_manifest() -> dict:
    return json.loads((GOLDEN_PACKAGE / "manifest.json").read_text(encoding="utf-8"))


def test_api_announces_the_same_version_as_the_golden_package(golden_manifest: dict) -> None:
    #самая дешёвая рассинхронизация: спецификацию обновили, а константу в API забыли
    assert golden_manifest["format"] == PACKAGE_FORMAT
    assert golden_manifest["format_version"] == PACKAGE_VERSION
    assert golden_manifest["data"]["content_fingerprint"]["algorithm"] == FINGERPRINT_ALGORITHM


def test_golden_package_files_match_their_declared_hashes(golden_manifest: dict) -> None:
    #каталог контракта заморожен: правка файла без пересборки фикстур обязана быть замечена
    mismatched = []

    for entry in golden_manifest["parts"]:
        path = GOLDEN_PACKAGE / entry["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        if digest != entry["sha256"]:
            mismatched.append(entry["path"])

    assert not mismatched, (
        "Файлы golden package не соответствуют манифесту: "
        f"{mismatched}. Пересоберите фикстуры и проверьте версию по §E."
    )


def test_contract_declares_the_frozen_status() -> None:
    specification = (CONTRACT_ROOT / "dataset-package-v1.md").read_text(encoding="utf-8")

    assert "**FROZEN**" in specification
    assert PACKAGE_VERSION in specification


@pytest.mark.parametrize(
    "name",
    ["manifest.schema.json", "schema.schema.json", "profile.schema.json",
     "lineage.schema.json", "recipe.schema.json"],
)
def test_every_json_schema_is_valid_json(name: str) -> None:
    payload = json.loads((CONTRACT_ROOT / "schema" / name).read_text(encoding="utf-8"))

    assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert payload["$id"].endswith(name)
