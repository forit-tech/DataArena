# ruff: noqa: F811 - фикстуры pytest client и ws импортируются из соседнего модуля
"""CCR-001, сторона producer: история колонок попадает в пакет — та же, что в API.

Пакет сериализует ответ единого resolver'а, а не строит историю заново. Поэтому главная
проверка здесь — совпадение с API: имена, источники, события в том же порядке, чтения.
Остальное — то, что потребитель обязан получить без искажений: «неизвестно» остаётся
неизвестным, усечённое — неполным, а идентичность данных от истории не зависит.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.services import context
from tests.integration.test_lineage_release_gate import (  # noqa: F401 - фикстуры pytest
    build_family,
    client,
    lineage,
    load_validator,
    package_bytes,
    put_steps,
    upload,
    ws,
)


def package_parts(content: bytes) -> tuple[dict, dict]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return json.loads(archive.read("manifest.json")), json.loads(archive.read("lineage.json"))


def valid(content: bytes, into: Path) -> list:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        archive.extractall(into)

    return load_validator().validate_package(into).errors


def test_the_package_carries_the_same_column_history_as_the_api(client: TestClient, ws: str, tmp_path: Path) -> None:
    _, _, child = build_family(client, ws)
    api = lineage(client, ws, child)
    content = package_bytes(client, ws, child)
    manifest, document = package_parts(content)

    assert manifest["format_version"] == document["format_version"] == "1.1.0"
    assert valid(content, tmp_path / "p") == []
    assert [entry["name"] for entry in document["columns"]] == [column["name"] for column in api["columns"]]

    for packaged, answered in zip(document["columns"], api["columns"], strict=True):
        assert packaged["origin"] == answered["origin"]
        assert [ref["column"] for ref in packaged["origins"]] == [ref["column"] for ref in answered["origins"]]
        #порядок событий — тот же, что в ответе resolver; ничего не пересортировано
        assert [(event["dataset_id"], event["operation"], event["position"]) for event in packaged["events"]] == [
            (event["dataset_id"], event["operation"], event["position"]) for event in answered["events"]
        ]
        assert packaged["read_by"] == [f"n{position}" for position in answered["read_by"]]

    email = next(entry for entry in document["columns"] if entry["name"] == "email")
    assert [event.get("node") for event in email["events"]] == ["jl1", "jl2", "j", "n2"]
    assert email["origins"][0]["source_file"] == "customers.csv"
    assert document["provenance"] == {
        "status": "known",
        "origin": "join",
        "unknown_reason": None,
        "history": "complete",
        "warnings": [],
    }


def test_a_grandparent_event_is_described_without_a_node(client: TestClient, ws: str, tmp_path: Path) -> None:
    _, _, child = build_family(client, ws)
    extra = upload(client, ws, pl.DataFrame({"id": [1, 2], "note": ["a", "b"]}), "notes.parquet")
    grandchild = lineage(client, ws, child)["dataset_id"]
    from tests.integration.test_lineage_release_gate import join

    built = join(client, ws, grandchild, extra, name="внук")
    content = package_bytes(client, ws, built)
    _, document = package_parts(content)
    email = next(entry for entry in document["columns"] if entry["name"] == "email")

    assert valid(content, tmp_path / "p") == []
    #шаги прародителя (customers) — без узлов: их нет в графе этого пакета, но событие цело
    assert [(event["dataset_name"], event["operation"], "node" in event) for event in email["events"]] == [
        ("customers.csv", "trim_whitespace", False),
        ("customers.csv", "fill_missing", False),
        ("customer_orders.parquet", "join", False),
        ("customer_orders.parquet", "change_case", True),
        ("внук.parquet", "join", True),
    ]


def test_unknown_stays_unknown_in_the_package(client: TestClient, ws: str, tmp_path: Path) -> None:
    dataset_id = upload(client, ws, pl.DataFrame({"a": [1, 2], "b": ["x", "y"]}), "т.parquet")
    store = context.get_workspace_store()

    with store._connect() as connection:
        connection.execute("DELETE FROM dataset_provenance WHERE dataset_id = ?", (dataset_id,))
        connection.commit()

    content = package_bytes(client, ws, dataset_id)
    _, document = package_parts(content)

    assert valid(content, tmp_path / "p") == []
    assert document["provenance"]["status"] == "unknown"
    assert document["provenance"]["origin"] is None
    assert document["provenance"]["history"] == "incomplete"
    assert all(entry["origin"] == "unknown" and entry["origins"] == [] for entry in document["columns"])


def test_truncated_parent_history_is_marked_incomplete(client: TestClient, ws: str, tmp_path: Path) -> None:
    _, _, child = build_family(client, ws)
    store = context.get_workspace_store()

    with store._connect() as connection:
        document = json.loads(
            connection.execute("SELECT document FROM dataset_provenance WHERE dataset_id = ?", (child,)).fetchone()[0]
        )
        right = document["join"]["parents"][1]
        right["history_truncated"] = True
        right["columns"] = [dict(column, events=[], read_by=[]) for column in right["columns"]]
        connection.execute(
            "UPDATE dataset_provenance SET document = ? WHERE dataset_id = ?",
            (json.dumps(document, ensure_ascii=False), child),
        )
        connection.commit()

    content = package_bytes(client, ws, child)
    _, packaged = package_parts(content)
    by_name = {entry["name"]: entry for entry in packaged["columns"]}

    assert valid(content, tmp_path / "p") == []
    assert packaged["provenance"]["history"] == "incomplete"
    assert "parent_history_truncated" in packaged["provenance"]["warnings"]
    #правая сторона усечена, левая — нет
    assert by_name["amount"]["history"] == "incomplete"
    assert by_name["email"]["history"] == "complete"


def test_data_identity_does_not_depend_on_history(client: TestClient, ws: str) -> None:
    """content_fingerprint и package_id описывают данные (§D); история защищена sha256 своей части."""
    dataset_id = upload(client, ws, pl.DataFrame({"id": [1, 2, 3], "v": ["a", "b", "c"]}), "id.parquet")
    before_manifest, before = package_parts(package_bytes(client, ws, dataset_id))
    #сортировка по уже отсортированному ключу: данные те же, история — другая
    put_steps(client, ws, dataset_id, [{"kind": "sort_rows", "params": {"column": "id"}}])
    after_manifest, after = package_parts(package_bytes(client, ws, dataset_id))

    lineage_sha = {
        name: next(part["sha256"] for part in manifest["parts"] if part["path"] == "lineage.json")
        for name, manifest in (("before", before_manifest), ("after", after_manifest))
    }

    assert before_manifest["data"]["content_fingerprint"] == after_manifest["data"]["content_fingerprint"]
    assert before_manifest["package_id"] == after_manifest["package_id"]
    assert before["columns"] != after["columns"]
    assert lineage_sha["before"] != lineage_sha["after"]


def test_history_beyond_the_contract_limits_is_omitted_and_said_so(
    client: TestClient, ws: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #история, не помещающаяся в пределы схемы, не пишется: пакет остаётся валидным,
    #а пропуск назван. Предел событий занижен, чтобы не строить десять тысяч шагов
    from backend.services.package import builder

    monkeypatch.setattr(builder, "MAX_EVENTS", 0)
    dataset_id = upload(client, ws, pl.DataFrame({"a": [" x", "y"], "b": [3, 4]}), "limits.parquet")
    put_steps(client, ws, dataset_id, [{"kind": "trim_whitespace", "params": {"columns": ["a"]}}])
    content = package_bytes(client, ws, dataset_id)
    _, document = package_parts(content)

    assert "columns" not in document
    assert "column_history_omitted" in document["provenance"]["warnings"]
    assert document["provenance"]["history"] == "incomplete"
    assert valid(content, tmp_path / "p") == []


def test_a_column_name_beyond_the_contract_limit_is_refused_not_packaged_invalid(
    client: TestClient, ws: str
) -> None:
    """Имя длиннее 300 символов не проходит schema.json контракта: прежде такой пакет
    собирался и отвергался бы потребителем, теперь — понятный отказ до сборки."""
    dataset_id = upload(client, ws, pl.DataFrame({"к" * 301: [1, 2], "b": [3, 4]}), "long.parquet")
    response = client.get(f"/api/workspaces/{ws}/datasets/{dataset_id}/package")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "package_failed"


def test_a_long_step_description_is_clipped_not_fatal(client: TestClient, ws: str, tmp_path: Path) -> None:
    dataset_id = upload(client, ws, pl.DataFrame({"s": ["x", "y"]}), "s.parquet")
    put_steps(client, ws, dataset_id, [{"kind": "replace_values", "params": {"column": "s", "from": "x", "to": "я" * 20_000}}])
    content = package_bytes(client, ws, dataset_id)
    _, document = package_parts(content)

    assert valid(content, tmp_path / "p") == []
    assert len(document["columns"][0]["events"][0]["description"]) == 10_000
