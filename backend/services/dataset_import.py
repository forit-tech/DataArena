"""Импорт файла в workspace: сохранение, нормализация, регистрация.

Порядок шагов выбран так, чтобы неудача на любом из них оставляла систему в понятном
состоянии, а не в половинчатом:

1. определяется формат и проверяется, что содержимое соответствует заявленному;
2. файл переносится в `sources/` под именем, собранным из идентификатора датасета;
3. если формат не читается лениво — собирается рабочий Parquet в `derived/`;
4. датасет и его артефакт записываются в хранилище **одной транзакцией**.

Неудача на шаге 3 не отменяет шаги 1–2: датасет существует, исходник цел, состояние
`normalization_failed` несёт причину. Потерять загруженный файл из-за сбоя конвертации
было бы худшим из возможных поведений.

Неудача на шаге 4 отменяет шаг 2: файл, о котором нет записи, удаляется, иначе в `sources/`
копились бы осиротевшие файлы, которых никто не видит и не может удалить через интерфейс.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from backend.adapters.formats.capabilities import FormatCapabilities
from backend.adapters.formats.registry import detect_upload_format, scan_dataset
from backend.adapters.storage.workspace_store import DuplicateDatasetError, WorkspaceStore
from backend.core.errors import AppError
from backend.core.logging import get_logger
from backend.domain.dataset.identifiers import ensure_workspace_id, new_dataset_id
from backend.domain.dataset.models import (
    ColumnSchema,
    Dataset,
    DatasetSchema,
    DatasetSource,
    DatasetStatus,
    DerivedArtifact,
    LogicalType,
    SemanticType,
)
from backend.services import normalization

#порог, начиная с которого почти уникальная колонка считается идентификатором
ID_UNIQUE_RATIO_THRESHOLD = 0.98
#детекция ID бессмысленна на очень коротких таблицах, где почти любая колонка уникальна
MIN_ROWS_FOR_ID_DETECTION = 10
#колонка считается категориальной либо по абсолютному числу значений, либо по их доле:
#относительный порог сам по себе врёт на коротких таблицах, абсолютный — на длинных
MAX_CATEGORICAL_VALUES = 50
MAX_CATEGORICAL_RATIO = 0.2
#колонка-идентификатор практически не имеет пропусков: одиночный null допускается,
#но колонка с заметной долей пустых значений идентификатором быть не может
MAX_ID_NULL_RATIO = 0.01
#схема и типы выводятся по выборке: полный проход по файлу ради этого не нужен
SCHEMA_SAMPLE_ROWS = 10_000

_LOGICAL_BY_DTYPE: tuple[tuple[object, LogicalType], ...] = (
    (pl.Boolean, LogicalType.BOOLEAN),
    (pl.Date, LogicalType.DATE),
    (pl.Time, LogicalType.TIME),
    (pl.Binary, LogicalType.BINARY),
)
_INTEGER_DTYPES = (
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
)


@dataclass(frozen=True, slots=True)
class ImportPaths:
    #раскладка workspace на диске; все пути собираются из проверенных идентификаторов
    source: Path
    derived: Path


def workspace_paths(
    root: Path, workspace_id: str, dataset_id: str, capabilities: FormatCapabilities
) -> ImportPaths:
    #расширение берётся из РАСПОЗНАННОГО формата, а не из имени, которое прислал пользователь
    #
    #иначе имя вида «data.csv:evil» дало бы суффикс «.csv:evil», и на NTFS запись пошла бы
    #в альтернативный поток данных файла — проверено, суффикс доходил до пути без изменений.
    #Имя «data.<300 символов>» дало бы путь за пределом длины пути.
    ensure_workspace_id(workspace_id)
    base = root / workspace_id
    extension = capabilities.extensions[0]

    return ImportPaths(
        source=base / "sources" / f"{dataset_id}{extension}",
        derived=base / "derived" / f"{dataset_id}.parquet",
    )


def sha256_file(path: Path) -> str:
    #потоковый хеш: файл не загружается в память целиком ради отпечатка
    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def logical_type_of(dtype: pl.DataType) -> LogicalType:  # noqa: PLR0911 - диспетчер типов
    for candidate, logical in _LOGICAL_BY_DTYPE:
        if dtype == candidate:
            return logical

    if dtype in _INTEGER_DTYPES:
        return LogicalType.INTEGER
    if dtype in (pl.Float32, pl.Float64):
        return LogicalType.FLOAT
    if isinstance(dtype, pl.Decimal):
        return LogicalType.DECIMAL
    if isinstance(dtype, pl.Datetime):
        return LogicalType.DATETIME
    if isinstance(dtype, pl.Duration):
        return LogicalType.DURATION

    return LogicalType.STRING


def semantic_type_of(
    logical: LogicalType,
    unique_count: int,
    row_count: int,
    null_ratio: float,
) -> SemanticType:
    #смысловой тип выводится ТОЛЬКО из данных: имя колонки в решение не входит, потому что
    #по имени «runtime» и «update_flag» прежняя эвристика AutoDataAnalysis объявляла датами
    if logical is LogicalType.BOOLEAN:
        return SemanticType.BOOLEAN

    if logical in (LogicalType.DATE, LogicalType.DATETIME, LogicalType.TIME):
        return SemanticType.DATETIME

    unique_ratio = unique_count / row_count if row_count else 0.0

    if (
        row_count >= MIN_ROWS_FOR_ID_DETECTION
        and unique_ratio >= ID_UNIQUE_RATIO_THRESHOLD
        and null_ratio <= MAX_ID_NULL_RATIO
        and logical in (LogicalType.INTEGER, LogicalType.STRING)
    ):
        return SemanticType.ID

    if logical in (LogicalType.INTEGER, LogicalType.FLOAT, LogicalType.DECIMAL):
        return SemanticType.NUMERIC

    #абсолютный порог защищает короткие таблицы: на 30 строках доля 0.2 дала бы шесть
    #категорий, и колонка из десяти значений считалась бы текстом
    if unique_count <= MAX_CATEGORICAL_VALUES or unique_ratio <= MAX_CATEGORICAL_RATIO:
        return SemanticType.CATEGORICAL

    return SemanticType.TEXT


def extract_schema(lazy: pl.LazyFrame) -> DatasetSchema:
    """Извлекает схему: типы по выборке, признак идентификатора — по всей колонке.

    Типы и категориальность на выборке из десяти тысяч строк определяются так же, как
    на всём файле, и полный проход ради них не нужен.

    Идентификатор — исключение. Уникальность на выборке ничего не говорит об уникальности
    в датасете: колонка `note-{i % 50000}` на первых десяти тысячах строк уникальна
    полностью, а на миллионе имеет пятьдесят тысяч различных значений. Проверено на живом
    датасете — текстовая колонка объявлялась идентификатором, и это ушло бы дальше
    в подсказки для ModelArena.

    Поэтому кандидаты в идентификаторы, отобранные по выборке, подтверждаются точным
    подсчётом по всей колонке. Считаются только кандидаты: их обычно единицы, и стоимость
    не зависит от ширины таблицы.
    """
    sample = lazy.head(SCHEMA_SAMPLE_ROWS).collect()
    sample_rows = sample.height
    draft: list[tuple[str, pl.DataType, LogicalType, SemanticType, bool]] = []

    for name, dtype in sample.schema.items():
        series = sample[name]
        logical = logical_type_of(dtype)
        null_ratio = series.null_count() / sample_rows if sample_rows else 0.0
        semantic = semantic_type_of(logical, series.n_unique(), sample_rows, null_ratio)
        draft.append((name, dtype, logical, semantic, series.null_count() > 0))

    candidates = [name for name, _, _, semantic, _ in draft if semantic is SemanticType.ID]
    confirmed = _confirm_identifier_columns(lazy, candidates) if candidates else set()

    columns = [
        ColumnSchema(
            name=name,
            position=position,
            physical_type=str(dtype),
            logical_type=logical,
            semantic_type=(
                semantic
                if semantic is not SemanticType.ID
                else (SemanticType.ID if name in confirmed else _fallback_semantic(logical))
            ),
            nullable=nullable,
        )
        for position, (name, dtype, logical, semantic, nullable) in enumerate(draft)
    ]

    return DatasetSchema(columns=tuple(columns))


def _confirm_identifier_columns(lazy: pl.LazyFrame, candidates: list[str]) -> set[str]:
    #точный подсчёт уникальных значений по всей колонке: только он отвечает на вопрос,
    #является ли колонка идентификатором. Один план на всех кандидатов сразу — Polars
    #прочитает файл один раз и посчитает нужные колонки за проход
    totals = lazy.select(
        [pl.len().alias("__rows__"), *[pl.col(name).n_unique().alias(name) for name in candidates]]
    ).collect()

    row_count = int(totals["__rows__"][0])

    if row_count < MIN_ROWS_FOR_ID_DETECTION:
        return set()

    return {
        name
        for name in candidates
        if int(totals[name][0]) / row_count >= ID_UNIQUE_RATIO_THRESHOLD
    }


def _fallback_semantic(logical: LogicalType) -> SemanticType:
    #кандидат не подтвердился: колонка получает тип, который следовал бы из данных,
    #если бы её вовсе не заподозрили в идентификаторах
    if logical in (LogicalType.INTEGER, LogicalType.FLOAT, LogicalType.DECIMAL):
        return SemanticType.NUMERIC

    return SemanticType.TEXT


def import_dataset(
    store: WorkspaceStore,
    workspace_root: Path,
    workspace_id: str,
    stored_file: Path,
    original_name: str,
) -> Dataset:
    #эта функция регистрирует уже сохранённый файл как датасет workspace
    #сам файл записан вызывающим потоково с проверкой лимита и больше не изменяется
    logger = get_logger()
    source_sha256 = sha256_file(stored_file)

    existing = store.find_dataset_by_content(workspace_id, source_sha256)

    if existing is not None:
        logger.info(
            "Повторная загрузка того же содержимого",
            extra={"dataset_id": existing.dataset_id, "workspace_id": workspace_id},
        )
        return existing

    #у принятого файла нет собственного расширения: подсказкой служит имя от клиента,
    #проверенное по белому списку, а сигнатура при её наличии важнее подсказки
    capabilities = detect_upload_format(stored_file, original_name)
    dataset_id = new_dataset_id()
    paths = workspace_paths(workspace_root, workspace_id, dataset_id, capabilities)
    paths.source.parent.mkdir(parents=True, exist_ok=True)
    stored_file.replace(paths.source)

    derived, status, reason = _prepare_working_artifact(store, paths, source_sha256, capabilities)
    schema, row_count = _describe(paths, derived, capabilities, status)

    dataset = Dataset(
        dataset_id=dataset_id,
        workspace_id=workspace_id,
        name=original_name,
        #настоящий псевдоним подбирает хранилище внутри транзакции: только там видно,
        #какие имена уже заняты, и только там подбор защищён от гонки
        alias="",
        source=DatasetSource(
            file_name=original_name,
            format=capabilities.key,
            bytes=paths.source.stat().st_size,
            sha256=source_sha256,
        ),
        schema=schema,
        row_count=row_count,
        created_at=datetime.now(UTC),
        status=status,
        status_reason=reason,
        derived=derived,
    )

    try:
        return store.add_dataset(dataset)
    except DuplicateDatasetError:
        #гонка: два запроса с одним содержимым прошли проверку выше одновременно, и уникальный
        #индекс отклонил второй. Это не ошибка пользователя — он загрузил тот же файл и должен
        #получить тот же датасет. Проверено: без этой ветки пять из шести параллельных
        #загрузок падали с сырым IntegrityError, то есть с ответом 500 и текстом из SQLite
        _discard_orphan_files(paths)
        winner = store.find_dataset_by_content(workspace_id, source_sha256)

        if winner is None:
            raise

        logger.info(
            "Параллельная загрузка того же содержимого разрешена в пользу первой",
            extra={"dataset_id": winner.dataset_id, "workspace_id": workspace_id},
        )
        return winner
    except Exception:
        #запись в хранилище не состоялась: файл, о котором нет записи, никому не виден
        #и не может быть удалён через интерфейс, поэтому убирается здесь
        _discard_orphan_files(paths)
        raise


def _discard_orphan_files(paths: ImportPaths) -> None:
    #файл, о котором нет записи в хранилище, не виден в интерфейсе и не может быть удалён
    #пользователем: он останется на диске навсегда, поэтому убирается здесь
    for path in (paths.source, paths.derived):
        Path(path).unlink(missing_ok=True)


def _prepare_working_artifact(
    store: WorkspaceStore,
    paths: ImportPaths,
    source_sha256: str,
    capabilities: FormatCapabilities,
) -> tuple[DerivedArtifact | None, DatasetStatus, str | None]:
    #эта функция готовит файл, из которого backend будет читать данные
    #для форматов с ленивым чтением она не делает ничего: конвертация решала бы
    #несуществующую проблему и удваивала бы диск без причины
    if not normalization.needs_normalization(capabilities):
        return None, DatasetStatus.READY, None

    reusable = store.find_reusable_derived(
        source_sha256, normalization.CONVERTER_NAME, normalization.CONVERTER_VERSION
    )

    if reusable is not None and Path(reusable.path).exists():
        #тот же исходник уже нормализован той же версией конвертера — переиспользуем
        return reusable, DatasetStatus.READY, None

    try:
        artifact = normalization.normalize_to_parquet(
            paths.source, paths.derived, source_sha256, capabilities
        )
    except AppError as error:
        #загрузка состоялась, исходник цел: датасет остаётся с понятной причиной,
        #а не исчезает вместе с файлом, который пользователь уже выбрал
        return None, DatasetStatus.NORMALIZATION_FAILED, error.message

    return artifact, DatasetStatus.READY, None


def _describe(
    paths: ImportPaths,
    derived: DerivedArtifact | None,
    capabilities: FormatCapabilities,
    status: DatasetStatus,
) -> tuple[DatasetSchema, int | None]:
    #схема снимается с того файла, из которого дальше будут читать данные
    if status is not DatasetStatus.READY:
        return DatasetSchema(columns=()), None

    if derived is not None:
        return extract_schema(scan_dataset(Path(derived.path))), derived.row_count

    return extract_schema(scan_dataset(paths.source, capabilities)), None
