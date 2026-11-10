"""Фикстуры истории колонок (1.1.0) — производные от эталонного пакета.

Данные, схема, профиль и рецепт — те же, что в `golden/customers_golden.dapkg`; меняется
только `lineage.json` и его запись в манифесте. Эталонный пакет при этом не трогается:
он остаётся фикстурой `1.0.0` без истории колонок — старым пакетом, который обязан
читаться как прежде.

    python build_lineage_fixtures.py [каталог контракта]

Сборка детерминирована: повторный запуск даёт те же байты.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_golden import sha256_file, write_json  # noqa: E402

VERSION = "1.1.0"
CREATED_AT = "2026-09-25T12:00:00.000Z"
SOURCE_SHA = "b" * 64
LEFT_SHA = "c" * 64
RIGHT_SHA = "d" * 64
COLUMNS = ["customer_id", "monthly_spend", "tenure_months", "plan", "city", "signup_date", "churned", "internal_note"]


def ref(column: str, dataset_id: str, dataset_name: str, sha: str, source: str | None) -> dict:
    entry = {
        "column": column,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "source_file": dataset_name,
        "source_sha256": sha,
    }
    if source is not None:
        entry["source"] = source
    return entry


def event(kind: str, operation: str, dataset: tuple[str, str], position: int, inputs: list[str], output: str, node: str | None) -> dict:
    entry = {
        "kind": kind,
        "operation": operation,
        "dataset_id": dataset[0],
        "dataset_name": dataset[1],
        "position": position,
        "step_id": f"s{position}" if kind == "step" else None,
        "description": f"{operation}: {', '.join(inputs)} → {output}",
        "inputs": inputs,
        "output": output,
    }
    if node is not None:
        entry["node"] = node
    return entry


def column(name: str, origin: str, origins: list[dict], events: list[dict], read_by: list[str] | None = None, history: str = "complete") -> dict:
    return {
        "name": name,
        "origin": origin,
        "history": history,
        "origins": origins,
        "events": events,
        "read_by": read_by or [],
    }


def provenance(status: str, origin: str | None, history: str, warnings: list[str], reason: str | None = None) -> dict:
    return {"status": status, "origin": origin, "unknown_reason": reason, "history": history, "warnings": warnings}


# ── загрузка файлом ──────────────────────────────────────────────────────────


def upload_lineage(base: dict) -> dict:
    lineage = copy.deepcopy(base)
    own = ("ds_customers", "customers.csv")
    lineage["format_version"] = VERSION
    lineage["provenance"] = provenance("known", "upload", "complete", [])
    lineage["columns"] = [
        column(
            "customer_id",
            "source",
            [ref("userid", *own, SOURCE_SHA, "src_customers")],
            [event("step", "rename_column", own, 1, ["userid"], "customer_id", "n2")],
            read_by=["n3"],
        ),
        *(
            column(name, "source", [ref(name, *own, SOURCE_SHA, "src_customers")], [])
            for name in COLUMNS[1:]
        ),
    ]
    return lineage


# ── сборка соединением ───────────────────────────────────────────────────────

LEFT = ("ds_clients", "clients.csv")
RIGHT = ("ds_billing", "billing.parquet")
CHILD = ("ds_customers", "customers ⋈ billing.parquet")


def join_graph(lineage: dict) -> None:
    lineage["sources"] = [
        {"id": "src_customers", "name": "customers ⋈ billing.parquet", "format": "parquet", "sha256": SOURCE_SHA, "row_count": 14, "column_count": 8},
        {"id": "src_ds_clients", "name": "clients.csv", "format": "csv", "sha256": LEFT_SHA, "kind": "dataset"},
        {"id": "src_ds_billing", "name": "billing.parquet", "format": "parquet", "sha256": RIGHT_SHA, "kind": "dataset"},
    ]
    lineage["graph"] = [
        {"id": "jl0", "op": "read", "inputs": [], "source": "src_ds_clients"},
        {"id": "jl1", "op": "trim_whitespace", "inputs": ["jl0"], "params": {"columns": ["city"]}},
        {"id": "jr0", "op": "read", "inputs": [], "source": "src_ds_billing"},
        {"id": "jr1", "op": "fill_missing", "inputs": ["jr0"], "params": {"column": "monthly_spend", "value": "0"}},
        {"id": "j", "op": "join", "inputs": ["jl1", "jr1"], "params": {"kind": "left", "keys": [{"left": "customer_id", "right": "customer_id"}], "right_suffix": "_2"}},
        {"id": "n0", "op": "read", "inputs": ["j"], "source": "src_customers"},
        {"id": "n1", "op": "change_case", "inputs": ["n0"], "params": {"columns": ["city"], "case": "lower"}},
        {"id": "n2", "op": "sort_rows", "inputs": ["n1"], "params": {"column": "monthly_spend"}, "output_rows": 12},
    ]
    lineage["row_count_trace"] = [{"after": "n2", "rows": 12}]


def join_event(name: str, side: str, source: str) -> dict:
    parent = LEFT if side == "left" else RIGHT
    return event("join", "join", CHILD, 0, [source], name, "j") | {"description": f"LEFT JOIN: «{parent[1]}».{source} → {name}"}


def left_column(name: str, extra: list[dict] | None = None, read_by: list[str] | None = None) -> dict:
    return column(
        name,
        "source",
        [ref(name, *LEFT, LEFT_SHA, "src_ds_clients")],
        [*(extra or []), join_event(name, "left", name)],
        read_by=read_by,
    )


def join_lineage(base: dict) -> dict:
    lineage = copy.deepcopy(base)
    lineage["format_version"] = VERSION
    join_graph(lineage)
    lineage["provenance"] = provenance("known", "join", "complete", [])
    lineage["columns"] = [
        left_column("customer_id"),
        column(
            "monthly_spend",
            "source",
            [ref("monthly_spend", *RIGHT, RIGHT_SHA, "src_ds_billing")],
            [event("step", "fill_missing", RIGHT, 1, ["monthly_spend"], "monthly_spend", "jr1"), join_event("monthly_spend", "right", "monthly_spend")],
            read_by=["n2"],
        ),
        left_column("tenure_months"),
        left_column("plan"),
        left_column(
            "city",
            extra=[event("step", "trim_whitespace", LEFT, 1, ["city"], "city", "jl1")],
        ),
        left_column("signup_date"),
        column(
            "churned",
            "source",
            [ref("churned", *RIGHT, RIGHT_SHA, "src_ds_billing")],
            [join_event("churned", "right", "churned")],
        ),
        left_column("internal_note"),
    ]
    #изменение после сборки: регистр города в самом датасете
    lineage["columns"][4]["events"].append(event("step", "change_case", CHILD, 1, ["city"], "city", "n1"))
    return lineage


def multi_level_lineage(base: dict) -> dict:
    """Три уровня: сырой файл → родитель → соединение → датасет, и колонка из двух источников.

    События дедушки ссылаются на узлы, которых в графе этого пакета нет, — поэтому
    `node` у них отсутствует, а датасет назван в самом событии.
    """
    lineage = join_lineage(base)
    raw = ("ds_raw", "raw_clients.json")
    city = lineage["columns"][4]
    city["origins"] = [ref("City", *raw, "e" * 64, None)]
    city["events"] = [
        event("step", "rename_column", raw, 1, ["City"], "city", None),
        event("step", "normalize_nulls", raw, 2, ["city"], "city", None),
        *city["events"],
    ]
    #будущая вычисляемая колонка: два источника, незнакомая операция
    lineage["columns"][1] = column(
        "monthly_spend",
        "step",
        [
            ref("quantity", *RIGHT, RIGHT_SHA, "src_ds_billing"),
            ref("price", *RIGHT, RIGHT_SHA, "src_ds_billing"),
        ],
        [
            event("step", "derive", RIGHT, 1, ["quantity", "price"], "monthly_spend", None),
            join_event("monthly_spend", "right", "monthly_spend"),
        ],
    )
    return lineage


def legacy_unknown(base: dict) -> dict:
    lineage = copy.deepcopy(base)
    lineage["format_version"] = VERSION
    lineage["provenance"] = provenance("unknown", None, "incomplete", ["provenance_unknown"], "missing_record")
    lineage["columns"] = [column(name, "unknown", [], [], history="incomplete") for name in COLUMNS]
    return lineage


def truncated_history(base: dict) -> dict:
    lineage = join_lineage(base)
    lineage["provenance"] = provenance("known", "join", "incomplete", ["parent_history_truncated"])
    for entry in lineage["columns"]:
        if entry["origins"] and entry["origins"][0]["dataset_id"] == RIGHT[0]:
            #история правого родителя не уместилась в предел: остались источник и соединение
            entry["events"] = [item for item in entry["events"] if item["dataset_id"] != RIGHT[0]]
            entry["history"] = "incomplete"
    return lineage


def future_optional_field(base: dict) -> dict:
    lineage = upload_lineage(base)
    lineage["provenance"]["confidence_from_the_future"] = 0.9
    lineage["provenance"]["status"] = "partially_known"
    lineage["provenance"]["history"] = "mostly"
    first = lineage["columns"][0]
    first["future_column_field"] = {"nested": [1, 2, 3]}
    first["origins"][0]["future_origin_field"] = "x"
    first["events"][0]["future_event_field"] = True
    first["events"].append(event("window", "rolling_mean", ("ds_customers", "customers.csv"), 2, ["customer_id"], "customer_id", None))
    lineage["future_top_level_field"] = {"anything": "goes"}
    return lineage


# ── отказы ───────────────────────────────────────────────────────────────────


def malformed_lineage(base: dict) -> dict:
    lineage = upload_lineage(base)
    lineage["columns"][0]["origins"] = "userid"
    return lineage


def inconsistent_lineage(base: dict) -> dict:
    lineage = upload_lineage(base)
    #«неизвестно» с источником и ссылка на несуществующий узел — пакет противоречит сам себе
    lineage["columns"][1]["origin"] = "unknown"
    lineage["columns"][2]["events"] = [event("step", "cast_column", ("ds_customers", "customers.csv"), 9, ["tenure_months"], "tenure_months", "n99")]
    return lineage


def oversized_lineage(base: dict) -> dict:
    lineage = upload_lineage(base)
    lineage["columns"][0]["events"][0]["description"] = "ж" * 10_001
    return lineage


def deep_lineage(base: dict) -> dict:
    lineage = upload_lineage(base)
    nested: dict = {"level": 0}
    for level in range(1, 40):
        nested = {"level": level, "inner": nested}
    lineage["columns"][0]["future_nested"] = nested
    return lineage


FIXTURES = {
    "upload-lineage": upload_lineage,
    "join-lineage": join_lineage,
    "multi-level-lineage": multi_level_lineage,
    "legacy-unknown": legacy_unknown,
    "truncated-history": truncated_history,
    "future-optional-field": future_optional_field,
    "malformed-lineage": malformed_lineage,
    "inconsistent-lineage": inconsistent_lineage,
    "oversized-lineage": oversized_lineage,
    "deep-lineage": deep_lineage,
}


def write_fixture(golden: Path, target: Path, lineage_text: str, version: str) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(golden, target)
    (target / "lineage.json").write_text(lineage_text, encoding="utf-8", newline="\n")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["format_version"] = version
    for part in manifest["parts"]:
        if part["path"] == "lineage.json":
            part["bytes"] = (target / "lineage.json").stat().st_size
            part["sha256"] = sha256_file(target / "lineage.json")
    write_json(target / "manifest.json", manifest)


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    golden = root / "golden" / "customers_golden.dapkg"
    base = json.loads((golden / "lineage.json").read_text(encoding="utf-8"))
    output = root / "golden" / "lineage"
    output.mkdir(parents=True, exist_ok=True)

    for name, build in FIXTURES.items():
        payload = build(base)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        write_fixture(golden, output / f"{name}.dapkg", text, VERSION)

    #NaN в JSON: json.dumps его пишет, а JSON его не допускает
    nan_text = json.dumps(upload_lineage(base), ensure_ascii=False, indent=2, sort_keys=True).replace(
        '"position": 1', '"position": NaN', 1
    ) + "\n"
    write_fixture(golden, output / "nan-lineage.dapkg", nan_text, VERSION)

    print(f"lineage-фикстуры: {len(FIXTURES) + 1} в {output}")


if __name__ == "__main__":
    main()
