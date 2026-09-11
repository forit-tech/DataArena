"""Сборка эталонного пакета dataarena.package/1 и негативных фикстур.

Скрипт детерминирован: при одних и тех же входных данных он даёт побайтово одинаковый пакет.
Именно поэтому golden package можно держать в двух репозиториях и сверять.

    python build_golden.py <каталог назначения>
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fingerprint import ALGORITHM, content_fingerprint, logical_type  # noqa: E402

PACKAGE_FORMAT = "dataarena.package"
PACKAGE_VERSION = "1.0.0"
#время сборки зафиксировано: пакет обязан быть воспроизводимым, а не зависеть от даты запуска
BUILT_AT = "2026-09-06T18:42:11.482Z"
ROWS_PER_PART = 6

SEMANTIC_BY_COLUMN = {
    "customer_id": "id",
    "monthly_spend": "numeric",
    "tenure_months": "numeric",
    "plan": "categorical",
    "city": "categorical",
    "signup_date": "datetime",
    "churned": "boolean",
    "internal_note": "text",
}


def build_frame() -> pl.DataFrame:
    #эта таблица намеренно содержит все semantic_type, пропуски в двух колонках,
    #высокую кардинальность в city и явный выброс в monthly_spend
    return pl.DataFrame(
        {
            "customer_id": [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012],
            "monthly_spend": [430.0, 128.5, None, 210.25, 9820.5, 305.0,
                              512.75, None, 188.0, 640.5, 275.25, 399.0],
            "tenure_months": [12, 3, 27, 8, 61, 15, 22, 4, 9, 33, 7, 18],
            "plan": ["basic", "pro", "pro", "basic", "enterprise", "basic",
                     "pro", "basic", "basic", "enterprise", "pro", "basic"],
            "city": ["Москва", "Санкт-Петербург", "Казань", "Новосибирск", "Екатеринбург", "Сочи",
                     "Пермь", "Самара", "Уфа", "Омск", "Тверь", "Тула"],
            "signup_date": [
                dt.date(2023, 1, 15), dt.date(2024, 6, 2), dt.date(2022, 3, 30), dt.date(2024, 1, 9),
                dt.date(2019, 8, 21), dt.date(2023, 11, 4), dt.date(2023, 5, 17), dt.date(2024, 9, 1),
                dt.date(2024, 4, 12), dt.date(2021, 12, 25), dt.date(2024, 7, 8), dt.date(2023, 8, 19),
            ],
            "churned": [False, True, False, False, False, True,
                        False, True, None, False, True, False],
            "internal_note": ["ok", None, "перенос тарифа", "ok", "vip", None,
                              "ok", "жалоба", "ok", "vip", "ok", None],
        }
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    #сортировка ключей и фиксированные разделители нужны для побайтовой воспроизводимости пакета
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def build_schema(frame: pl.DataFrame) -> dict:
    columns = []
    for position, (name, dtype) in enumerate(frame.schema.items()):
        series = frame[name]
        columns.append(
            {
                "name": name,
                "position": position,
                "physical_type": str(dtype),
                "logical_type": logical_type(dtype),
                "semantic_type": SEMANTIC_BY_COLUMN[name],
                "nullable": series.null_count() > 0,
                "unique": series.n_unique() == frame.height,
                "description": None,
                "source": {"dataset": "customers.csv", "original_name": name},
            }
        )
    return {
        "format_version": PACKAGE_VERSION,
        "columns": columns,
        "primary_key": ["customer_id"],
        "constraints": [
            {"kind": "not_null", "columns": ["customer_id", "plan"]},
            {"kind": "unique", "columns": ["customer_id"]},
        ],
    }


def build_profile(frame: pl.DataFrame) -> dict:
    spend = frame["monthly_spend"].drop_nulls()
    q1 = float(spend.quantile(0.25, interpolation="linear"))
    q3 = float(spend.quantile(0.75, interpolation="linear"))
    iqr = q3 - q1
    upper = q3 + 1.5 * iqr
    lower = q1 - 1.5 * iqr
    outlier_positions = [
        index
        for index, value in enumerate(frame["monthly_spend"].to_list())
        if value is not None and (value > upper or value < lower)
    ]

    columns = []
    for name in frame.columns:
        series = frame[name]
        entry = {
            "name": name,
            "semantic_type": SEMANTIC_BY_COLUMN[name],
            "missing_count": series.null_count(),
            "missing_ratio": round(series.null_count() / frame.height, 6),
            "unique_count": series.n_unique(),
            "unique_ratio": round(series.n_unique() / frame.height, 6),
            "is_constant": series.n_unique() <= 1,
            "is_probable_id": name == "customer_id",
            "id_reason": (
                "почти уникальные значения (100.0%) и имя, характерное для идентификатора"
                if name == "customer_id"
                else None
            ),
            "is_imbalanced": False,
        }
        if SEMANTIC_BY_COLUMN[name] == "numeric":
            values = series.drop_nulls()
            entry["numeric_stats"] = {
                "min": float(values.min()),
                "max": float(values.max()),
                "mean": round(float(values.mean()), 6),
                "median": float(values.median()),
                "std": round(float(values.std()), 6),
                "q1": float(values.quantile(0.25, interpolation="linear")),
                "q3": float(values.quantile(0.75, interpolation="linear")),
                "skewness": round(float(values.skew()), 6),
            }
        entry["examples"] = [
            value.isoformat() if hasattr(value, "isoformat") else value
            for value in series.drop_nulls().head(3).to_list()
        ]
        columns.append(entry)

    return {
        "format_version": PACKAGE_VERSION,
        "computed_at": BUILT_AT,
        "computed_on": {"rows": frame.height, "sampled": False},
        "dataset": {
            "row_count": frame.height,
            "column_count": frame.width,
            "duplicate_row_count": frame.height - frame.unique().height,
            "missing_cell_count": sum(frame[name].null_count() for name in frame.columns),
            "missing_cell_ratio": round(
                sum(frame[name].null_count() for name in frame.columns) / (frame.height * frame.width), 6
            ),
            "quality_score": 88,
            "quality_breakdown": {
                "score": 88,
                "max_score": 100,
                "total_penalty": 12.0,
                "penalties": [
                    {
                        "key": "missing_cells",
                        "label": "Пропуски в ячейках",
                        "ratio": 0.083333,
                        "weight": 60.0,
                        "penalty": 5.0,
                        "explanation": "Доля пустых ячеек во всём датасете.",
                    },
                    {
                        "key": "id_like_columns",
                        "label": "ID-подобные колонки",
                        "ratio": 0.125,
                        "weight": 20.0,
                        "penalty": 2.5,
                        "explanation": "Доля почти уникальных колонок.",
                    },
                    {
                        "key": "outlier_values",
                        "label": "Выбросы (1,5 IQR)",
                        "ratio": 0.1,
                        "weight": 20.0,
                        "penalty": 2.0,
                        "explanation": "Доля числовых значений за границами полутора межквартильных размахов.",
                    },
                    {
                        "key": "high_cardinality_columns",
                        "label": "Высокая кардинальность",
                        "ratio": 0.25,
                        "weight": 10.0,
                        "penalty": 2.5,
                        "explanation": "Доля категориальных колонок с очень большим числом различных значений.",
                    },
                ],
            },
        },
        "columns": columns,
        "correlation": {
            "method": "spearman",
            "columns": ["monthly_spend", "tenure_months"],
            "matrix": [[1.0, 0.61], [0.61, 1.0]],
            "high_pairs": [
                {"a": "monthly_spend", "b": "tenure_months", "value": 0.61, "method": "spearman"}
            ],
        },
        "findings": [
            {
                "id": "outliers.monthly_spend",
                "kind": "outliers",
                "severity": "warning",
                "column": "monthly_spend",
                "count": len(outlier_positions),
                "method": "iqr_1.5",
                "threshold": {"lower": round(lower, 4), "upper": round(upper, 4)},
                "explanation": (
                    f"{len(outlier_positions)} значений выходят за границы "
                    "полутора межквартильных размахов."
                ),
                "drilldown": {
                    "kind": "sql",
                    "dialect": "duckdb",
                    "query": (
                        "SELECT * FROM dataset WHERE monthly_spend > "
                        f"{round(upper, 4)} OR monthly_spend < {round(lower, 4)}"
                    ),
                    "row_indexes": outlier_positions,
                },
            },
            {
                "id": "missing.internal_note",
                "kind": "missing",
                "severity": "info",
                "column": "internal_note",
                "count": frame["internal_note"].null_count(),
                "method": "null_count",
                "explanation": "Колонка заполнена не для всех строк.",
                "drilldown": {
                    "kind": "sql",
                    "dialect": "duckdb",
                    "query": "SELECT * FROM dataset WHERE internal_note IS NULL",
                    "row_indexes": [
                        index
                        for index, value in enumerate(frame["internal_note"].to_list())
                        if value is None
                    ],
                },
            },
        ],
        "suspicious_columns": [
            {
                "column": "internal_note",
                "reason": "свободный текст, заполняемый оператором; часть значений появляется после события",
                "recommendation": "проверить момент заполнения относительно предсказываемого события",
            }
        ],
    }


def build_lineage() -> dict:
    return {
        "format_version": PACKAGE_VERSION,
        "package_id": "pkg_01J8ZK4M7Q2N5X",
        "created_at": BUILT_AT,
        "sources": [
            {
                "id": "src_customers",
                "name": "customers.csv",
                "format": "csv",
                "bytes": 1284,
                "sha256": "b" * 64,
                "row_count": 14,
                "column_count": 8,
                "read_options": {"separator": ",", "encoding": "utf-8"},
            }
        ],
        "graph": [
            {"id": "n1", "op": "read", "inputs": [], "source": "src_customers", "output_rows": 14},
            {
                "id": "n2",
                "op": "rename_column",
                "inputs": ["n1"],
                "output_rows": 14,
                "params": {"from": "userid", "to": "customer_id"},
            },
            {
                "id": "n3",
                "op": "deduplicate",
                "inputs": ["n2"],
                "output_rows": 12,
                "params": {"subset": ["customer_id"], "keep": "first"},
                "note": "две строки повторяли customer_id",
            },
        ],
        "row_count_trace": [
            {"after": "n1", "rows": 14},
            {"after": "n2", "rows": 14},
            {"after": "n3", "rows": 12},
        ],
    }


def build_recipe() -> dict:
    return {
        "format_version": PACKAGE_VERSION,
        "name": "customers_golden",
        "derived_from_lineage": False,
        "inputs": [
            {
                "alias": "customers",
                "required_columns": ["userid", "monthly_spend", "plan", "signup_date"],
                "format_hint": "csv",
                "source_sha256": "b" * 64,
            }
        ],
        "steps": [
            {"op": "rename_column", "on": "customers", "params": {"from": "userid", "to": "customer_id"}},
            {"op": "deduplicate", "params": {"subset": ["customer_id"], "keep": "first"}},
        ],
        "output": {"format": "parquet", "compression": "zstd"},
    }


def build_package(destination: Path, frame: pl.DataFrame) -> dict:
    #эта функция собирает пакет на диске и возвращает манифест
    if destination.exists():
        shutil.rmtree(destination)
    (destination / "data").mkdir(parents=True)

    parts = [frame.slice(0, ROWS_PER_PART), frame.slice(ROWS_PER_PART, ROWS_PER_PART)]
    data_files = []
    for index, part in enumerate(parts):
        relative = f"data/part-{index:04d}.parquet"
        part.write_parquet(destination / relative, compression="zstd")
        data_files.append(relative)

    write_json(destination / "schema.json", build_schema(frame))
    write_json(destination / "profile.json", build_profile(frame))
    write_json(destination / "lineage.json", build_lineage())
    write_json(destination / "recipe.json", build_recipe())

    part_entries = []
    for relative in [*data_files, "schema.json", "profile.json", "lineage.json", "recipe.json"]:
        path = destination / relative
        part_entries.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )

    manifest = {
        "format": PACKAGE_FORMAT,
        "format_version": PACKAGE_VERSION,
        "package_id": "pkg_01J8ZK4M7Q2N5X",
        "name": "customers_golden",
        "created_at": BUILT_AT,
        "producer": {"name": "DataArena", "version": "0.0.0-contract"},
        "version": {
            "dataset_id": "ds_01J8ZK1A2B3C4D",
            "version_id": "dsv_01J8ZK4M6P1Q2R",
            "version_number": 2,
            "parent_version_id": "dsv_01J8ZJ9Z8Y7X6W",
        },
        "data": {
            "files": data_files,
            "format": "parquet",
            "compression": "zstd",
            "row_count": frame.height,
            "row_count_exact": True,
            "column_count": frame.width,
            "bytes": sum(entry["bytes"] for entry in part_entries[: len(data_files)]),
            "row_key": ["customer_id"],
            "content_fingerprint": {"algorithm": ALGORITHM, "value": content_fingerprint(frame)},
        },
        "parts": part_entries,
        "hints": {
            "suggested_targets": [
                {
                    "column": "churned",
                    "reason": "boolean, 2 класса, доля меньшего 0.36",
                    "confidence": "medium",
                },
                {
                    "column": "plan",
                    "reason": "категориальная, 3 класса, без пропусков",
                    "confidence": "low",
                },
            ],
            "suggested_group_column": "customer_id",
            "suggested_time_column": "signup_date",
            "identifier_columns": ["customer_id"],
            "datetime_columns": ["signup_date"],
            "high_cardinality_columns": ["city"],
            "excluded_candidates": [
                {
                    "column": "internal_note",
                    "reason": "свободный текст оператора, часть значений появляется после события",
                }
            ],
            "excluded_by_user": [],
        },
        "links": {
            "dataarena_base_url": "http://127.0.0.1:5174",
            "inspect_url_template": "/workspaces/ws_golden/datasets/ds_01J8ZK1A2B3C4D/v2?focus={column}",
        },
    }
    write_json(destination / "manifest.json", manifest)
    return manifest


def zip_package(source: Path, archive_path: Path) -> None:
    #архив собирается с фиксированной датой записей, иначе два запуска дали бы разные байты
    entries = sorted(path for path in source.rglob("*") if path.is_file())
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in entries:
            info = zipfile.ZipInfo(str(path.relative_to(source)).replace("\\", "/"), (2026, 9, 6, 18, 42, 10))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def build_negative_fixtures(root: Path, frame: pl.DataFrame) -> None:
    #негативные фикстуры описаны в контракте, §H: каждая проверяет одну реакцию потребителя
    negative = root / "negative"
    negative.mkdir(parents=True, exist_ok=True)

    base = root / "customers_golden.dapkg"

    def clone(name: str) -> Path:
        target = negative / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(base, target)
        return target

    def load_manifest(package: Path) -> dict:
        return json.loads((package / "manifest.json").read_text(encoding="utf-8"))

    future_major = clone("future-major.dapkg")
    manifest = load_manifest(future_major)
    manifest["format_version"] = "2.0.0"
    write_json(future_major / "manifest.json", manifest)

    future_minor = clone("future-minor.dapkg")
    manifest = load_manifest(future_minor)
    manifest["format_version"] = "1.7.0"
    manifest["unknown_future_field"] = {"added_in": "1.7.0"}
    manifest["hints"]["unknown_hint_kind"] = ["city"]
    write_json(future_minor / "manifest.json", manifest)

    corrupted = clone("corrupted-part.dapkg")
    manifest = load_manifest(corrupted)
    manifest["parts"][0]["sha256"] = "0" * 64
    write_json(corrupted / "manifest.json", manifest)

    #подменяем данные, но честно пересчитываем sha256: ловится только отпечатком содержимого
    mismatch = clone("fingerprint-mismatch.dapkg")
    tampered = frame.slice(0, ROWS_PER_PART).with_columns(
        pl.when(pl.col("customer_id") == 1001)
        .then(99999.0)
        .otherwise(pl.col("monthly_spend"))
        .alias("monthly_spend")
    )
    tampered.write_parquet(mismatch / "data/part-0000.parquet", compression="zstd")
    manifest = load_manifest(mismatch)
    for entry in manifest["parts"]:
        if entry["path"] == "data/part-0000.parquet":
            path = mismatch / entry["path"]
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha256_file(path)
    write_json(mismatch / "manifest.json", manifest)

    sampled = clone("sampled-profile.dapkg")
    profile = json.loads((sampled / "profile.json").read_text(encoding="utf-8"))
    profile["computed_on"] = {"rows": 6, "sampled": True, "sample_method": "head"}
    write_json(sampled / "profile.json", profile)
    manifest = load_manifest(sampled)
    for entry in manifest["parts"]:
        if entry["path"] == "profile.json":
            path = sampled / entry["path"]
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha256_file(path)
    write_json(sampled / "manifest.json", manifest)

    malformed = clone("malformed-schema.dapkg")
    schema_payload = json.loads((malformed / "schema.json").read_text(encoding="utf-8"))
    #position обязан быть целым: подменяем тип, чтобы пакет нарушил собственную JSON Schema
    schema_payload["columns"][0]["position"] = "нулевая"
    write_json(malformed / "schema.json", schema_payload)
    manifest = load_manifest(malformed)
    for entry in manifest["parts"]:
        if entry["path"] == "schema.json":
            path = malformed / entry["path"]
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha256_file(path)
    write_json(malformed / "manifest.json", manifest)

    missing = clone("missing-artifact.dapkg")
    (missing / "data/part-0001.parquet").unlink()

    duplicate = clone("duplicate-entry.dapkg")
    manifest = load_manifest(duplicate)
    conflicting = dict(manifest["parts"][0])
    conflicting["sha256"] = "1" * 64
    manifest["parts"].append(conflicting)
    write_json(duplicate / "manifest.json", manifest)

    #переставленные колонки в schema.json при честно пересчитанном sha256:
    #иначе первым сработал бы контроль целостности и правило схемы не было бы достигнуто
    schema_mismatch = clone("schema-mismatch.dapkg")
    schema_payload = json.loads((schema_mismatch / "schema.json").read_text(encoding="utf-8"))
    schema_payload["columns"][0], schema_payload["columns"][1] = (
        schema_payload["columns"][1],
        schema_payload["columns"][0],
    )
    for position, column in enumerate(schema_payload["columns"]):
        column["position"] = position
    write_json(schema_mismatch / "schema.json", schema_payload)
    manifest = load_manifest(schema_mismatch)
    for entry in manifest["parts"]:
        if entry["path"] == "schema.json":
            path = schema_mismatch / entry["path"]
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha256_file(path)
    write_json(schema_mismatch / "manifest.json", manifest)

    #архивные фикстуры собираются вручную: такие записи нельзя получить обычной упаковкой каталога
    traversal = negative / "traversal.dapkg.zip"
    with zipfile.ZipFile(traversal, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(str(path.relative_to(base)).replace("\\", "/"), (2026, 9, 6, 18, 42, 10))
            archive.writestr(info, path.read_bytes())
        escape = zipfile.ZipInfo("../escaped.txt", (2026, 9, 6, 18, 42, 10))
        archive.writestr(escape, b"this file tries to leave the package directory")

    bomb = negative / "zip-bomb.dapkg.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(str(path.relative_to(base)).replace("\\", "/"), (2026, 9, 6, 18, 42, 10))
            archive.writestr(info, path.read_bytes())
        #40 МБ нулей сжимаются в десятки килобайт: коэффициент заведомо выше 100:1
        filler = zipfile.ZipInfo("data/filler.bin", (2026, 9, 6, 18, 42, 10))
        filler.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(filler, b"\x00" * (40 * 1024 * 1024))

    minimal = negative / "minimal.dapkg"
    if minimal.exists():
        shutil.rmtree(minimal)
    (minimal / "data").mkdir(parents=True)
    frame.write_parquet(minimal / "data/part-0000.parquet", compression="zstd")
    part_path = minimal / "data/part-0000.parquet"
    write_json(
        minimal / "manifest.json",
        {
            "format": PACKAGE_FORMAT,
            "format_version": PACKAGE_VERSION,
            "package_id": "pkg_minimal01",
            "name": "customers_minimal",
            "created_at": BUILT_AT,
            "producer": {"name": "DataArena", "version": "0.0.0-contract"},
            "data": {
                "files": ["data/part-0000.parquet"],
                "format": "parquet",
                "compression": "zstd",
                "row_count": frame.height,
                "row_count_exact": True,
                "column_count": frame.width,
                "bytes": part_path.stat().st_size,
                "content_fingerprint": {
                    "algorithm": ALGORITHM,
                    "value": content_fingerprint(frame),
                },
            },
            "parts": [
                {
                    "path": "data/part-0000.parquet",
                    "bytes": part_path.stat().st_size,
                    "sha256": sha256_file(part_path),
                }
            ],
        },
    )


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "golden"
    root.mkdir(parents=True, exist_ok=True)
    frame = build_frame()

    package = root / "customers_golden.dapkg"
    manifest = build_package(package, frame)
    zip_package(package, root / "customers_golden.dapkg.zip")
    build_negative_fixtures(root, frame)

    print(f"golden package: {package}")
    print(f"  строк: {manifest['data']['row_count']}, колонок: {manifest['data']['column_count']}")
    print(f"  fingerprint: {manifest['data']['content_fingerprint']['value']}")
    print(f"  частей данных: {len(manifest['data']['files'])}, файлов всего: {len(manifest['parts'])}")


if __name__ == "__main__":
    main()
