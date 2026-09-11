"""Валидатор пакета dataarena.package/1 — эталонная реализация правил §H контракта.

Обе стороны используют одни и те же JSON Schema и один и тот же список проверок.

    python validate_package.py <путь к .dapkg>
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fingerprint import ALGORITHM, content_fingerprint, logical_type  # noqa: E402

SCHEMA_DIRECTORY = Path(__file__).resolve().parents[1] / "schema"
SUPPORTED_MAJOR = 1
#лимиты §H: метаданные заданы контрактом, размер данных — конфигурацией потребителя
LIMITS = {
    "manifest_bytes": 1 * 1024 * 1024,
    "schema_bytes": 16 * 1024 * 1024,
    "profile_bytes": 16 * 1024 * 1024,
    "lineage_bytes": 16 * 1024 * 1024,
    "recipe_bytes": 16 * 1024 * 1024,
    "metadata_total_bytes": 64 * 1024 * 1024,
    "data_parts": 4096,
    "archive_files": 10_000,
    "decompression_ratio": 100,
}
ARCHIVE_SUFFIXES = {".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".tgz", ".dapkg"}
#не часть контракта: предел конкретного потребителя, при отказе он обязан сказать, что это его ограничение
DEFAULT_MAX_DATA_BYTES = 5 * 1024 * 1024 * 1024
EXECUTABLE_SUFFIXES = {".pkl", ".pickle", ".joblib", ".dill", ".pt", ".pth", ".h5", ".so", ".dll", ".exe"}
OPTIONAL_PARTS = {
    "schema.json": ("schema.schema.json", "schema_bytes"),
    "profile.json": ("profile.schema.json", "profile_bytes"),
    "lineage.json": ("lineage.schema.json", "lineage_bytes"),
    "recipe.json": ("recipe.schema.json", "recipe_bytes"),
}


@dataclass
class Report:
    #структура повторяет контракт ошибок обоих сервисов: код, сообщение, детали
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, code: str, message: str) -> None:
        self.errors.append((code, message))

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def load_schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMA_DIRECTORY / name).read_text(encoding="utf-8")))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_json_part(package: Path, name: str, report: Report) -> dict | None:
    #эта функция проверяет размер и схему одной необязательной части пакета
    path = package / name
    if not path.exists():
        return None

    schema_name, limit_key = OPTIONAL_PARTS[name]
    if path.stat().st_size > LIMITS[limit_key]:
        report.fail("package_limit_exceeded", f"{name} больше лимита {LIMITS[limit_key]} байт.")
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    errors = list(load_schema(schema_name).iter_errors(payload))

    for error in errors:
        report.fail("package_part_invalid", f"{name}: {error.message}")

    #часть, не прошедшую собственную схему, дальше не разбираем: иначе на один дефект
    #пришло бы несколько кодов ошибки, и потребителю было бы неясно, на какой реагировать
    return None if errors else payload


def validate_archive(archive_path: Path, report: Report) -> bool:
    #эта функция проверяет архив ДО распаковки: после неё лимиты уже не помогут
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()

        if len(entries) > LIMITS["archive_files"]:
            report.fail("package_limit_exceeded", f"В архиве {len(entries)} файлов.")
            return False

        for entry in entries:
            name = entry.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                report.fail("package_unsafe_path", f"Небезопасный путь в архиве: {entry.filename}.")
                return False
            if Path(name).suffix.lower() in ARCHIVE_SUFFIXES:
                report.fail("package_nested_archive", f"Вложенный архив: {entry.filename}.")
                return False

        compressed = sum(entry.compress_size for entry in entries) or 1
        uncompressed = sum(entry.file_size for entry in entries)
        if uncompressed / compressed > LIMITS["decompression_ratio"]:
            report.fail(
                "package_limit_exceeded",
                f"Коэффициент распаковки {uncompressed / compressed:.0f}:1 выше предела "
                f"{LIMITS['decompression_ratio']}:1.",
            )
            return False

    return True


def validate_package(package: Path, max_data_bytes: int = DEFAULT_MAX_DATA_BYTES) -> Report:
    #эта функция выполняет матрицу проверок §H в том же порядке, в каком её обязан выполнять потребитель
    report = Report()

    if package.is_file():
        #архив сначала проверяется по своим правилам, и только потом распаковывается во временный каталог
        if not zipfile.is_zipfile(package):
            report.fail("package_format_unknown", "Файл не является каталогом пакета и не является zip-архивом.")
            return report
        if not validate_archive(package, report):
            return report
        with tempfile.TemporaryDirectory() as temporary:
            with zipfile.ZipFile(package) as archive:
                archive.extractall(temporary)
            return validate_package(Path(temporary), max_data_bytes)

    manifest_path = package / "manifest.json"
    if not manifest_path.exists():
        report.fail("package_manifest_invalid", "manifest.json отсутствует.")
        return report

    if manifest_path.stat().st_size > LIMITS["manifest_bytes"]:
        report.fail("package_limit_exceeded", "manifest.json больше 1 MiB.")
        return report

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("format") != "dataarena.package":
        report.fail("package_format_unknown", f"Неизвестный format: {manifest.get('format')!r}.")
        return report

    raw_version = str(manifest.get("format_version", ""))
    try:
        major, minor = (int(part) for part in raw_version.split(".")[:2])
    except ValueError:
        report.fail("package_manifest_invalid", f"Некорректный format_version: {raw_version!r}.")
        return report

    if major != SUPPORTED_MAJOR:
        report.fail(
            "package_version_unsupported",
            f"Понимается dataarena.package версии {SUPPORTED_MAJOR}.x, в пакете {raw_version}.",
        )
        return report

    if minor > 0:
        report.warn(f"Пакет собран более новой minor-версией ({raw_version}); незнакомые поля игнорируются.")

    for error in load_schema("manifest.schema.json").iter_errors(manifest):
        report.fail("package_manifest_invalid", error.message)
    if not report.ok:
        return report

    #объявленный дважды путь: манифест противоречит сам себе, и неизвестно, какому хешу верить
    part_paths = [entry["path"] for entry in manifest.get("parts", [])]
    duplicates = {path for path in part_paths if part_paths.count(path) > 1}
    if duplicates:
        report.fail("package_duplicate_entry", f"Путь объявлен в parts несколько раз: {sorted(duplicates)}.")
        return report

    missing_from_parts = [path for path in manifest["data"]["files"] if path not in part_paths]
    if part_paths and missing_from_parts:
        report.fail(
            "package_conflicting_entry",
            f"Файлы данных отсутствуют в parts: {missing_from_parts}.",
        )
        return report

    #посторонние и исполняемые файлы
    declared = set(part_paths) | set(manifest["data"]["files"])
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        if relative == "manifest.json":
            continue
        if path.suffix.lower() in EXECUTABLE_SUFFIXES:
            report.fail("package_executable_payload", f"Исполняемая сериализация в пакете: {relative}.")
        elif relative not in declared:
            report.warn(f"Файл вне manifest.parts не читается: {relative}.")

    data = manifest["data"]
    if len(data["files"]) > LIMITS["data_parts"]:
        report.fail("package_limit_exceeded", "Слишком много частей данных.")
        return report

    metadata_bytes = sum(
        (package / name).stat().st_size for name in OPTIONAL_PARTS if (package / name).exists()
    ) + manifest_path.stat().st_size
    if metadata_bytes > LIMITS["metadata_total_bytes"]:
        report.fail("package_limit_exceeded", "Метаданные суммарно больше 64 MiB.")
        return report

    data_bytes = sum((package / name).stat().st_size for name in data["files"] if (package / name).exists())
    if data_bytes > max_data_bytes:
        report.fail(
            "package_limit_exceeded",
            f"Данные ({data_bytes} байт) больше лимита потребителя ({max_data_bytes} байт). "
            "Это ограничение потребителя, а не дефект пакета.",
        )
        return report

    #целостность всех объявленных файлов
    for entry in manifest.get("parts", []):
        path = package / entry["path"]
        if not path.exists():
            report.fail("package_part_missing", f"Файл объявлен, но отсутствует: {entry['path']}.")
            continue
        if sha256_file(path) != entry["sha256"]:
            report.fail("package_integrity_failed", f"sha256 не сошёлся: {entry['path']}.")
    if not report.ok:
        return report

    frames: list[pl.DataFrame] = []
    for relative in data["files"]:
        path = package / relative
        if not path.exists():
            report.fail("package_part_missing", f"Часть данных отсутствует: {relative}.")
            return report
        frames.append(pl.read_parquet(path))

    first_schema = frames[0].schema
    for relative, frame in zip(data["files"][1:], frames[1:], strict=False):
        if frame.schema != first_schema:
            report.fail("package_parts_schema_mismatch", f"Схема части {relative} отличается от первой.")
    if not report.ok:
        return report

    table = pl.concat(frames, how="vertical")

    if table.height == 0:
        report.fail("package_empty", "В пакете ноль строк.")
        return report

    if table.height != data["row_count"]:
        if data.get("row_count_exact", True):
            report.fail(
                "package_row_count_mismatch",
                f"Заявлено {data['row_count']} строк, фактически {table.height}.",
            )
        else:
            report.warn(f"row_count неточен: заявлено {data['row_count']}, фактически {table.height}.")

    fingerprint = data.get("content_fingerprint")
    if fingerprint:
        if fingerprint["algorithm"] != ALGORITHM:
            #незнакомый алгоритм — не мягкий случай: иначе это путь понижения,
            #пакет с чужим algorithm проходил бы вообще без проверки содержимого
            report.fail(
                "package_fingerprint_algorithm_unknown",
                f"Алгоритм отпечатка не поддерживается: {fingerprint['algorithm']}. "
                "Приём такого пакета возможен только по явному действию пользователя, "
                "и снимок обязан нести признак fingerprint_verified: false.",
            )
        elif content_fingerprint(table) != fingerprint["value"]:
            report.fail(
                "package_fingerprint_mismatch",
                "content_fingerprint не совпал с пересчитанным по данным.",
            )

    schema_payload = _validate_json_part(package, "schema.json", report)
    profile_payload = _validate_json_part(package, "profile.json", report)
    _validate_json_part(package, "lineage.json", report)
    _validate_json_part(package, "recipe.json", report)

    row_key = data.get("row_key")
    if row_key:
        missing = [name for name in row_key if name not in table.columns]
        if missing:
            report.fail("package_manifest_invalid", f"row_key ссылается на отсутствующие колонки: {missing}.")
        elif table.select(row_key).n_unique() != table.height:
            report.fail("package_manifest_invalid", "row_key не уникален: он не идентифицирует строку.")

    if schema_payload:
        #описание, не соответствующее данным, не должно читаться вовсе: дальше на него
        #опираются и потребитель, и человек, поэтому расхождение — отказ, а не предупреждение
        declared_columns = [column["name"] for column in schema_payload["columns"]]
        if declared_columns != table.columns:
            report.fail(
                "package_schema_mismatch",
                f"schema.json описывает колонки {declared_columns}, "
                f"а в parquet {table.columns}.",
            )
        else:
            for column in schema_payload["columns"]:
                actual = logical_type(table.schema[column["name"]])
                if column["logical_type"] != actual:
                    report.fail(
                        "package_schema_mismatch",
                        f"Колонка {column['name']}: schema.json объявляет "
                        f"logical_type {column['logical_type']}, в parquet {actual}.",
                    )
            for position, column in enumerate(schema_payload["columns"]):
                if column["position"] != position:
                    report.fail(
                        "package_schema_mismatch",
                        f"Колонка {column['name']}: position {column['position']} "
                        f"не совпадает с фактической {position}.",
                    )

    if profile_payload and profile_payload["computed_on"].get("sampled"):
        report.warn("Профиль посчитан по выборке: статистики приблизительны.")

    return report


def main() -> None:
    package = Path(sys.argv[1]).resolve()
    report = validate_package(package)

    print(f"пакет: {package.name}")
    for message in report.warnings:
        print(f"  warning: {message}")
    for code, message in report.errors:
        print(f"  ОТКАЗ [{code}]: {message}")
    print("  результат:", "ПРИНЯТ" if report.ok else "ОТВЕРГНУТ")
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
