"""Рабочий процесс выполнения SQL.

Запускается родителем как `python -m backend.adapters.engine.sql_worker`, получает
задание в stdin, отдаёт ответ в stdout и завершается. Ничего не слушает, ничего
не помнит между запусками и не имеет доступа ни к чему, кроме переданных ему файлов.

**Зачем отдельный процесс.** Долг D-01: движок выполнялся внутри процесса API, и вместе
с ним умирали таймаут, предел памяти и отмена. Найденный при разборе Этапа 3 запрос
с патологическим шаблоном `LIKE` убивал интерпретатор целиком — один обычный POST клал
весь сервер. Ни одна внутрипроцессная защита от этого не спасает: падает не запрос,
а процесс. Здесь падает только рабочий процесс, а сервер остаётся жив и отвечает.

Протокол намеренно простой: одна строка JSON туда, одна обратно. Ни pickle, ни общей
памяти — падение рабочего процесса не должно оставить родителю повреждённое состояние.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    raw = sys.stdin.read()

    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        #задание пишет родитель, и испорченный JSON означает ошибку в нём самом.
        #Отвечать надо всё равно: молчание родитель прочитает как падение движка
        _write({"ok": False, "error": {"code": "worker_bad_request", "message": "Задание не разобрано."}})
        return 2

    #импорт внутри функции: он тянет duckdb и polars, и делать это до чтения задания
    #незачем — процесс запускается на один запрос
    from backend.adapters.engine.duckdb_session import (
        QueryLimits,
        execute_query,
        hardened_session,
    )
    from backend.core.errors import AppError

    bindings = {alias: Path(path) for alias, path in request["bindings"].items()}
    limits = QueryLimits(**request["limits"]) if request.get("limits") else None

    try:
        with hardened_session(bindings, limits) as connection:
            result = execute_query(connection, request["sql"], limits=limits)
    except AppError as error:
        #доменная ошибка переносится целиком: родитель обязан отдать её пользователю
        #ровно так же, как отдал бы при выполнении внутри своего процесса
        _write(
            {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details or {},
                    "status_code": error.status_code,
                },
            }
        )
        return 0

    _write({"ok": True, "result": result.to_dict(), "elapsed_ms": result.elapsed_ms})

    return 0


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
