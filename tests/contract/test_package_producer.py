"""Пакет, собранный приложением, проверяется тем же валидатором, что и эталонный.

Это главный смысл файла. Контракт `dataarena.package/1` заморожен, и «мы собираем
по спецификации» — не утверждение, а проверка: пакет из живого датасета прогоняется
через `docs/contracts/reference/validate_package.py`, то есть через ту же программу,
которой пакет будет проверять потребитель.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context

REFERENCE = Path(__file__).resolve().parents[2] / "docs/contracts/reference"

FRAME = pl.DataFrame(
    {
        "client_id": [1, 2, 3, 4, 5, 6],
        "город": ["Москва ", "москва", "Казань", "Омск", "Уфа", "Москва "],
        "сумма": ["1 200,50", "300", "н/д", "40", "77", "1 200,50"],
        "активен": [True, False, True, True, False, True],
    }
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


@pytest.fixture
def dataset(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/workspaces", json={"name": "Пакет"}).json()["workspace_id"]
    buffer = io.BytesIO()
    FRAME.write_parquet(buffer)
    buffer.seek(0)
    dataset_id = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("клиенты.parquet", buffer)}
    ).json()["dataset_id"]

    return workspace_id, dataset_id


def load_validator():
    #валидатор лежит в документации как часть спецификации: импортируется он оттуда,
    #а не копируется — копия однажды отстала бы от контракта
    spec = importlib.util.spec_from_file_location(
        "reference_validator", REFERENCE / "validate_package.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["reference_validator"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    return module


def fetch_package(client: TestClient, workspace_id: str, dataset_id: str, into: Path) -> Path:
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/package"
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"

    into.mkdir(parents=True, exist_ok=True)
    archive = into / "package.dapkg"
    archive.write_bytes(response.content)

    return archive


def test_the_fingerprint_implementation_is_the_published_one() -> None:
    """Копия отпечатка обязана совпадать с эталоном байт в байт.

    Расхождение означало бы, что DataArena собирает пакеты не по тому контракту,
    который сама публикует, — и обнаружилось бы это у потребителя.
    """
    published = (REFERENCE / "fingerprint.py").read_bytes()
    used = (
        Path(__file__).resolve().parents[2] / "backend/adapters/package/fingerprint.py"
    ).read_bytes()

    assert used == published, "копия реализации отпечатка разошлась с эталоном"


def test_a_package_from_a_live_dataset_passes_the_reference_validator(
    client: TestClient, dataset: tuple[str, str], tmp_path: Path
) -> None:
    workspace_id, dataset_id = dataset
    #датасет проходит через настоящие шаги: в пакет должен попасть результат,
    #а не исходник
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {"step_id": "s1", "kind": "trim_whitespace", "params": {"columns": ["город"]}},
                {"step_id": "s2", "kind": "normalize_nulls", "params": {"columns": ["сумма"]}},
                {"step_id": "s3", "kind": "parse_number", "params": {"column": "сумма"}},
                {"step_id": "s4", "kind": "deduplicate", "params": {}},
            ]
        },
    )

    archive = fetch_package(client, workspace_id, dataset_id, tmp_path)
    unpacked = tmp_path / "unpacked"

    with zipfile.ZipFile(archive) as package:
        package.extractall(unpacked)

    validator = load_validator()
    report = validator.validate_package(unpacked)

    assert report.errors == [], report.errors
    assert report.warnings == [], report.warnings


def test_the_package_describes_the_result_and_its_origin(
    client: TestClient, dataset: tuple[str, str], tmp_path: Path
) -> None:
    """Происхождение — не украшение: по пакету видно, из чего и как получились данные."""
    workspace_id, dataset_id = dataset
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                #дубликат здесь — по городу и сумме: client_id у строк разный,
                #и по всем колонкам они не повторяются
                {
                    "step_id": "s1",
                    "kind": "deduplicate",
                    "params": {"columns": ["город", "сумма"]},
                },
            ]
        },
    )

    archive = fetch_package(client, workspace_id, dataset_id, tmp_path)

    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("manifest.json"))
        lineage = json.loads(package.read("lineage.json"))
        recipe = json.loads(package.read("recipe.json"))
        data = pl.read_parquet(io.BytesIO(package.read(manifest["data"]["files"][0])))

    #дубликат убран шагом: в пакете пять строк, а не шесть
    assert manifest["data"]["row_count"] == 5
    assert data.height == 5
    assert manifest["format"] == "dataarena.package"
    assert manifest["format_version"] == "1.1.0"
    assert manifest["data"]["content_fingerprint"]["algorithm"] == "dataarena-logical-sha256-v1"

    #происхождение: исходный файл с его хэшем и по узлу на шаг
    assert lineage["sources"][0]["sha256"]
    assert [node["op"] for node in lineage["graph"]] == ["read", "deduplicate"]

    #рецепт повторяем: те же шаги и хэш исходника
    assert recipe["steps"] == [
        {
            "op": "deduplicate",
            "on": recipe["inputs"][0]["alias"],
            "params": {"columns": ["город", "сумма"]},
        }
    ]
    assert recipe["inputs"][0]["source_sha256"] == lineage["sources"][0]["sha256"]


def test_two_builds_of_the_same_dataset_give_the_same_package(
    client: TestClient, dataset: tuple[str, str], tmp_path: Path
) -> None:
    """Пакет детерминирован — иначе сверять пакет с пакетом было бы нельзя.

    Время сборки в манифесте отличаться может: это отметка о событии, а не содержимое.
    Всё остальное — данные, схема, профиль, рецепт, происхождение и отпечаток — обязано
    совпасть.
    """
    workspace_id, dataset_id = dataset
    first = fetch_package(client, workspace_id, dataset_id, tmp_path / "a")
    second = fetch_package(client, workspace_id, dataset_id, tmp_path / "b")

    def content(path: Path) -> dict[str, bytes]:
        with zipfile.ZipFile(path) as package:
            return {name: package.read(name) for name in sorted(package.namelist())}

    left, right = content(first), content(second)

    assert sorted(left) == sorted(right)

    for name in left:
        if name in {"manifest.json", "profile.json", "lineage.json"}:
            #в этих файлах есть отметка времени сборки; сверяются они без неё
            continue

        assert left[name] == right[name], f"{name} различается между сборками"

    left_manifest = json.loads(left["manifest.json"])
    right_manifest = json.loads(right["manifest.json"])

    assert (
        left_manifest["data"]["content_fingerprint"]
        == right_manifest["data"]["content_fingerprint"]
    )
    assert left_manifest["package_id"] == right_manifest["package_id"]


def test_an_empty_result_is_refused_instead_of_shipping_an_empty_package(
    client: TestClient, dataset: tuple[str, str]
) -> None:
    #пустой пакет пройдёт проверку схемы и окажется бесполезным у потребителя
    workspace_id, dataset_id = dataset
    client.put(
        f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/pipeline",
        json={
            "steps": [
                {
                    "step_id": "s1",
                    "kind": "filter_rows",
                    "params": {"column": "client_id", "operator": "gt", "value": "1000"},
                }
            ]
        },
    )

    response = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/package")

    assert response.status_code == 422
    assert "нет строк" in response.json()["error"]["message"]
