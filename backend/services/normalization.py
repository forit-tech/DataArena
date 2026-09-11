"""Нормализация исходного файла в рабочий Parquet.

Решение и его цена
------------------
Форматы делятся на два класса: те, что читаются лениво (`scan_*`), и те, что нет.
JSON и XLSX относятся ко вторым — их нельзя открыть постранично, только целиком.

Нормализация выполняется **только для них**. CSV, TSV, Parquet, JSONL, Feather и Arrow
конвертировать не нужно: это решало бы несуществующую проблему и удваивало бы диск
без причины.

Цена решения — дублирование данных на диске для JSON и XLSX. Она принята сознательно,
взамен получаем одинаковое поведение всех форматов на каждом слое выше: пагинация,
профилирование, фильтрация, SQL и лимиты памяти не содержат ни одного `if format == xlsx`.

Что здесь не реализовано
------------------------
Прогресс конвертации и её отмена требуют слоя фоновых задач, которого пока нет:
конвертация выполняется синхронно в рамках загрузки. Состояние датасета при этом
честное — `NORMALIZING`, затем `READY` или `NORMALIZATION_FAILED`, — но полосы
прогресса и кнопки «отменить» не будет, пока не появится job-слой.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from backend.adapters.formats.capabilities import FormatCapabilities
from backend.adapters.formats.registry import detect_format, read_dataset
from backend.core.errors import AppError, DatasetReadError
from backend.core.logging import get_logger
from backend.domain.dataset.models import DerivedArtifact

CONVERTER_NAME = "polars-parquet"
#версия логики нормализации. Её изменение делает ранее собранные артефакты недействительными:
#прежний Parquet мог быть получен другой логикой, и молча продолжать им пользоваться нельзя
CONVERTER_VERSION = 1
DERIVED_FORMAT = "parquet"
#конвертация читает исходник целиком, поэтому под неё нужно место и под результат, и под запас
#коэффициент грубый и намеренно щедрый: отказать заранее лучше, чем упасть на середине записи
FREE_SPACE_FACTOR = 3


class InsufficientDiskSpaceError(AppError):
    #отдельный код: пользователю нужно понять, что дело в диске, а не в файле
    status_code = 507
    code = "insufficient_disk_space"


def needs_normalization(capabilities: FormatCapabilities) -> bool:
    #единственный критерий — отсутствие ленивого чтения
    #нормализация нужна там, где она решает проблему доступа, а не «для единообразия»
    return not capabilities.supports_lazy_scan


def ensure_free_space(target_directory: Path, source_bytes: int) -> None:
    #эта функция отказывает до начала тяжёлой работы, а не в середине записи
    #на середине пользователь получил бы и испорченный артефакт, и забитый диск
    target_directory.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(target_directory).free
    required = source_bytes * FREE_SPACE_FACTOR

    if free_bytes < required:
        raise InsufficientDiskSpaceError(
            "Недостаточно места на диске для нормализации файла. "
            f"Нужно около {required // (1024 * 1024)} МБ, свободно {free_bytes // (1024 * 1024)} МБ.",
            details={"required_bytes": required, "free_bytes": free_bytes},
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def normalize_to_parquet(
    source_path: Path,
    derived_path: Path,
    source_sha256: str,
    capabilities: FormatCapabilities | None = None,
) -> DerivedArtifact:
    #эта функция превращает нечитаемый лениво файл в рабочий Parquet
    #исходный файл не изменяется и не удаляется: он остаётся единственным источником истины
    #о том, что загрузил пользователь, и переживает любые пересборки артефакта
    logger = get_logger()
    format_capabilities = capabilities or detect_format(source_path)

    ensure_free_space(derived_path.parent, source_path.stat().st_size)

    logger.info(
        "Нормализация в Parquet",
        extra={
            "source_format": format_capabilities.key,
            "source_bytes": source_path.stat().st_size,
            "converter_version": CONVERTER_VERSION,
        },
    )

    frame = read_dataset(source_path, format_capabilities)
    #запись во временный файл рядом и атомарная замена: прерванная конвертация не оставит
    #artefact, который выглядит готовым, но содержит половину данных
    temporary_path = derived_path.with_suffix(derived_path.suffix + ".partial")
    temporary_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        frame.write_parquet(temporary_path, compression="zstd")
        temporary_path.replace(derived_path)
    except Exception as error:
        temporary_path.unlink(missing_ok=True)
        raise DatasetReadError(
            f"Не удалось нормализовать файл «{source_path.name}»: {str(error).splitlines()[0][:200]}"
        ) from error

    artifact = DerivedArtifact(
        path=str(derived_path),
        source_sha256=source_sha256,
        derived_sha256=_sha256_file(derived_path),
        source_format=format_capabilities.key,
        derived_format=DERIVED_FORMAT,
        converter=CONVERTER_NAME,
        converter_version=CONVERTER_VERSION,
        created_at=datetime.now(UTC),
        row_count=frame.height,
        #предупреждения берутся из объявленных возможностей исходного формата:
        #если XLSX ограничен числом строк, пользователь должен знать об этом и после конвертации
        warnings=format_capabilities.warnings,
    )

    logger.info(
        "Нормализация завершена",
        extra={
            "rows": artifact.row_count,
            "derived_bytes": derived_path.stat().st_size,
            "source_format": artifact.source_format,
        },
    )

    return artifact
