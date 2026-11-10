"""Сборка `.dapkg` — пакета `dataarena.package/1` из датасета рабочей области.

Пакет — единственный мост между DataArena и ModelArena, и контракт заморожен. Поэтому
здесь нет ни одного поля «от себя»: состав, имена и смысл берутся из спецификации,
а отпечаток содержимого считается той же реализацией, что опубликована как эталон.

Три решения стоит объяснить.

**Происхождение — не украшение.** `lineage.json` собирается из настоящего конвейера:
исходник с его хэшем, затем по узлу на каждый шаг с числом строк после него. По этому
файлу видно, что именно было сделано с данными, а не «кто-то что-то сделал».

**Происхождение — одно на всех.** `lineage.json` не строится здесь заново: он
сериализует ответ того же `services.lineage.resolve`, что показывает интерфейс. У
собранного датасета в граф входят оба родителя в состоянии на момент сборки — из снимка,
а не из их нынешнего конвейера. Контракт `dataarena.package/1` заморожен, поэтому
история отдельных колонок в пакет пока не пишется: её расширение описано в запросе
на изменение контракта (`docs/contracts/CCR-001-lineage-columns.md`).

**Рецепт воспроизводим.** `recipe.json` содержит те же шаги в том же порядке и хэш
исходника. Применить его к новой версии файла — значит получить тот же результат
или честный отказ, если колонок больше нет.

**Пакет детерминирован.** Время сборки и время файлов в архиве фиксируются, порядок
файлов задан, JSON пишется с одинаковыми разделителями и сортировкой ключей. Две сборки
одного датасета дают одинаковый пакет — иначе сверить пакет с пакетом было бы нельзя.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from backend.adapters.package.fingerprint import ALGORITHM, content_fingerprint, logical_type
from backend.core.errors import AppError
from backend.domain.dataset.models import Dataset
from backend.domain.lineage.effects import ColumnCreate, ColumnDrop, ColumnRename
from backend.domain.lineage.models import (
    ColumnLineageEvent,
    ColumnOrigin,
    DatasetLineage,
    EventKind,
    JoinSide,
    ProvenanceStatus,
)
from backend.domain.pipeline.models import Step
from backend.services.export import safe_file_name
from backend.services.pipeline.operations import operation_for

PACKAGE_FORMAT = "dataarena.package"
#1.1.0 — MINOR по CCR-001: в lineage.json появились provenance и columns. Пакеты 1.0.0
#остаются валидными, и потребитель 1.0 читает 1.1 с предупреждением о более новой версии
PACKAGE_VERSION = "1.1.0"
#§H: предел части lineage.json. История колонок, не помещающаяся в него с запасом,
#не пишется — и об этом сказано в provenance.warnings
LINEAGE_PART_LIMIT = 16 * 1024 * 1024
LINEAGE_BUDGET = LINEAGE_PART_LIMIT - 256 * 1024
#предел имени пакета в манифесте и рецепте — из схемы контракта
MAX_PACKAGE_NAME = 200
#предел имени колонки в schema.json и profile.json — из схемы контракта
MAX_COLUMN_NAME = 300
#строк в одном файле данных. Разбиение нужно, чтобы большой датасет не становился
#одним файлом на гигабайты, который потребитель обязан прочитать целиком
ROWS_PER_PART = 250_000
#время в архиве фиксировано: иначе один и тот же пакет отличался бы байтами от сборки
#к сборке, и сверка пакетов перестала бы что-либо значить
ARCHIVE_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
#идентификатор источника в рецепте: формат задан контрактом
_ASCII_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PackageError(AppError):
    status_code = 422
    code = "package_failed"


@dataclass(frozen=True, slots=True)
class BuiltPackage:
    path: Path
    file_name: str
    package_id: str
    rows: int
    fingerprint: str


def build(  # noqa: PLR0913 - пакет собирается из датасета, данных, шагов и происхождения
    dataset: Dataset,
    frame: pl.DataFrame,
    steps: tuple[Step, ...],
    destination: Path,
    *,
    producer_version: str,
    lineage: DatasetLineage,
) -> BuiltPackage:
    """Собирает пакет на диске и возвращает путь к архиву."""
    too_long = [name for name in frame.columns if len(name) > MAX_COLUMN_NAME]

    if too_long:
        #схема контракта ограничивает имя колонки: пакет с таким именем не прошёл бы
        #проверку у потребителя. Сократить имя молча нельзя — оно указывало бы не на ту колонку
        raise PackageError(
            f"Имя колонки длиннее {MAX_COLUMN_NAME} символов не помещается в пакет. "
            "Переименуйте колонку шагом конвейера.",
            details={"dataset_id": dataset.dataset_id, "columns": [name[:50] + "…" for name in too_long[:5]]},
        )

    if frame.height == 0:
        #пустой пакет пройдёт проверку схемы и окажется бесполезным у потребителя.
        #Лучше отказать здесь, где видно почему
        raise PackageError(
            "В датасете нет строк: собирать пакет не из чего.",
            details={"dataset_id": dataset.dataset_id},
        )

    try:
        fingerprint = content_fingerprint(frame)
    except pl.exceptions.InvalidOperationError as error:
        #эталонный отпечаток приводит целые к Int64: UInt64 больше 2^63 в него не помещается.
        #Реализация отпечатка — часть замороженного контракта, её здесь не меняют
        raise PackageError(
            "В датасете есть целые больше 2^63 (UInt64): отпечаток пакета их не описывает. "
            "Приведите колонку к числу с плавающей точкой или к тексту.",
            details={"dataset_id": dataset.dataset_id},
        ) from error
    except ValueError as error:
        #тип без канонического представления обязан быть явным отказом, а не тихо
        #стать строкой: иначе два добросовестных пакета разошлись бы молча
        raise PackageError(str(error), details={"dataset_id": dataset.dataset_id}) from error

    root = destination / "package"
    (root / "data").mkdir(parents=True, exist_ok=True)
    created_at = _timestamp()
    package_id = f"pkg_{fingerprint[:16]}"

    data_files = _write_parts(frame, root)
    _write_json(root / "schema.json", _schema(dataset, frame))
    _write_json(root / "profile.json", _profile(dataset, frame, created_at))
    _write_json(
        root / "lineage.json",
        _lineage(dataset, frame, lineage, package_id, created_at),
    )
    _write_json(root / "recipe.json", _recipe(dataset, steps))

    parts = [
        {
            "path": relative,
            "bytes": (root / relative).stat().st_size,
            "sha256": _sha256(root / relative),
        }
        for relative in [*data_files, "schema.json", "profile.json", "lineage.json", "recipe.json"]
    ]

    _write_json(
        root / "manifest.json",
        _manifest(
            dataset,
            frame,
            data_files=data_files,
            parts=parts,
            package_id=package_id,
            created_at=created_at,
            fingerprint=fingerprint,
            producer_version=producer_version,
        ),
    )

    #путь архива на диске не зависит от имени датасета: имя приходит от пользователя,
    #и собранное из него «../../x.dapkg» уводило запись за пределы временного каталога.
    #Имя для скачивания отдельно и очищено
    file_name = safe_file_name(_package_name(dataset), "dapkg")
    archive = destination / "package.dapkg"
    _zip(root, archive)

    return BuiltPackage(
        path=archive,
        file_name=file_name,
        package_id=package_id,
        rows=frame.height,
        fingerprint=fingerprint,
    )


def _write_parts(frame: pl.DataFrame, root: Path) -> list[str]:
    files: list[str] = []

    for index, offset in enumerate(range(0, frame.height, ROWS_PER_PART)):
        relative = f"data/part-{index:04d}.parquet"
        frame.slice(offset, ROWS_PER_PART).write_parquet(root / relative, compression="zstd")
        files.append(relative)

    return files


def _schema(dataset: Dataset, frame: pl.DataFrame) -> dict[str, Any]:
    semantic = _current_semantic(dataset, frame)
    columns = []

    for position, (name, dtype) in enumerate(frame.schema.items()):
        series = frame[name]
        columns.append(
            {
                "name": name,
                "position": position,
                "physical_type": str(dtype),
                "logical_type": logical_type(dtype),
                #у колонки, появившейся в конвейере, исходного смыслового типа нет:
                #её смысл выводится из физического типа, а не выдумывается
                "semantic_type": semantic.get(name, _semantic_from(dtype)),
                "nullable": series.null_count() > 0,
                "unique": series.n_unique() == frame.height,
                "description": None,
                "source": {"dataset": dataset.name, "original_name": name},
            }
        )

    return {"format_version": PACKAGE_VERSION, "columns": columns}


def _semantic_from(dtype: pl.DataType) -> str:
    if dtype == pl.Boolean:
        return "boolean"

    if dtype.is_numeric():
        return "numeric"

    if isinstance(dtype, pl.Datetime) or dtype in (pl.Date, pl.Time):
        return "datetime"

    return "categorical"


def _current_semantic(dataset: Dataset, frame: pl.DataFrame) -> dict[str, str]:
    """Смысловой тип из схемы источника — только для колонок, чей тип шаги не сменили.

    Колонка, приведённая к тексту, не должна описываться в пакете как числовая.
    """
    return {
        column.name: column.semantic_type.value
        for column in dataset.schema.columns
        if column.name in frame.schema and str(frame.schema[column.name]) == column.physical_type
    }


def _profile(dataset: Dataset, frame: pl.DataFrame, created_at: str) -> dict[str, Any]:
    semantic = _current_semantic(dataset, frame)
    columns = []

    for name in frame.columns:
        series = frame[name]
        missing = int(series.null_count())
        unique = int(series.n_unique())
        entry: dict[str, Any] = {
            "name": name,
            "semantic_type": semantic.get(name, _semantic_from(frame.schema[name])),
            "missing_count": missing,
            "missing_ratio": missing / frame.height,
            "unique_count": unique,
            "unique_ratio": unique / frame.height,
            "is_constant": unique <= 1,
        }

        if frame.schema[name].is_numeric():
            #статистика — по конечным значениям: NaN и бесконечность дали бы в профиле
            #литералы NaN и Infinity, то есть файл, который не является строгим JSON
            finite = series.drop_nulls()
            finite = finite.filter(finite.is_finite()) if finite.dtype.is_float() else finite

            if finite.len() > 0:
                entry["numeric_stats"] = {
                    "min": float(finite.min()),  # type: ignore[arg-type]
                    "max": float(finite.max()),  # type: ignore[arg-type]
                    "mean": float(finite.mean()),  # type: ignore[arg-type]
                    "median": float(finite.median()),  # type: ignore[arg-type]
                }

        columns.append(entry)

    return {
        "format_version": PACKAGE_VERSION,
        "computed_at": created_at,
        #профиль считается по всем строкам: выборочный профиль обязан помечаться,
        #и пока выборки нет, врать флагом нельзя
        "computed_on": {"rows": frame.height, "sampled": False},
        "dataset": {
            "name": dataset.name,
            "row_count": frame.height,
            "column_count": frame.width,
        },
        "columns": columns,
    }


def _lineage(
    dataset: Dataset,
    frame: pl.DataFrame,
    lineage: DatasetLineage,
    package_id: str,
    created_at: str,
) -> dict[str, Any]:
    """`lineage.json` из единого ответа о происхождении — в полях контракта.

    Здесь нет своей логики вывода происхождения: только перевод готового ответа
    в узлы графа. Всё, чего ответ не знает, здесь тоже не появляется.
    """
    artifact = lineage.artifact
    source_id = f"src_{dataset.dataset_id}"
    #у CSV и Parquet число строк при импорте не считается: считать его ради записи
    #в происхождение значило бы полный проход по файлу. «Неизвестно» здесь не пишется
    #ни нулём, ни null — поля просто нет, и это предусмотрено контрактом
    read_node: dict[str, Any] = {
        "id": "n0",
        "op": "read",
        "inputs": [],
        "source": source_id,
    }
    trace: list[dict[str, Any]] = []

    if dataset.row_count is not None:
        read_node["output_rows"] = dataset.row_count
        trace.append({"after": "n0", "rows": dataset.row_count})

    sources: list[dict[str, Any]] = [
        {
            "id": source_id,
            "name": artifact.file_name,
            "format": artifact.format,
            "bytes": artifact.bytes,
            "sha256": artifact.sha256,
            **({"row_count": dataset.row_count} if dataset.row_count is not None else {}),
            "column_count": len(dataset.schema.columns),
        }
    ]
    graph: list[dict[str, Any]] = []
    snapshots = [parent.snapshot for parent in lineage.parents if parent.snapshot is not None]
    join = lineage.provenance.join

    if join is not None and len(snapshots) == 2:  # noqa: PLR2004 - у соединения две стороны
        #датасет собран соединением: без этих узлов происхождение выглядело бы как чтение
        #файла, взявшегося ниоткуда. Родители — в состоянии на момент сборки, из снимка
        tails = []

        for snapshot in snapshots:
            prefix = "jl" if snapshot.role is JoinSide.LEFT else "jr"
            parent_source = f"src_{snapshot.dataset_id}"
            sources.append(
                {
                    "id": parent_source,
                    "name": snapshot.dataset_name or snapshot.dataset_id,
                    "format": snapshot.artifact_format,
                    "sha256": snapshot.artifact_sha256,
                    "kind": "dataset",
                }
            )
            graph.append({"id": f"{prefix}0", "op": "read", "inputs": [], "source": parent_source})

            for step in snapshot.steps:
                graph.append(
                    {
                        "id": f"{prefix}{step.position}",
                        "op": step.operation,
                        "inputs": [graph[-1]["id"]],
                        "params": step.params,
                    }
                )

            tails.append(graph[-1]["id"])

        graph.append(
            {
                "id": "j",
                "op": "join",
                "inputs": tails,
                "params": {
                    "kind": join.kind,
                    "keys": [{"left": left, "right": right} for left, right in join.keys],
                    "right_suffix": join.right_suffix,
                },
            }
        )
        read_node["inputs"] = ["j"]
    elif join is not None:
        read_node["note"] = "Собран соединением до появления снимков родителей: их состояние неизвестно."
    elif not lineage.provenance.is_upload:
        read_node["note"] = "Происхождение неизвестно: запись о создании отсутствует или повреждена."

    graph.append(read_node)

    for event in lineage.events:
        node = f"n{event.position}"
        entry: dict[str, Any] = {
            "id": node,
            "op": event.operation,
            "inputs": [f"n{event.position - 1}"],
            "params": event.params,
        }

        #число строк после промежуточного шага не считается заново: это стоило бы прохода
        #по данным на каждый шаг. Поле просто отсутствует — «неизвестно» не записывается
        #ни нулём, ни null: и то и другое потребитель прочитал бы как число
        if event.position == len(lineage.events):
            entry["output_rows"] = frame.height
            trace.append({"after": node, "rows": frame.height})

        graph.append(entry)

    document: dict[str, Any] = {
        "format_version": PACKAGE_VERSION,
        "package_id": package_id,
        "created_at": created_at,
        "sources": sources,
        "graph": graph,
        "row_count_trace": trace,
        "provenance": _provenance(lineage),
        "columns": _column_history(
            lineage,
            source_ids={source["id"] for source in sources},
            node_ids={node["id"] for node in graph},
        ),
    }

    if not _fits_contract(document["columns"]) or len(
        json.dumps(document, ensure_ascii=False).encode("utf-8")
    ) > LINEAGE_BUDGET:
        #история не помещается в предел части: пакет без неё лучше, чем пакет, который
        #потребитель отвергнет целиком. Пропуск назван, а история помечена неполной
        del document["columns"]
        document["provenance"]["warnings"].append("column_history_omitted")
        document["provenance"]["history"] = "incomplete"

    return document


#пределы полей columns из lineage.schema.json 1.1
MAX_TEXT = 10_000
MAX_COLUMNS = 10_000
MAX_ORIGINS = 1_000
MAX_EVENTS = 10_000
MAX_INPUTS = 100


def _clipped(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _fits_contract(columns: list[dict[str, Any]]) -> bool:
    """История укладывается в пределы схемы: иначе потребитель отверг бы пакет целиком.

    Имена колонок не сокращаются — сокращённое имя указывало бы не на ту колонку, — поэтому
    история с именем длиннее предела не пишется вовсе.
    """
    if len(columns) > MAX_COLUMNS:
        return False

    for column in columns:
        names = [column["name"], *(ref["column"] for ref in column["origins"])]
        names += [name for event in column["events"] for name in (*event["inputs"], event["output"])]

        if (
            any(len(name) > MAX_TEXT for name in names)
            or len(column["origins"]) > MAX_ORIGINS
            or len(column["events"]) > MAX_EVENTS
            or any(len(event["inputs"]) > MAX_INPUTS for event in column["events"])
        ):
            return False

    return True


def _provenance(lineage: DatasetLineage) -> dict[str, Any]:
    """`provenance` 1.1: известно ли происхождение и полна ли история — из ответа resolver."""
    provenance = lineage.provenance
    known = provenance.status is ProvenanceStatus.KNOWN

    return {
        "status": "known" if known else "unknown",
        "origin": provenance.origin.value if provenance.origin is not None else None,
        "unknown_reason": provenance.unknown_reason.value if provenance.unknown_reason else None,
        "history": "complete" if known and not lineage.warnings else "incomplete",
        "warnings": list(lineage.warnings),
    }


def _column_history(
    lineage: DatasetLineage, *, source_ids: set[str], node_ids: set[str]
) -> list[dict[str, Any]]:
    """`columns` 1.1 — история каждой колонки из ответа resolver, в порядке данных.

    Своего вывода здесь нет: события, источники и чтения берутся как есть. Добавляются
    только ссылки на узлы графа этого пакета там, где такой узел есть; событие прародителя,
    чьих узлов в пакете нет, описывается само. Порядок событий не меняется.
    """
    join = lineage.provenance.join
    sides = {item.name: item.side for item in join.columns} if join is not None else {}
    truncated = {
        parent.role
        for parent in lineage.parents
        if parent.snapshot is None or parent.snapshot.history_truncated
    }
    prefixes = {parent.dataset_id: ("jl" if parent.role is JoinSide.LEFT else "jr") for parent in lineage.parents}

    def node_of(event: ColumnLineageEvent) -> str | None:
        if event.kind is EventKind.JOIN:
            #узел «j» — соединение этого датасета; соединение родителя в графе не описано
            if event.dataset_id != lineage.dataset_id:
                return None

            candidate = "j"
        elif event.dataset_id == lineage.dataset_id:
            candidate = f"n{event.position}"
        elif event.dataset_id in prefixes:
            candidate = f"{prefixes[event.dataset_id]}{event.position}"
        else:
            return None

        return candidate if candidate in node_ids else None

    history = []

    for column in lineage.columns:
        joined_from = [sides.get(event.output) for event in column.events if event.kind is EventKind.JOIN]
        complete = (
            column.origin is not ColumnOrigin.UNKNOWN
            and bool(column.origins)
            and not any(side in truncated for side in joined_from if side is not None)
        )
        history.append(
            {
                "name": column.name,
                "origin": column.origin.value,
                "history": "complete" if complete else "incomplete",
                "origins": [
                    {
                        **ref.to_dict(),
                        **({"source": f"src_{ref.dataset_id}"} if f"src_{ref.dataset_id}" in source_ids else {}),
                    }
                    for ref in column.origins
                ],
                "events": [
                    {
                        **event.to_dict(),
                        #описание — текст для человека; параметры шага бывают длиннее
                        #предела строки, и целиком пакет из-за этого не отвергается
                        "description": _clipped(event.description),
                        **({"node": node} if (node := node_of(event)) is not None else {}),
                    }
                    for event in column.events
                ],
                "read_by": [f"n{position}" for position in column.read_by if f"n{position}" in node_ids],
            }
        )

    return history


def _required_columns(dataset: Dataset, steps: tuple[Step, ...]) -> list[str]:
    """Колонки источника, без которых рецепт не применить, — под именами в источнике.

    Прежде сюда попадало всё, что шаги читают, включая колонки, созданные или
    переименованные предыдущими шагами: после «a → b» и отбора по «b» рецепт требовал
    от источника колонку «b», которой в нём нет, и не применялся к тому самому файлу,
    из которого получен. Имена прослеживаются по эффектам шагов — тем же, что у происхождения.
    """
    #текущее имя -> имя в источнике; None — колонка создана шагом
    origin: dict[str, str | None] = {column.name: column.name for column in dataset.schema.columns}
    required: set[str] = set()

    for step in steps:
        operation = operation_for(step)

        for name in operation.reads(step.params):
            source = origin.get(name, name)

            if source is not None:
                required.add(source)

        match operation.effect(step.params):
            case ColumnRename(source=old, target=new):
                origin[new] = origin.pop(old, old)
            case ColumnCreate(output=output):
                origin[output] = None
            case ColumnDrop(columns=columns):
                for name in columns:
                    origin.pop(name, None)
            case _:
                pass

    return sorted(required)


def _recipe_alias(dataset: Dataset) -> str:
    """Имя источника внутри рецепта. Контракт требует латинского идентификатора.

    Псевдоним датасета в DataArena бывает кириллическим — он для SQL и для человека.
    В рецепте же имя связывает шаги с источником, и его формат задан спецификацией:
    подставить туда «клиенты» значит собрать пакет, который не пройдёт проверку
    у потребителя.
    """
    candidate = dataset.alias or dataset.name

    if _ASCII_ALIAS.match(candidate):
        return candidate

    return f"source_{dataset.dataset_id.removeprefix('ds_')[:16]}"


def _recipe(dataset: Dataset, steps: tuple[Step, ...]) -> dict[str, Any]:
    required = _required_columns(dataset, steps)
    alias = _recipe_alias(dataset)

    return {
        "format_version": PACKAGE_VERSION,
        "name": _package_name(dataset),
        "derived_from_lineage": False,
        "inputs": [
            {
                "alias": alias,
                #колонки, без которых рецепт не применится к новой версии файла:
                #ровно те, которые читают шаги
                "required_columns": required,
                "format_hint": dataset.source.format,
                "source_sha256": dataset.source.sha256,
            }
        ],
        "steps": [
            {"op": step.kind.value, "on": alias, "params": step.params} for step in steps
        ],
        "output": {"format": "parquet", "compression": "zstd"},
    }


def _manifest(  # noqa: PLR0913 - манифест собирается из всего сразу
    dataset: Dataset,
    frame: pl.DataFrame,
    *,
    data_files: list[str],
    parts: list[dict[str, Any]],
    package_id: str,
    created_at: str,
    fingerprint: str,
    producer_version: str,
) -> dict[str, Any]:
    identifiers = [
        column.name
        for column in dataset.schema.columns
        if column.semantic_type.value == "id" and column.name in frame.columns
    ]
    datetimes = [
        name
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Datetime) or dtype in (pl.Date, pl.Time)
    ]

    return {
        "format": PACKAGE_FORMAT,
        "format_version": PACKAGE_VERSION,
        "package_id": package_id,
        "name": _package_name(dataset),
        "created_at": created_at,
        "producer": {"name": "DataArena", "version": producer_version},
        "version": {
            "dataset_id": dataset.dataset_id,
            #версия пакета выводится из содержимого: один и тот же датасет даёт один
            #и тот же идентификатор версии, а изменённый — другой
            "version_id": f"dsv_{fingerprint[:16]}",
            "version_number": 1,
        },
        "data": {
            "files": data_files,
            "format": "parquet",
            "compression": "zstd",
            "row_count": frame.height,
            "row_count_exact": True,
            "column_count": frame.width,
            "bytes": sum(entry["bytes"] for entry in parts[: len(data_files)]),
            "content_fingerprint": {"algorithm": ALGORITHM, "value": fingerprint},
        },
        "parts": parts,
        "hints": {
            #подсказки описывают данные, а не задачу: выбор цели — дело ModelArena,
            #и DataArena о ней ничего не знает
            "identifier_columns": identifiers,
            "datetime_columns": datetimes,
            "excluded_by_user": [],
        },
    }


def _package_name(dataset: Dataset) -> str:
    #манифест и рецепт ограничивают имя двумястами символами (схема контракта), а имя
    #датасета бывает длиннее: без усечения пакет не проходил бы собственную проверку
    stem = dataset.name.rsplit(".", 1)[0] if "." in dataset.name else dataset.name
    stem = "".join(character for character in stem if character.isprintable()).strip()

    return stem[:MAX_PACKAGE_NAME] or dataset.dataset_id


def _timestamp() -> str:
    #один момент на обе части: секунды и миллисекунды от двух вызовов на границе секунды
    #давали время почти на секунду раньше настоящего
    now = datetime.now(UTC)

    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    #одинаковые разделители и порядок ключей: два одинаковых пакета обязаны совпасть байтами
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _zip(source: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        #manifest.json первым: потребитель читает его до всего остального и не должен
        #ради этого распаковывать весь архив
        ordered = ["manifest.json", *sorted(
            str(path.relative_to(source)).replace("\\", "/")
            for path in source.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        )]

        for relative in ordered:
            info = zipfile.ZipInfo(relative, ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (source / relative).read_bytes())
