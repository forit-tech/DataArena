"""Сквозная проверка диагностики на настоящем сервере.

Идёт тем же путём, что и человек: загрузить датасет, открыть диагностику, взять находку,
посмотреть её строки, отсортировать их, перелистнуть, вернуться, перезапустить backend
и открыть снова.

Запуск: python scripts/smoke_health.py
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_sql import Server, check, free_port, post_json, request, upload

ROWS = 500


def messy_csv() -> bytes:
    frame = pl.DataFrame(
        {
            "id": list(range(ROWS)),
            #регистр, краевые пробелы и пропуски
            "город": [["Москва", "москва", "Казань ", None, "Омск"][n % 5] for n in range(ROWS)],
            #пропуски и одно значение далеко за пределами основной массы
            "сумма": [
                None if n % 20 == 0 else (999_999.0 if n == 7 else float(n % 50))
                for n in range(ROWS)
            ],
            "пусто": [None] * ROWS,
            "одно": ["одинаково"] * ROWS,
            "почти": ["да"] * (ROWS - 3) + ["нет", "нет", "нет"],
        }
    )
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    return buffer.getvalue()


def prepare(base: str) -> tuple[str, str]:
    _, workspace = post_json(f"{base}/workspaces", {"name": "Диагностика"})
    workspace_id = workspace["workspace_id"]

    status, dataset = upload(
        f"{base}/workspaces/{workspace_id}/datasets", "грязный.parquet", messy_csv()
    )
    check("датасет загружен", status == 201, str(dataset)[:160])

    return workspace_id, dataset["dataset_id"]


def look_at_findings(base: str, workspace_id: str, dataset_id: str) -> dict:
    status, report = request("GET", f"{base}/workspaces/{workspace_id}/datasets/{dataset_id}/health")
    check("диагностика посчитана", status == 200, str(report)[:160])
    check(
        "находки есть и они объяснены",
        report["findings"] and all(len(item["explanation"]) > 40 for item in report["findings"]),
        str(report["summary"]),
    )
    check(
        "сводной оценки качества нет",
        set(report["summary"]) == {"problem", "warning", "notice"},
        str(report["summary"]),
    )
    check(
        "сигналы не выданы за дефекты",
        all(
            item["severity"] == "notice"
            for item in report["findings"]
            if item["code"]
            in {
                "high_cardinality",
                "potential_identifier",
                "outlier_values",
                "constant_column",
                "near_constant_column",
            }
        ),
    )
    check(
        "у находки на весь датасет нет кнопки строк",
        all(
            not item["has_affected_rows"]
            for item in report["findings"]
            if item["code"] == "all_null_column"
        ),
    )

    return report


def drill_into_every_finding(base: str, workspace_id: str, dataset_id: str, report: dict) -> dict:
    """Главное обещание: сколько сказала находка, столько и в таблице."""
    endpoint = f"{base}/workspaces/{workspace_id}/datasets/{dataset_id}/rows"
    first_with_rows: dict = {}

    for finding in report["findings"]:
        if not finding["has_affected_rows"]:
            continue

        status, page = request(
            "GET", f"{endpoint}?finding={finding['finding_id']}&limit=5"
        )
        check(
            f"строки находки {finding['code'][:28]}",
            status == 200 and page["total_rows"] == finding["affected_rows"],
            f"{status} находка={finding['affected_rows']} таблица={page.get('total_rows')}",
        )

        first_with_rows = first_with_rows or finding

    check("нашлось хотя бы одно подмножество строк", bool(first_with_rows))

    return first_with_rows


def work_inside_the_subset(base: str, workspace_id: str, dataset_id: str, finding: dict) -> None:
    endpoint = f"{base}/workspaces/{workspace_id}/datasets/{dataset_id}/rows"
    token = finding["finding_id"]

    status, ascending = request("GET", f"{endpoint}?finding={token}&sort=id:asc&limit=5")
    check("сортировка внутри найденных строк", status == 200, str(status))

    status, descending = request("GET", f"{endpoint}?finding={token}&sort=id:desc&limit=5")
    check(
        "порядок действительно меняется",
        [row["id"] for row in ascending["rows"]] != [row["id"] for row in descending["rows"]],
    )
    check(
        "отбор при сортировке сохраняется",
        ascending["total_rows"] == descending["total_rows"] == finding["affected_rows"],
    )

    status, second = request("GET", f"{endpoint}?finding={token}&sort=id:asc&offset=5&limit=5")
    check(
        "перелистывание внутри найденных строк",
        status == 200
        and [row["id"] for row in second["rows"]] != [row["id"] for row in ascending["rows"]],
    )

    check(
        "номер строки относится к файлу, а не к странице",
        second["row_ordinals"] and second["row_ordinals"] != [0, 1, 2, 3, 4],
        str(second["row_ordinals"]),
    )

    status, narrowed = request("GET", f"{endpoint}?finding={token}&filter=id:lt:50")
    check(
        "пользовательский фильтр сужает найденное, а не заменяет",
        status == 200 and narrowed["total_rows"] < finding["affected_rows"],
        f"{narrowed.get('total_rows')} против {finding['affected_rows']}",
    )

    status, whole = request("GET", endpoint)
    check(
        "без находки видно весь датасет",
        status == 200 and whole["total_rows"] == ROWS,
        str(whole.get("total_rows")),
    )


def main() -> int:
    workspace_root = Path(tempfile.mkdtemp(prefix="dataarena_health_"))
    port = free_port()
    server = Server(workspace_root, port)

    try:
        server.wait()

        print("1. Датасет")
        workspace_id, dataset_id = prepare(server.base)

        print("2. Диагностика")
        report = look_at_findings(server.base, workspace_id, dataset_id)

        print("3. Строки находок")
        finding = drill_into_every_finding(server.base, workspace_id, dataset_id, report)

        print("4. Работа внутри найденных строк")
        work_inside_the_subset(server.base, workspace_id, dataset_id, finding)

        print("5. Возврат к диагностике")
        again = look_at_findings(server.base, workspace_id, dataset_id)
        check("отчёт тот же, а не пересчитан заново", again["computed_at"] == report["computed_at"])
        check(
            "идентификаторы находок не изменились",
            [item["finding_id"] for item in again["findings"]]
            == [item["finding_id"] for item in report["findings"]],
        )

        print("6. Перезапуск backend")
        server.stop()
        server = Server(workspace_root, port)
        server.wait()

        status, after = request(
            "GET", f"{server.base}/workspaces/{workspace_id}/datasets/{dataset_id}/health"
        )
        check("диагностика пережила перезапуск", status == 200 and after["computed_at"] == report["computed_at"])

        status, page = request(
            "GET",
            f"{server.base}/workspaces/{workspace_id}/datasets/{dataset_id}/rows"
            f"?finding={finding['finding_id']}&limit=5",
        )
        check(
            "провал в строки работает после перезапуска",
            status == 200 and page["total_rows"] == finding["affected_rows"],
            f"{status} {page.get('total_rows')}",
        )

        print("\nСквозная проверка диагностики пройдена.")
        return 0
    finally:
        server.stop()
        shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
