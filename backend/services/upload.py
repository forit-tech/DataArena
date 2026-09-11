"""Приём файла по HTTP.

Лимит проверяется **во время потока**, а не после: файл на 50 ГБ не должен сначала
оказаться на диске и только потом быть отвергнут. Это разница между отказом на первом
превышающем чанке и забитым диском.

Файл пишется в `staging/` и переносится в `sources/` только после успешной регистрации.
Прерванная загрузка, отвалившийся клиент и любая ошибка ниже по стеку оставляют мусор
только в `staging/`, откуда он убирается тем же кодом, который его создал.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import UploadFile

from backend.adapters.formats.registry import ensure_supported_extension
from backend.core.errors import AppError, ValidationError
from backend.core.logging import get_logger
from backend.domain.dataset.identifiers import ensure_workspace_id

#файл пишется порциями: превышение обнаруживается на первом лишнем чанке
UPLOAD_CHUNK_SIZE = 1024 * 1024
#имя, показываемое пользователю, ограничено по длине: оно попадает в интерфейс и в логи
MAX_DISPLAY_NAME_LENGTH = 255


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


def safe_display_name(raw_name: str | None) -> str:
    """Имя для показа пользователю — и только для показа.

    В пути оно не участвует: путь собирается из идентификатора датасета
    и расширения распознанного формата. Здесь снимаются лишь те свойства, которые
    ломают интерфейс и логи: каталоги, управляющие символы, чрезмерная длина.
    """
    if not raw_name:
        raise ValidationError("Не удалось определить имя файла.")

    #Path(...).name отсекает каталоги: «../../evil.csv» становится «evil.csv»
    name = Path(raw_name.replace("\\", "/")).name

    #управляющие символы ломают вывод в терминале и в логе, а NUL ещё и обрывает строку в C-слоях
    name = "".join(character for character in name if character.isprintable())

    #NTFS-поток: «data.csv:stream» описывает альтернативный поток данных, а не другой формат.
    #Отбрасывается только хвост после двоеточия в расширении: сам файл при этом остаётся CSV,
    #и отвергать его как «неподдерживаемый формат» было бы неправдой
    stem, dot, extension = name.rpartition(".")

    if dot and ":" in extension:
        name = f"{stem}.{extension.split(':', 1)[0]}"

    #Windows молча отбрасывает точки и пробелы в конце имени, поэтому «data.csv.» на этой
    #системе и есть «data.csv». Приводим к тому же виду, чтобы поведение не зависело от того,
    #с какой системы пришёл клиент
    name = name.rstrip(". ").strip()

    if not name:
        raise ValidationError("Имя файла состоит из недопустимых символов.")

    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        stem, _, extension = name.rpartition(".")
        keep = MAX_DISPLAY_NAME_LENGTH - len(extension) - 1
        name = f"{stem[:keep]}.{extension}" if extension else name[:MAX_DISPLAY_NAME_LENGTH]

    return name


@contextmanager
def staged_upload(
    workspace_root: Path,
    workspace_id: str,
    upload: UploadFile,
    max_bytes: int,
) -> Iterator[tuple[Path, str]]:
    """Принимает файл в `staging/` и гарантированно убирает его за собой.

    Возвращает путь к принятому файлу и безопасное имя для показа. Файл существует
    только внутри блока `with`: вызывающий обязан перенести его, если хочет сохранить.
    """
    ensure_workspace_id(workspace_id)
    display_name = safe_display_name(upload.filename)
    #расширение проверяется до записи: дешёвая проверка отбрасывает большую часть мусора,
    #а окончательное решение о формате принимает сигнатура уже принятого файла
    ensure_supported_extension(display_name)

    staging = workspace_root / workspace_id / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    #имя во временном каталоге не зависит от пользовательского: даже здесь оно не участвует
    target = staging / f"upload_{id(upload):x}.part"
    logger = get_logger()

    try:
        written = _write_stream(upload, target, max_bytes, display_name)
        logger.info(
            "Файл принят",
            extra={"workspace_id": workspace_id, "bytes": written, "format_hint": Path(display_name).suffix},
        )
        yield target, display_name
    finally:
        #сюда попадают и успешный путь, и разрыв соединения, и любая ошибка ниже по стеку:
        #в staging не должно оставаться ничего ни при каком исходе
        target.unlink(missing_ok=True)
        _remove_empty_directory(staging)


def _write_stream(upload: UploadFile, target: Path, max_bytes: int, display_name: str) -> int:
    #эта функция пишет файл порциями и прерывается на первом чанке, который выводит за лимит
    #проверка «после записи» означала бы, что диск уже занят целиком
    written = 0

    with target.open("wb") as destination:
        while chunk := upload.file.read(UPLOAD_CHUNK_SIZE):
            written += len(chunk)

            if written > max_bytes:
                raise PayloadTooLargeError(
                    f"Файл «{display_name}» больше {max_bytes // (1024 * 1024)} МБ.",
                    details={"max_upload_mb": max_bytes // (1024 * 1024)},
                )

            destination.write(chunk)

    if written == 0:
        raise ValidationError(f"Файл «{display_name}» пуст.")

    return written


def _remove_empty_directory(directory: Path) -> None:
    #пустой staging не нужен, но его отсутствие не является ошибкой
    try:
        directory.rmdir()
    except OSError:
        #каталог не пуст или занят другим запросом — это нормальная ситуация,
        #а не сбой: следующая загрузка воспользуется тем же каталогом
        return


def free_space_for(path: Path) -> int:
    return shutil.disk_usage(path).free
