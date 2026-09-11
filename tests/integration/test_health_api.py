"""Вертикальный путь диагностики: API → находка → строки в той же таблице.

Главное, что здесь проверяется, — обещание «в таблице ровно те строки, о которых
говорит находка». Оно проверяется не на одной находке, а на каждой, какая нашлась,
и не сравнением чисел из разных источников, а прохождением того же пути, которым
пойдёт интерфейс.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core import config
from backend.services import context


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
    return client.post("/api/workspaces", json={"name": "Диагностика"}).json()["workspace_id"]


def upload(client: TestClient, workspace_id: str, frame: pl.DataFrame, name: str) -> dict:
    buffer = io.BytesIO()

    if name.endswith(".csv"):
        buffer.write(frame.write_csv().encode("utf-8"))
    else:
        frame.write_parquet(buffer)

    buffer.seek(0)
    response = client.post(
        f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}
    )
    assert response.status_code == 201, response.text
    return response.json()


def messy_frame(rows: int = 200) -> pl.DataFrame:
    """Датасет, в котором намеренно есть почти все виды отклонений."""
    return pl.DataFrame(
        {
            "id": list(range(rows)),
            #регистр, краевые пробелы и пропуски в одной колонке
            "город": [["Москва", "москва", "Казань ", None, "Омск"][n % 5] for n in range(rows)],
            #пропуски и одно очень большое значение
            "сумма": [
                None if n % 20 == 0 else (999_999.0 if n == 7 else float(n % 50))
                for n in range(rows)
            ],
            "пусто": [None] * rows,
            "одно": ["одинаково"] * rows,
            "почти": ["да"] * (rows - 2) + ["нет", "нет"],
            "дробь": [float("nan") if n % 50 == 0 else float(n) for n in range(rows)],
        }
    )


@pytest.fixture
def dataset(client: TestClient, workspace_id: str) -> dict:
    return upload(client, workspace_id, messy_frame(), "грязный.parquet")


def health_of(client: TestClient, workspace_id: str, dataset_id: str) -> dict:
    response = client.get(f"/api/workspaces/{workspace_id}/datasets/{dataset_id}/health")
    assert response.status_code == 200, response.text
    return response.json()


# ── отчёт ─────────────────────────────────────────────────────────────────────


def test_the_report_describes_what_is_wrong(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    report = health_of(client, workspace_id, dataset["dataset_id"])
    codes = {finding["code"] for finding in report["findings"]}

    assert {"all_null_column", "missing_values", "case_variant_categories"} <= codes
    assert report["total_rows"] == 200
    assert report["summary"]["problem"] >= 1


def test_there_is_no_aggregate_quality_score(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    """Сводной оценки нет, и это решение, а не упущение.

    Одно число скрывает, какой именно дефект важен, и подталкивает улучшать число
    вместо данных.
    """
    report = health_of(client, workspace_id, dataset["dataset_id"])

    assert set(report["summary"]) == {"problem", "warning", "notice"}

    for forbidden in ("score", "quality", "grade", "rating"):
        assert forbidden not in str(report).lower()


def test_every_finding_explains_itself(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    #находка без объяснения — это счётчик, а счётчик не помогает принять решение
    for finding in health_of(client, workspace_id, dataset["dataset_id"])["findings"]:
        assert len(finding["explanation"]) > 40, finding["code"]
        assert finding["title"]
        assert finding["exactness"] in {"exact", "sampled"}


def test_signals_are_never_reported_as_defects(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    """Высокая кардинальность, выбросы и похожесть на идентификатор — свойства данных.

    Объявив их дефектом, мы отправим пользователя «чинить» нормальные данные: удалять
    идентификатор и срезать настоящие крупные значения.
    """
    signals = {
        "high_cardinality",
        "potential_identifier",
        "outlier_values",
        "constant_column",
        "near_constant_column",
    }

    for finding in health_of(client, workspace_id, dataset["dataset_id"])["findings"]:
        if finding["code"] in signals:
            assert finding["severity"] == "notice", finding


def test_the_order_of_findings_is_stable(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    #отчёт, меняющий порядок между открытиями, выглядит меняющимся, хотя данные те же
    first = [item["finding_id"] for item in health_of(client, workspace_id, dataset["dataset_id"])["findings"]]
    second = [item["finding_id"] for item in health_of(client, workspace_id, dataset["dataset_id"])["findings"]]

    assert first == second
    #и сначала идёт то, что скорее всего сломано
    severities = [
        item["severity"] for item in health_of(client, workspace_id, dataset["dataset_id"])["findings"]
    ]
    rank = {"problem": 0, "warning": 1, "notice": 2}
    assert severities == sorted(severities, key=lambda level: rank[level])


# ── провал в строки: главное обещание этапа ───────────────────────────────────


def test_the_table_shows_exactly_the_rows_the_finding_counted(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    """«127 строк» в находке означает ровно 127 строк в таблице.

    Проверяется по каждой находке, у которой есть подмножество строк. Подсчёт внутри
    находки и отбор в таблице идут через одно и то же описание предиката — если бы
    это были две похожие реализации, они однажды разошлись бы.
    """
    report = health_of(client, workspace_id, dataset["dataset_id"])
    checked = 0

    for finding in report["findings"]:
        if not finding["has_affected_rows"]:
            continue

        page = client.get(
            f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows",
            params={"finding": finding["finding_id"], "limit": 5},
        )

        assert page.status_code == 200, page.text
        assert page.json()["total_rows"] == finding["affected_rows"], finding["code"]
        checked += 1

    assert checked >= 5, "проверять нечего: находок с подмножеством строк не нашлось"


def test_a_finding_about_the_whole_dataset_offers_no_drill_down(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    #«показать все строки» — это просто открыть датасет, и кнопки для этого быть не должно
    report = health_of(client, workspace_id, dataset["dataset_id"])
    whole = [item for item in report["findings"] if item["code"] == "all_null_column"]

    assert whole, "находка про пустую колонку не найдена"
    assert whole[0]["has_affected_rows"] is False


def test_sorting_and_paging_work_inside_the_subset(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    """Внутри найденных строк работает та же таблица, а не урезанная копия."""
    report = health_of(client, workspace_id, dataset["dataset_id"])
    finding = next(
        item
        for item in report["findings"]
        if item["code"] == "case_variant_categories" and item["has_affected_rows"]
    )
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"

    ascending = client.get(
        base, params={"finding": finding["finding_id"], "sort": "id:asc", "limit": 5}
    ).json()
    descending = client.get(
        base, params={"finding": finding["finding_id"], "sort": "id:desc", "limit": 5}
    ).json()
    second_page = client.get(
        base, params={"finding": finding["finding_id"], "sort": "id:asc", "offset": 5, "limit": 5}
    ).json()

    assert ascending["total_rows"] == finding["affected_rows"]
    assert descending["total_rows"] == finding["affected_rows"]
    assert [row["id"] for row in ascending["rows"]] != [row["id"] for row in descending["rows"]]
    assert [row["id"] for row in second_page["rows"]] != [row["id"] for row in ascending["rows"]]


def test_a_user_filter_narrows_within_the_subset(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    #фильтр пользователя сужает найденные строки, а не заменяет их
    report = health_of(client, workspace_id, dataset["dataset_id"])
    finding = next(item for item in report["findings"] if item["code"] == "missing_values")
    base = f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows"

    whole = client.get(base, params={"finding": finding["finding_id"]}).json()
    narrowed = client.get(
        base, params={"finding": finding["finding_id"], "filter": "id:lt:50"}
    ).json()

    assert narrowed["total_rows"] < whole["total_rows"]
    assert all(row["id"] < 50 for row in narrowed["rows"])


def test_the_rows_of_a_finding_really_have_the_defect(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    """Строки провала действительно содержат то, о чём говорит находка.

    Совпадения количества мало: предикат мог бы отобрать столько же, но не тех строк.
    """
    report = health_of(client, workspace_id, dataset["dataset_id"])
    missing = next(
        item
        for item in report["findings"]
        if item["code"] == "missing_values" and item["columns"] == ["сумма"]
    )

    rows = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows",
        params={"finding": missing["finding_id"], "limit": 50},
    ).json()["rows"]

    assert rows
    assert all(row["сумма"] is None for row in rows)


def test_an_unknown_finding_is_not_found(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    #подставлять вместо неизвестной находки другие строки нельзя: это тихая подмена
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows",
        params={"finding": "fnd_0000000000000000"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "finding_not_found"


# ── пересчёт и устойчивость ───────────────────────────────────────────────────


def test_the_report_is_reused_while_the_artifact_is_the_same(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    first = health_of(client, workspace_id, dataset["dataset_id"])
    second = health_of(client, workspace_id, dataset["dataset_id"])

    assert first["computed_at"] == second["computed_at"], "отчёт пересчитан без причины"
    assert first["artifact_fingerprint"] == second["artifact_fingerprint"]


def test_the_report_survives_a_restart(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    before = health_of(client, workspace_id, dataset["dataset_id"])
    context.reset_context()

    restarted = TestClient(create_app())
    after = restarted.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/health"
    ).json()

    assert after["computed_at"] == before["computed_at"], "отчёт пересчитан после перезапуска"
    assert [item["finding_id"] for item in after["findings"]] == [
        item["finding_id"] for item in before["findings"]
    ]


def test_drill_down_still_works_after_a_restart(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    #описание предиката хранится вместе с находкой, поэтому провал не требует пересчёта
    finding = next(
        item
        for item in health_of(client, workspace_id, dataset["dataset_id"])["findings"]
        if item["has_affected_rows"]
    )
    context.reset_context()

    restarted = TestClient(create_app())
    page = restarted.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows",
        params={"finding": finding["finding_id"]},
    )

    assert page.status_code == 200
    assert page.json()["total_rows"] == finding["affected_rows"]


def test_a_different_dataset_has_different_finding_identifiers(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    """Идентификатор находки привязан к датасету и артефакту.

    Иначе находка одного датасета открыла бы строки другого — полностью неверная
    диагностика при внешне работающем интерфейсе.
    """
    other = upload(client, workspace_id, messy_frame(100), "второй.parquet")
    first_ids = {
        item["finding_id"] for item in health_of(client, workspace_id, dataset["dataset_id"])["findings"]
    }
    second_ids = {
        item["finding_id"] for item in health_of(client, workspace_id, other["dataset_id"])["findings"]
    }

    assert not (first_ids & second_ids)


def test_a_finding_of_another_dataset_is_refused(
    client: TestClient, workspace_id: str, dataset: dict
) -> None:
    other = upload(client, workspace_id, messy_frame(100), "чужой.parquet")
    foreign = health_of(client, workspace_id, other["dataset_id"])["findings"][0]["finding_id"]

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/rows",
        params={"finding": foreign},
    )

    assert response.status_code == 404


def test_health_of_a_missing_dataset_is_not_found(client: TestClient, workspace_id: str) -> None:
    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/ds_0000000000000000/health"
    )

    assert response.status_code in {404, 422}


def test_a_dataset_the_reader_cannot_open_is_a_domain_error(
    client: TestClient, workspace_id: str, tmp_path: Path
) -> None:
    #тот же класс, что R-47: не 500, а понятный отказ
    dataset = upload(client, workspace_id, messy_frame(50), "исчезнет.parquet")

    for path in (tmp_path / "workspaces" / workspace_id / "sources").rglob("*"):
        if path.is_file():
            path.unlink()

    response = client.get(
        f"/api/workspaces/{workspace_id}/datasets/{dataset['dataset_id']}/health"
    )

    assert response.status_code == 404
    assert "C:/" not in str(response.json())
