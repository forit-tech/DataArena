"""Реестр форматов: определение, чтение и запись.

Определение формата идёт по расширению, но расширение — не доказательство содержимого.
Для бинарных форматов дополнительно проверяется сигнатура файла: `.csv`, оказавшийся
Excel-файлом, — обычная ситуация, а не экзотика (риск R-6).

Чтение по умолчанию ленивое. Форматы, для которых `scan_*` недоступен (JSON, XLSX),
нормализуются в Parquet один раз при загрузке — так ленивый путь становится доступен
для всех восьми форматов, а не только для колоночных.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import polars as pl

from backend.adapters.formats.capabilities import ALL_FORMATS, FormatCapabilities
from backend.core.errors import DatasetReadError, UnsupportedFormatError

_BY_EXTENSION: dict[str, FormatCapabilities] = {
    extension: capabilities for capabilities in ALL_FORMATS for extension in capabilities.extensions
}
_BY_KEY: dict[str, FormatCapabilities] = {capabilities.key: capabilities for capabilities in ALL_FORMATS}

#магические байты в начале файла: их наличие доказывает формат надёжнее расширения
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"PAR1", "parquet"),
    (b"ARROW1", "arrow"),
    (b"PK\x03\x04", "xlsx"),
)
#перебор кодировок и разделителей стоит дорого на больших файлах, поэтому схема выводится
#по ограниченной выборке строк
SCHEMA_INFERENCE_ROWS = 10_000
#пороги распознавания кириллицы: доля кириллических букв, при которой файл считается cp1251,
#и минимальное число таких букв — иначе одно случайное совпадение решало бы за весь файл
CYRILLIC_RATIO_THRESHOLD = 0.05
MIN_CYRILLIC_CHARACTERS = 3
#разделитель определяется по началу файла: читать больше незачем, а читать весь файл опасно
SEPARATOR_SAMPLE_BYTES = 8192
#перекодировка возможна только для файла, который целиком помещается в бюджет памяти:
#это единственный путь чтения, который нельзя сделать ленивым
MAX_RECODED_BYTES = 256 * 1024 * 1024
#текстовый формат с нулевыми байтами внутри — это не текстовый формат
NUL_SCAN_BYTES = 64 * 1024


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(_BY_EXTENSION))


def supported_formats_text() -> str:
    #одинаковый человекочитаемый список форматов для всех сообщений об ошибках
    return ", ".join(supported_extensions())


def capabilities_for_key(key: str) -> FormatCapabilities:
    capabilities = _BY_KEY.get(key)

    if capabilities is None:
        raise UnsupportedFormatError(
            f"Неизвестный формат «{key}». Поддерживаются: {', '.join(sorted(_BY_KEY))}.",
        )

    return capabilities


def detect_format(file_path: Path) -> FormatCapabilities:
    #эта функция определяет формат по сигнатуре, а при её отсутствии — по расширению
    #сигнатура важнее: она описывает содержимое, а расширение — только намерение того, кто назвал файл
    signature_key = _detect_by_signature(file_path)

    if signature_key is not None:
        return _BY_KEY[signature_key]

    suffix = file_path.suffix.lower()
    capabilities = _BY_EXTENSION.get(suffix)

    if capabilities is None:
        raise UnsupportedFormatError(
            f"Неподдерживаемый формат файла. Поддерживаются: {supported_formats_text()}.",
            details={"file_name": file_path.name, "supported": list(supported_extensions())},
        )

    return capabilities


def detect_upload_format(file_path: Path, declared_name: str) -> FormatCapabilities:
    """Определяет формат принятого файла, у которого нет собственного расширения.

    Файл в `staging/` намеренно назван без участия пользовательского имени — это защита
    от подстановки пути. Но текстовые форматы не имеют сигнатуры, и по такому имени
    их не отличить друг от друга, поэтому подсказкой служит имя, которое прислал клиент.

    Подсказка безопасна: `ensure_supported_extension` сверяет расширение с белым списком
    из десяти значений, и наружу выходит не пользовательская строка, а элемент этого
    списка. Сигнатура при этом важнее подсказки: файл, объявленный как `.csv`, но
    являющийся Parquet, будет прочитан как Parquet.
    """
    signature_key = _detect_by_signature(file_path)

    if signature_key is not None:
        return _BY_KEY[signature_key]

    return _BY_EXTENSION[ensure_supported_extension(declared_name)]


def ensure_text_format_is_text(file_path: Path, capabilities: FormatCapabilities) -> None:
    #эта функция отвергает двоичный мусор, названный .csv
    #без неё ignore_errors=True в парсере превращает случайные байты в «датасет» из одной
    #колонки, и пользователь получает бессмыслицу вместо понятного отказа
    if capabilities.key not in {"csv", "tsv", "json", "jsonl"}:
        return

    with file_path.open("rb") as source:
        sample = source.read(NUL_SCAN_BYTES)

    if b"\x00" in sample:
        raise DatasetReadError(
            f"Файл «{file_path.name}» объявлен как {capabilities.label}, но содержит двоичные "
            "данные. Проверьте, что это действительно текстовый файл.",
        )


def ensure_supported_extension(file_name: str) -> str:
    #эта функция отсекает очевидно посторонние загрузки до записи файла на диск
    #расширение — не гарантия содержимого, но проверка дешёвая и отбрасывает большую часть мусора
    suffix = Path(file_name).suffix.lower()

    if suffix not in _BY_EXTENSION:
        raise UnsupportedFormatError(
            f"Неподдерживаемый формат файла. Поддерживаются: {supported_formats_text()}.",
            details={"file_name": file_name, "supported": list(supported_extensions())},
        )

    return suffix


def scan_dataset(file_path: Path, capabilities: FormatCapabilities | None = None) -> pl.LazyFrame:
    #эта функция — единственная точка ленивого чтения во всём приложении
    #она возвращает LazyFrame: материализация остаётся осознанным решением конкретной операции,
    #а не побочным эффектом открытия файла
    resolved = file_path.resolve()
    format_capabilities = capabilities or detect_format(resolved)

    ensure_text_format_is_text(resolved, format_capabilities)

    if not format_capabilities.supports_lazy_scan:
        raise DatasetReadError(
            f"Формат {format_capabilities.label} не поддерживает ленивое чтение. "
            "Такие файлы нормализуются в Parquet при загрузке.",
        )

    try:
        if format_capabilities.key == "csv":
            return _scan_delimited(resolved, separator=_detect_csv_separator(resolved))
        if format_capabilities.key == "tsv":
            return _scan_delimited(resolved, separator="\t")
        if format_capabilities.key == "parquet":
            return pl.scan_parquet(resolved)
        if format_capabilities.key == "jsonl":
            return pl.scan_ndjson(resolved)
        return pl.scan_ipc(resolved)
    except DatasetReadError:
        raise
    except Exception as error:
        raise DatasetReadError(_describe_read_failure(resolved.name, error)) from error


def read_dataset(file_path: Path, capabilities: FormatCapabilities | None = None) -> pl.DataFrame:
    #эта функция читает файл целиком и используется там, где ленивый путь недоступен:
    #при нормализации JSON и XLSX в Parquet и в тестах
    resolved = file_path.resolve()
    format_capabilities = capabilities or detect_format(resolved)

    ensure_text_format_is_text(resolved, format_capabilities)

    try:
        if format_capabilities.key == "json":
            return _read_json(resolved)
        if format_capabilities.key == "xlsx":
            return _read_excel(resolved)

        return scan_dataset(resolved, format_capabilities).collect()
    except DatasetReadError:
        raise
    except Exception as error:
        raise DatasetReadError(_describe_read_failure(resolved.name, error)) from error


def write_dataset(frame: pl.DataFrame, file_path: Path, key: str) -> None:
    #эта функция пишет таблицу в выбранный формат
    #предупреждения о потерях формирует вызывающий по capabilities: здесь только запись
    capabilities = capabilities_for_key(key)

    if not capabilities.can_write:
        raise UnsupportedFormatError(f"Формат {capabilities.label} доступен только для чтения.")

    file_path.parent.mkdir(parents=True, exist_ok=True)

    if key == "csv":
        frame.write_csv(file_path)
    elif key == "tsv":
        frame.write_csv(file_path, separator="\t")
    elif key == "parquet":
        frame.write_parquet(file_path, compression="zstd")
    elif key == "json":
        frame.write_json(file_path)
    elif key == "jsonl":
        frame.write_ndjson(file_path)
    elif key == "xlsx":
        frame.write_excel(file_path)
    else:
        frame.write_ipc(file_path)


def _detect_by_signature(file_path: Path) -> str | None:
    #эта функция читает первые байты файла и сверяет их с известными сигнатурами
    try:
        with file_path.open("rb") as source:
            head = source.read(8)
    except OSError as error:
        #нечитаемый файл — это ошибка, а не «сигнатура не найдена»: тихий возврат None увёл бы
        #дальше по ветке определения формата и дал бы пользователю совсем другое сообщение
        raise DatasetReadError(
            f"Не удалось прочитать файл «{file_path.name}»: {error.strerror or error}"
        ) from error

    for signature, key in _SIGNATURES:
        if head.startswith(signature):
            return key

    return None


def _looks_like_utf8(file_path: Path, sample_bytes: int = 1024 * 1024) -> bool:
    #эта функция проверяет кодировку по началу файла, не читая его целиком
    #инкрементальный декодер не финализируется намеренно: иначе обрезанный на границе выборки
    #многобайтовый символ выглядел бы как ошибка кодировки на совершенно корректном файле
    import codecs

    with file_path.open("rb") as source:
        sample = source.read(sample_bytes)

    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False

    return True


def _scan_delimited(file_path: Path, separator: str) -> pl.LazyFrame:
    #эта функция открывает CSV или TSV лениво, а файлы не в UTF-8 переводит на путь
    #с перекодировкой: чтение целиком дороже, но иначе такой файл не открыть вовсе
    #
    #проверка кодировки выполняется ДО scan_csv намеренно: ленивое чтение не декодирует
    #содержимое при построении плана, поэтому ошибка кодировки всплыла бы только при первом
    #запросе страницы — далеко от места, где её можно объяснить пользователю
    if _looks_like_utf8(file_path):
        return pl.scan_csv(
            file_path,
            separator=separator,
            infer_schema_length=SCHEMA_INFERENCE_ROWS,
            ignore_errors=True,
            truncate_ragged_lines=True,
            try_parse_dates=True,
        )

    decoded = _decode_text_content(file_path)
    return pl.read_csv(
        io.StringIO(decoded),
        separator=separator,
        infer_schema_length=SCHEMA_INFERENCE_ROWS,
        ignore_errors=True,
        truncate_ragged_lines=True,
        try_parse_dates=True,
    ).lazy()


def _decode_text_content(file_path: Path) -> str:
    #эта функция декодирует текстовый файл, который не является UTF-8
    #выбор между cp1251 и cp1252 делается по доле кириллицы: иначе русские выгрузки Excel
    #превращаются в нечитаемые символы, а формально декодирование «удаётся» в обоих случаях
    size = file_path.stat().st_size

    if size > MAX_RECODED_BYTES:
        #ленивое чтение для этого файла недоступно, а целиком он в память не поместится:
        #честный отказ лучше, чем OOM в середине запроса
        raise DatasetReadError(
            f"Файл «{file_path.name}» не в UTF-8 и слишком велик для перекодировки "
            f"({size // (1024 * 1024)} МБ при пределе {MAX_RECODED_BYTES // (1024 * 1024)} МБ). "
            "Сохраните его в UTF-8 или в Parquet.",
        )

    content = file_path.read_bytes()

    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")

    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        #неудача здесь — не ошибка, а ответ «файл не в UTF-8», ради которого проверка и делается:
        #перебор кодировок построен на неудачных попытках, и следующая ветвь продолжает его.
        #Если не подойдёт ни одна, функция возбуждает DatasetReadError в конце — незамеченным
        #этот путь закончиться не может
        pass

    decoded: dict[str, str] = {}

    for encoding in ("cp1251", "cp1252", "latin-1"):
        try:
            decoded[encoding] = content.decode(encoding)
        except UnicodeDecodeError:
            continue

    cyrillic_text = decoded.get("cp1251")

    if cyrillic_text:
        letters = sum(character.isalpha() for character in cyrillic_text)
        cyrillic = sum("Ѐ" <= character <= "ӿ" for character in cyrillic_text)

        if (
            cyrillic >= MIN_CYRILLIC_CHARACTERS
            and letters > 0
            and cyrillic / letters >= CYRILLIC_RATIO_THRESHOLD
        ):
            return cyrillic_text

    for encoding in ("cp1252", "latin-1", "cp1251"):
        if encoding in decoded:
            return decoded[encoding]

    raise DatasetReadError("Не удалось определить текстовую кодировку файла.")


def _detect_csv_separator(file_path: Path) -> str:
    #эта функция смотрит начало файла и пытается понять, каким символом разделены колонки
    #при неудаче возвращается запятая как самый распространённый вариант
    #read_bytes()[:8192] прочитал бы файл целиком ради восьми килобайт: на многогигабайтном
    #CSV это гарантированный OOM ещё до открытия датасета
    with file_path.open("rb") as source:
        sample = source.read(SEPARATOR_SAMPLE_BYTES)

    text = ""

    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = sample.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if not text:
        return ","

    try:
        return csv.Sniffer().sniff(text, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _read_json(file_path: Path) -> pl.DataFrame:
    #эта функция читает JSON в двух популярных вариантах: массив объектов и newline-delimited
    #выбор варианта строится на неудачной первой попытке, а не на угадывании по содержимому
    try:
        return pl.read_json(file_path)
    except Exception:  # noqa: BLE001 - выбор варианта JSON строится на неудачной первой попытке
        return pl.read_ndjson(file_path)


def _read_excel(file_path: Path) -> pl.DataFrame:
    #эта функция читает первый лист и нормализует результат независимо от версии Polars:
    #для однокелоночного листа он может вернуть Series, а для книги — словарь листов
    result = pl.read_excel(file_path)

    if isinstance(result, pl.DataFrame):
        return result

    if isinstance(result, pl.Series):
        return result.to_frame()

    if isinstance(result, dict) and result:
        first = next(iter(result.values()))
        return first if isinstance(first, pl.DataFrame) else first.to_frame()

    raise DatasetReadError("Не удалось прочитать Excel-файл: лист не содержит таблицы.")


def _describe_read_failure(file_name: str, error: Exception) -> str:
    #эта функция превращает многострочные подсказки парсеров в одну короткую фразу
    #полный текст исключения полезен разработчику в логе, но в интерфейсе выглядит как утечка
    text = str(error).strip()
    first_line = text.splitlines()[0] if text else error.__class__.__name__
    return f"Не удалось прочитать датасет «{file_name}»: {first_line[:200]}"
