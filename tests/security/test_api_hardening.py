"""Враждебные сценарии для HTTP-слоя.

Три группы: TOCTOU (состояние изменилось между проверкой и использованием),
особенности Windows (проект разрабатывается на Windows, а CI идёт на Linux —
приколы файловой системы Windows не должны всплывать только у пользователя)
и злоупотребление параметрами запроса.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context
from backend.services.upload import MAX_DISPLAY_NAME_LENGTH, safe_display_name
from backend.services.workspace_access import reading_path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()
    yield TestClient(create_app())
    config.get_settings.cache_clear()
    context.reset_context()


@pytest.fixture
def workspace_id(client: TestClient) -> str:
    return client.post("/api/workspaces", json={"name": "ws"}).json()["workspace_id"]


def upload_csv(client: TestClient, workspace_id: str, name: str = "data.csv") -> dict:
    frame = pl.DataFrame({"a": range(50), "b": ["x", "y"] * 25})
    buffer = io.BytesIO(frame.write_csv().encode("utf-8"))
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── TOCTOU ────────────────────────────────────────────────────────────────────


def test_dataset_deleted_between_check_and_read_reports_not_found(
    client: TestClient, workspace_id: str, tmp_path: Path
) -> None:
    #запись есть, файла нет: это «датасета больше нет», а не внутренняя ошибка сервера
    dataset = upload_csv(client, workspace_id)
    store = context.get_workspace_store()
    stored = store.get_dataset(dataset["dataset_id"])

    reading_path(context.get_workspace_root(), stored).unlink()

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "dataset_not_found"
    assert "Traceback" not in response.text


def test_workspace_deleted_while_its_dataset_is_being_read(
    client: TestClient, workspace_id: str
) -> None:
    dataset = upload_csv(client, workspace_id)
    client.delete(f"/api/workspaces/{workspace_id}")

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"
    )

    assert response.status_code == 404


def test_dataset_of_another_workspace_looks_exactly_like_a_missing_one(
    client: TestClient, workspace_id: str
) -> None:
    #иначе перебор идентификаторов сообщал бы о существовании чужих данных
    dataset = upload_csv(client, workspace_id)
    other = client.post("/api/workspaces", json={"name": "чужой"}).json()["workspace_id"]

    response = client.get(f"/api/workspaces/{other}/datasets/{dataset['dataset_id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "dataset_not_found"


def test_repeated_rapid_requests_stay_consistent(client: TestClient, workspace_id: str) -> None:
    #двойной клик и быстрые повторы не должны давать разные ответы на один запрос
    dataset = upload_csv(client, workspace_id)
    url = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?limit=10&sort=a:asc"
    results: list[str] = []

    def fetch() -> None:
        results.append(client.get(url).text)

    threads = [threading.Thread(target=fetch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    #ответы отличаются только идентификатором запроса, поэтому сравниваются строки
    bodies = {result.split('"request_id"')[0] for result in results}
    assert len(bodies) == 1, "одинаковые запросы дали разные ответы"


# ── особенности Windows ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostile_name",
    [
        "CON.csv",           # зарезервированное имя устройства
        "PRN.csv",
        "AUX.csv",
        "NUL.csv",
        "COM1.csv",
        "data.csv.",         # точка в конце — Windows её отбрасывает
        "data.csv ",         # пробел в конце — то же самое
        "data.csv:stream",   # альтернативный поток данных NTFS
        "DATA.CSV",          # регистр: файловая система нечувствительна к нему
        "данные.csv",        # не-ASCII имя
        "e\u0301.csv",       # комбинирующий акцент: другая форма нормализации Unicode
        "a" * 400 + ".csv",  # имя за пределом длины
    ],
)
def test_hostile_windows_filenames_are_accepted_without_touching_the_filesystem(
    client: TestClient, workspace_id: str, hostile_name: str
) -> None:
    #имя пользователя используется ТОЛЬКО для показа: путь собирается из идентификатора
    #датасета и расширения распознанного формата, поэтому ни одно из этих имён
    #не должно ни сломать загрузку, ни повлиять на путь
    dataset = upload_csv(client, workspace_id, hostile_name)
    stored = context.get_workspace_store().get_dataset(dataset["dataset_id"])
    path = reading_path(context.get_workspace_root(), stored)

    assert path.exists()
    assert path.name == f"{dataset['dataset_id']}.csv"
    assert ":" not in path.name
    assert not path.name.endswith((" ", "."))
    assert len(path.name) < 100


@pytest.mark.parametrize(
    "raw,expected_missing",
    [
        ("../../etc/passwd", ".."),
        ("..\\..\\windows\\system32\\cmd.exe", ".."),
        ("C:\\Windows\\win.ini", "\\"),
        ("/etc/shadow", "/"),
    ],
)
def test_display_name_never_keeps_directory_parts(raw: str, expected_missing: str) -> None:
    name = safe_display_name(raw)

    assert expected_missing not in name
    assert Path(name).name == name


def test_display_name_drops_control_characters() -> None:
    #управляющие символы ломают вывод в терминале и в логе, а NUL обрывает строку в C-слоях
    name = safe_display_name("da\x00ta\x1b[31m.csv")

    assert "\x00" not in name
    assert "\x1b" not in name


def test_display_name_is_length_limited() -> None:
    name = safe_display_name("x" * 1000 + ".csv")

    assert len(name) <= MAX_DISPLAY_NAME_LENGTH
    assert name.endswith(".csv"), "расширение обязано пережить обрезку"


def test_two_files_with_case_different_names_are_separate_datasets(
    client: TestClient, workspace_id: str
) -> None:
    #файловая система Windows нечувствительна к регистру, но датасеты различаются
    #по содержимому, а не по имени: два разных файла обязаны стать двумя датасетами
    first = pl.DataFrame({"a": [1]})
    second = pl.DataFrame({"a": [2]})

    for frame, name in ((first, "Data.csv"), (second, "DATA.CSV")):
        buffer = io.BytesIO(frame.write_csv().encode("utf-8"))
        response = client.post(
            f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
        )
        assert response.status_code == 201

    assert len(client.get(f"/api/workspaces/{workspace_id}/datasets").json()["datasets"]) == 2


# ── злоупотребление параметрами ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "?sort=несуществующая:asc",
        "?sort=a:вбок",
        "?filter=несуществующая:eq:1",
        "?filter=a:взорвись:1",
        "?filter=a:eq:" + "x" * 1000,
        "?search=" + "y" * 1000,
        "?offset=-5",
        "?limit=0",
    ],
)
def test_malformed_table_parameters_are_rejected_cleanly(
    client: TestClient, workspace_id: str, query: str
) -> None:
    dataset = upload_csv(client, workspace_id)
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows{query}"
    )

    assert response.status_code == 422
    assert "Traceback" not in response.text
    assert response.json()["error"]["code"]


def test_filter_value_of_wrong_type_says_so_instead_of_finding_nothing(
    client: TestClient, workspace_id: str
) -> None:
    #молчаливый пустой результат выглядит как «данных нет», хотя запрос просто некорректен
    dataset = upload_csv(client, workspace_id)
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows?filter=a:gt:слово"
    )

    assert response.status_code == 422
    assert "не подходит" in response.json()["error"]["message"]


def test_unknown_column_statistics_is_rejected(client: TestClient, workspace_id: str) -> None:
    dataset = upload_csv(client, workspace_id)
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/columns/неттакой"
    )

    assert response.status_code == 422


def test_malformed_identifier_never_reaches_the_filesystem(client: TestClient) -> None:
    response = client.get("/api/workspaces/..%2F..%2Fetc/datasets")

    assert response.status_code in (404, 422)
    assert "Traceback" not in response.text


# ── загрузка ──────────────────────────────────────────────────────────────────


def test_empty_file_is_rejected(client: TestClient, workspace_id: str) -> None:
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets",
        files={"file": ("empty.csv", io.BytesIO(b""))},
    )

    assert response.status_code == 422
    assert "пуст" in response.json()["error"]["message"]


def test_oversized_upload_is_rejected_during_the_stream(
    client: TestClient, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    #лимит проверяется во время передачи: файл не должен сначала оказаться на диске
    monkeypatch.setenv("DATAARENA_MAX_UPLOAD_MB", "1")
    config.get_settings.cache_clear()

    payload = io.BytesIO(b"a,b\n" + b"1,2\n" * 400_000)
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": ("big.csv", payload)}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_staging_is_empty_after_a_rejected_upload(
    client: TestClient, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    #прерванная и отвергнутая загрузка не должна оставлять мусор
    monkeypatch.setenv("DATAARENA_MAX_UPLOAD_MB", "1")
    config.get_settings.cache_clear()

    payload = io.BytesIO(b"a,b\n" + b"1,2\n" * 400_000)
    client.post(f"/api/workspaces/{workspace_id}/datasets", files={"file": ("big.csv", payload)})

    staging = context.get_workspace_root() / workspace_id / "staging"
    leftovers = list(staging.glob("*")) if staging.exists() else []

    assert not leftovers, f"в staging осталось: {leftovers}"


def test_upload_to_a_missing_workspace_is_refused_before_reading_the_file(
    client: TestClient,
) -> None:
    #незачем принимать сотни мегабайт, чтобы затем сообщить, что складывать их некуда
    response = client.post(
        "/api/workspaces/ws_0123456789abcdef/datasets",
        files={"file": ("data.csv", io.BytesIO(b"a\n1\n"))},
    )

    assert response.status_code == 404


def test_binary_garbage_named_csv_is_refused_by_the_api(
    client: TestClient, workspace_id: str
) -> None:
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets",
        files={"file": ("garbage.csv", io.BytesIO(bytes(range(256)) * 20))},
    )

    assert response.status_code == 400
    assert "двоичные" in response.json()["error"]["message"]


def test_unsupported_extension_is_refused(client: TestClient, workspace_id: str) -> None:
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets",
        files={"file": ("script.exe", io.BytesIO(b"MZ\x90\x00"))},
    )

    assert response.status_code == 415


def test_ntfs_stream_suffix_is_stripped_not_rejected() -> None:
    #«data.csv:stream» описывает альтернативный поток данных, а не другой формат:
    #сам файл остаётся CSV, и отвергать его как «неподдерживаемый формат» было бы неправдой
    assert safe_display_name("data.csv:stream") == "data.csv"
    assert safe_display_name("отчёт.parquet:$DATA") == "отчёт.parquet"


def test_trailing_dots_and_spaces_are_normalised_like_windows_does() -> None:
    #Windows молча отбрасывает их, поэтому поведение не должно зависеть от того,
    #с какой системы пришёл клиент
    assert safe_display_name("data.csv.") == "data.csv"
    assert safe_display_name("data.csv   ") == "data.csv"
    assert safe_display_name("data.csv. . ") == "data.csv"


def test_colon_inside_the_stem_is_left_alone() -> None:
    #обрезается только хвост в расширении: имя целиком портить незачем
    assert safe_display_name("отчёт: итоги.csv") == "отчёт: итоги.csv"
