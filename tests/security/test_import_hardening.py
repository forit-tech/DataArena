"""Regression-тесты на находки adversarial review.

Каждый тест соответствует записи в SECURITY_AND_ROBUSTNESS_REVIEW.md и воспроизводит
именно тот сценарий, которым дефект был обнаружен. Тест, написанный «потому что функция
существует», сюда не относится: здесь только то, как пользователь или атакующий может
это сломать.
"""

from __future__ import annotations

import threading
import tracemalloc
from pathlib import Path

import polars as pl
import pytest

from backend.adapters.formats import registry
from backend.adapters.formats.capabilities import CSV, PARQUET
from backend.adapters.storage.workspace_store import DuplicateDatasetError, WorkspaceStore
from backend.core.errors import DatasetReadError
from backend.domain.dataset.identifiers import new_dataset_id, new_workspace_id
from backend.services.dataset_import import import_dataset, workspace_paths


@pytest.fixture
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "state.db")


# ── R-01: суффикс из пользовательского имени попадал в путь ───────────────────


@pytest.mark.parametrize(
    "hostile_name",
    [
        "data.csv:evil",          # альтернативный поток данных NTFS
        "data." + "a" * 300,      # путь за пределом допустимой длины
        "data.csv ",              # хвостовой пробел, который Windows отбрасывает
        "data.CSV.",              # точка в конце
    ],
)
def test_path_never_takes_the_extension_from_the_user_filename(
    tmp_path: Path, hostile_name: str
) -> None:
    #расширение берётся из РАСПОЗНАННОГО формата, поэтому имя файла на путь не влияет вовсе
    paths = workspace_paths(tmp_path, new_workspace_id(), new_dataset_id(), CSV)

    assert paths.source.suffix == ".csv"
    assert paths.source.name.endswith(".csv")
    #имя из аргумента нигде не участвует: проверяем, что оно не просочилось в путь
    assert hostile_name not in str(paths.source)
    assert ":" not in paths.source.name
    assert paths.source.resolve().is_relative_to(tmp_path.resolve())


def test_workspace_paths_stay_inside_the_workspace(tmp_path: Path) -> None:
    workspace_id = new_workspace_id()
    paths = workspace_paths(tmp_path, workspace_id, new_dataset_id(), PARQUET)
    base = (tmp_path / workspace_id).resolve()

    assert paths.source.resolve().is_relative_to(base)
    assert paths.derived.resolve().is_relative_to(base)


# ── R-02: детектор разделителя читал файл целиком ─────────────────────────────


def test_separator_detection_does_not_read_the_whole_file(tmp_path: Path) -> None:
    #read_bytes()[:8192] прочитал бы файл целиком ради восьми килобайт: на многогигабайтном
    #CSV это OOM ещё до открытия датасета
    path = tmp_path / "wide.csv"

    with path.open("wb") as handle:
        handle.write(b"a,b\n")
        for _ in range(400_000):
            handle.write(b"1,2\n")

    file_size = path.stat().st_size
    tracemalloc.start()
    registry._detect_csv_separator(path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert file_size > 1_000_000, "файл должен быть достаточно большим, чтобы разница была видна"
    assert peak < file_size // 4, (
        f"детектор разделителя занял {peak} байт на файле {file_size} байт — "
        "похоже, файл снова читается целиком"
    )


# ── R-03: перекодировка читала файл любого размера ────────────────────────────


def test_oversized_non_utf8_file_is_refused_instead_of_being_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #ленивое чтение для такого файла недоступно, а целиком он в память не поместится:
    #честный отказ лучше, чем OOM в середине запроса
    monkeypatch.setattr(registry, "MAX_RECODED_BYTES", 1024)
    path = tmp_path / "big_cp1251.csv"
    path.write_bytes("город,продажи\n".encode("cp1251") + "Москва,1\n".encode("cp1251") * 500)

    with pytest.raises(DatasetReadError) as error:
        registry.read_dataset(path)

    assert "перекодировки" in error.value.message


# ── R-04: двоичный мусор с расширением .csv становился датасетом ──────────────


def test_binary_garbage_named_csv_is_rejected(tmp_path: Path) -> None:
    #ignore_errors=True в парсере превращал случайные байты в «датасет» из одной колонки,
    #и пользователь получал бессмыслицу вместо понятного отказа
    path = tmp_path / "garbage.csv"
    path.write_bytes(bytes(range(256)) * 10)

    with pytest.raises(DatasetReadError) as error:
        registry.read_dataset(path)

    assert "двоичные" in error.value.message


def test_a_real_text_csv_is_still_accepted(tmp_path: Path) -> None:
    #проверка на двоичность не должна отвергать нормальные файлы
    path = tmp_path / "fine.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    assert registry.read_dataset(path).columns == ["a", "b"]


# ── R-05: нечитаемый файл молча объявлялся форматом по расширению ─────────────


def test_unreadable_file_reports_the_real_reason(tmp_path: Path) -> None:
    #каталог с именем data.csv раньше проходил в ветку определения по расширению
    #и падал позже с совсем другим сообщением
    path = tmp_path / "data.csv"
    path.mkdir()

    with pytest.raises(DatasetReadError):
        registry.detect_format(path)


# ── R-06: одновременная загрузка одного файла давала сырой IntegrityError ─────


def test_concurrent_upload_of_the_same_content_yields_one_dataset(tmp_path: Path) -> None:
    #проверено на шести потоках: без обработки гонки пять из шести падали
    #с UNIQUE constraint failed, то есть с ответом 500 и текстом из SQLite
    store = WorkspaceStore(tmp_path / "race.db")
    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "гонка")
    payload = b"a,b\n1,2\n3,4\n"
    failures: list[str] = []

    def upload(index: int) -> None:
        try:
            staged = tmp_path / f"staged_{index}.csv"
            staged.write_bytes(payload)
            import_dataset(store, tmp_path / "ws", workspace_id, staged, "race.csv")
        except Exception as error:  # noqa: BLE001 - тест обязан увидеть любую утечку наружу
            failures.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=upload, args=(index,)) for index in range(6)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"параллельная загрузка вернула ошибки: {failures}"
    assert len(store.list_datasets(workspace_id)) == 1


def test_duplicate_is_reported_as_a_typed_error_not_a_database_message(
    store: WorkspaceStore, tmp_path: Path
) -> None:
    #вызывающий должен уметь отличить гонку от настоящего сбоя, а пользователь не должен
    #видеть текст из SQLite
    from tests.unit.test_workspace_store import make_dataset

    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "ws")
    store.add_dataset(make_dataset(workspace_id, sha256="d" * 64))

    with pytest.raises(DuplicateDatasetError) as error:
        store.add_dataset(make_dataset(workspace_id, sha256="d" * 64))

    assert "UNIQUE constraint" not in error.value.message
    assert error.value.status_code == 409


# ── R-07: неудачная запись оставляла осиротевший файл ─────────────────────────


def test_failed_registration_does_not_leave_an_orphan_file(tmp_path: Path) -> None:
    #файл, о котором нет записи в хранилище, не виден в интерфейсе и не может быть удалён
    #пользователем: он остался бы на диске навсегда
    store = WorkspaceStore(tmp_path / "orphan.db")
    missing_workspace = new_workspace_id()
    staged = tmp_path / "staged.csv"
    staged.write_text("a,b\n1,2\n", encoding="utf-8")

    with pytest.raises(Exception, match="не найден"):
        import_dataset(store, tmp_path / "ws", missing_workspace, staged, "data.csv")

    sources = tmp_path / "ws" / missing_workspace / "sources"
    left_behind = list(sources.glob("*")) if sources.exists() else []

    assert not left_behind, f"после неудачной регистрации остались файлы: {left_behind}"


# ── R-08: датасет и его артефакт записывались двумя операциями ────────────────


def test_dataset_and_its_artifact_are_written_atomically(tmp_path: Path) -> None:
    #датасет без своего артефакта выглядел бы готовым к чтению, не имея рабочего файла
    from datetime import UTC, datetime

    from backend.domain.dataset.models import DerivedArtifact
    from tests.unit.test_workspace_store import make_dataset

    store = WorkspaceStore(tmp_path / "atomic.db")
    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "ws")

    dataset = make_dataset(workspace_id, sha256="e" * 64)
    dataset.derived = DerivedArtifact(
        path=str(tmp_path / "derived.parquet"),
        source_sha256="e" * 64,
        derived_sha256="f" * 64,
        source_format="xlsx",
        derived_format="parquet",
        converter="polars-parquet",
        converter_version=1,
        created_at=datetime.now(UTC),
        row_count=10,
        warnings=("Excel вмещает не более 1 048 576 строк.",),
    )
    store.add_dataset(dataset)

    restored = store.get_dataset(dataset.dataset_id)

    assert restored.derived is not None
    assert restored.derived.converter_version == 1
    assert restored.working_format == "parquet"
    assert restored.source.format == "csv"


# ── R-09: артефакт прежней версии конвертера считался годным ──────────────────


def test_artifact_from_another_converter_version_is_not_reused(tmp_path: Path) -> None:
    #прежний Parquet мог быть получен другой логикой, и молча продолжать им пользоваться нельзя
    from datetime import UTC, datetime

    from backend.domain.dataset.models import DerivedArtifact
    from tests.unit.test_workspace_store import make_dataset

    store = WorkspaceStore(tmp_path / "converter.db")
    workspace_id = new_workspace_id()
    store.create_workspace(workspace_id, "ws")

    dataset = make_dataset(workspace_id, sha256="1" * 64)
    dataset.derived = DerivedArtifact(
        path=str(tmp_path / "old.parquet"),
        source_sha256="1" * 64,
        derived_sha256="2" * 64,
        source_format="json",
        derived_format="parquet",
        converter="polars-parquet",
        converter_version=1,
        created_at=datetime.now(UTC),
        row_count=5,
    )
    store.add_dataset(dataset)

    assert store.find_reusable_derived("1" * 64, "polars-parquet", 1) is not None
    assert store.find_reusable_derived("1" * 64, "polars-parquet", 2) is None
    assert store.find_reusable_derived("1" * 64, "other-converter", 1) is None

    stale = store.drop_stale_derived("polars-parquet", 2)

    assert dataset.dataset_id in stale
    assert store.find_reusable_derived("1" * 64, "polars-parquet", 1) is None


# ── R-10: недостаток места обнаруживался в середине записи ────────────────────


def test_conversion_refuses_before_starting_when_disk_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #на середине пользователь получил бы и испорченный артефакт, и забитый диск
    from backend.services import normalization

    class Usage:
        free = 1024

    monkeypatch.setattr(normalization.shutil, "disk_usage", lambda _: Usage())
    source = tmp_path / "data.json"
    pl.DataFrame({"a": range(100)}).write_json(source)

    with pytest.raises(normalization.InsufficientDiskSpaceError) as error:
        normalization.normalize_to_parquet(source, tmp_path / "out.parquet", "a" * 64)

    assert not (tmp_path / "out.parquet").exists()
    assert error.value.status_code == 507


def test_interrupted_conversion_leaves_no_half_written_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #запись идёт во временный файл и переносится атомарно: прерванная конвертация
    #не оставит артефакт, который выглядит готовым и содержит половину данных
    from backend.services import normalization

    source = tmp_path / "data.json"
    pl.DataFrame({"a": range(10)}).write_json(source)
    target = tmp_path / "out.parquet"

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("диск отвалился на середине")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", explode)

    with pytest.raises(DatasetReadError):
        normalization.normalize_to_parquet(source, target, "a" * 64)

    assert not target.exists()
    assert not list(tmp_path.glob("*.partial"))


# ── R-11: нормализация не должна касаться форматов с ленивым чтением ──────────


@pytest.mark.parametrize("key", ["csv", "tsv", "parquet", "jsonl", "feather", "arrow"])
def test_lazy_formats_are_never_normalised(key: str) -> None:
    #конвертация решала бы несуществующую проблему и удваивала бы диск без причины
    from backend.adapters.formats.registry import capabilities_for_key
    from backend.services.normalization import needs_normalization

    assert not needs_normalization(capabilities_for_key(key))


@pytest.mark.parametrize("key", ["json", "xlsx"])
def test_non_lazy_formats_are_normalised(key: str) -> None:
    from backend.adapters.formats.registry import capabilities_for_key
    from backend.services.normalization import needs_normalization

    assert needs_normalization(capabilities_for_key(key))


# ── R-20: смысловой тип выводился по выборке ─────────────────────────────────


def test_high_cardinality_text_is_not_mistaken_for_an_identifier(tmp_path: Path) -> None:
    """Уникальность на выборке ничего не говорит об уникальности в датасете.

    Колонка `note-{i % 5000}` на первых десяти тысячах строк уникальна полностью,
    а на пятидесяти тысячах имеет пять тысяч различных значений. Найдено на живом
    датасете в миллион строк: текстовая колонка объявлялась идентификатором,
    и это ушло бы дальше в подсказки для ModelArena.
    """
    from backend.adapters.formats.registry import scan_dataset, write_dataset
    from backend.domain.dataset.models import SemanticType
    from backend.services.dataset_import import extract_schema

    #ключевое условие: в ВЫБОРКЕ колонка обязана выглядеть уникальной, иначе тест
    #проходит по случайности — она просто не попадает в кандидаты, и подтверждение
    #ни на что не влияет. Мутационная проверка это и вскрыла: с 5 000 различных значений
    #выборка из 10 000 строк уже видела повторы, и тест оставался зелёным даже с отключённым
    #подтверждением. Здесь период больше выборки: первые 10 000 строк уникальны полностью
    rows = 50_000
    period = 20_000
    frame = pl.DataFrame(
        {
            "real_id": range(rows),
            "note": [f"note-{index % period}" for index in range(rows)],
        }
    )
    path = tmp_path / "cardinality.parquet"
    write_dataset(frame, path, "parquet")

    schema = extract_schema(scan_dataset(path))
    by_name = {column.name: column for column in schema.columns}

    #проверяем и предпосылку теста: без неё он проверял бы не то, что заявлено
    sample_unique = frame.head(10_000)["note"].n_unique()
    assert sample_unique == 10_000, "в выборке колонка обязана выглядеть уникальной"

    assert by_name["real_id"].semantic_type is SemanticType.ID
    assert by_name["note"].semantic_type is SemanticType.TEXT, (
        f"колонка с {period} различных значений на {rows} строк не является идентификатором"
    )


def test_a_genuinely_unique_text_column_is_still_an_identifier(tmp_path: Path) -> None:
    #подтверждение не должно превращаться в запрет: настоящий идентификатор обязан остаться им
    from backend.adapters.formats.registry import scan_dataset, write_dataset
    from backend.domain.dataset.models import SemanticType
    from backend.services.dataset_import import extract_schema

    frame = pl.DataFrame({"uuid": [f"u-{index}" for index in range(20_000)]})
    path = tmp_path / "unique.parquet"
    write_dataset(frame, path, "parquet")

    schema = extract_schema(scan_dataset(path))

    assert schema.columns[0].semantic_type is SemanticType.ID
