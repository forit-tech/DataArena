"""Выполнение SQL в отдельном процессе — закрытие долга D-01.

Что это меняет. Раньше запрос выполнялся внутри процесса API: таймаут, предел памяти
и отмена жили в том же интерпретаторе, что и сервер, и падение движка уносило сервер
целиком. Разбор Этапа 3 нашёл такой запрос — патологический шаблон `LIKE` из сорока
чередований роняет процесс с `Fatal Python error`.

Теперь запрос выполняет отдельный процесс, а родитель держит его за горло:

* **таймаут** — это не «подождём и понадеемся», а убийство процесса по истечении срока;
* **отмена** — то же убийство, пришедшее от пользователя;
* **падение движка** убивает только рабочий процесс: сервер отвечает дальше, и следующий
  запрос выполняется в новом процессе;
* **частичный результат не считается успехом**: ответ берётся только из целого JSON,
  полученного от процесса, который завершился сам и с нулевым кодом.

Процесс не переиспользуется между запросами. Пул дал бы экономию в сотню миллисекунд
и вернул бы главную проблему: состояние, пережившее падение.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.adapters.engine.duckdb_session import (
    QueryLimits,
    QueryResult,
    SqlCancelledError,
    SqlTimeoutError,
)
from backend.core.errors import AppError
from backend.core.logging import get_logger

#запас поверх таймаута запроса: процессу нужно время на старт интерпретатора и импорт
#движка, и убивать его за это было бы неверно
STARTUP_GRACE_SECONDS = 20


class SqlEngineCrashedError(AppError):
    #движок упал вместе со своим процессом. Это не «внутренняя ошибка сервера»:
    #сервер цел, и пользователю нужно знать, что упал именно его запрос
    status_code = 422
    code = "sql_engine_crashed"


@dataclass
class ProcessHandle:
    """Ручка отмены запроса, выполняемого отдельным процессом.

    Та же задача, что у внутрипроцессной ручки: отмена может опередить запуск. Замок
    закрывает окно — отмена либо застаёт процесс запущенным и убивает его, либо приходит
    раньше, и тогда процесс не запускается вовсе.
    """

    cancelled: bool = False
    process: subprocess.Popen[bytes] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            running = self.process

        if running is not None and running.poll() is None:
            running.kill()

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self.cancelled:
                process.kill()
                raise SqlCancelledError("Запрос отменён.")

            self.process = process


def run_in_process(
    sql: str,
    bindings: dict[str, Path],
    limits: QueryLimits | None = None,
    handle: ProcessHandle | None = None,
) -> QueryResult:
    """Выполняет запрос в отдельном процессе и возвращает результат."""
    request = json.dumps(
        {
            "sql": sql,
            "bindings": {alias: str(path) for alias, path in bindings.items()},
            "limits": _limits_payload(limits),
        },
        ensure_ascii=False,
    )
    timeout = (limits.timeout_seconds if limits else 30) + STARTUP_GRACE_SECONDS

    if handle is not None and handle.cancelled:
        #отмена пришла раньше запуска: процесс не должен начинаться вовсе
        raise SqlCancelledError("Запрос отменён.")

    #запуск собственного модуля тем же интерпретатором: список аргументов, без оболочки
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "backend.adapters.engine.sql_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    if handle is not None:
        handle.attach(process)

    try:
        stdout, stderr = process.communicate(request.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        #настоящее прекращение работы, а не просьба остановиться
        process.kill()
        process.communicate()
        raise SqlTimeoutError(
            f"Запрос выполнялся дольше {limits.timeout_seconds if limits else timeout} с "
            "и был остановлен.",
            details={"timeout_seconds": limits.timeout_seconds if limits else timeout},
        ) from None

    if handle is not None and handle.cancelled:
        raise SqlCancelledError("Запрос отменён.")

    return _decode(process.returncode, stdout, stderr)


def _decode(code: int, stdout: bytes, stderr: bytes) -> QueryResult:
    text = stdout.decode("utf-8", errors="replace").strip()

    if code != 0 or not text:
        #процесс умер, не ответив. Внутрь сообщения не попадает ни путь, ни стек:
        #в лог — подробности, пользователю — что произошло и что делать
        get_logger().warning(
            "Рабочий процесс SQL завершился аварийно",
            extra={"returncode": code, "stderr": stderr.decode("utf-8", errors="replace")[:2000]},
        )
        raise SqlEngineCrashedError(
            "Движок не справился с этим запросом и был остановлен. "
            "Сервер продолжает работать: попробуйте упростить запрос.",
            details={"returncode": code},
        )

    payload = json.loads(text)

    if not payload.get("ok"):
        error = payload["error"]
        raise _domain_error(error)

    result = payload["result"]

    return QueryResult(
        columns=result["columns"],
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
        truncated_by=result.get("truncated_by"),
        elapsed_ms=result["elapsed_ms"],
    )


def _domain_error(error: dict[str, Any]) -> AppError:
    """Восстанавливает доменную ошибку рабочего процесса.

    Пользователю нельзя знать, что запрос выполнял другой процесс: сообщение и код
    обязаны быть теми же, что и раньше. Иначе перенос движка в процесс стал бы
    изменением контракта API.
    """
    restored = AppError(error.get("message", "Запрос не выполнен."), details=error.get("details"))
    restored.code = error.get("code", "sql_failed")
    restored.status_code = int(error.get("status_code", 422))

    return restored


def _limits_payload(limits: QueryLimits | None) -> dict[str, Any] | None:
    if limits is None:
        return None

    return {
        "row_limit": limits.row_limit,
        "result_bytes": limits.result_bytes,
        "timeout_seconds": limits.timeout_seconds,
        "memory_limit": limits.memory_limit,
        "threads": limits.threads,
    }
