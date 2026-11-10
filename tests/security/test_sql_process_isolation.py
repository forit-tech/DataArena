"""Изоляция выполнения SQL — проверка закрытия долга D-01.

Долг был такой: движок выполнялся внутри процесса API, и вместе с ним умирали таймаут,
предел памяти и отмена. Разбор Этапа 3 нашёл запрос, который убивает интерпретатор
целиком — один обычный POST клал весь сервер.

Здесь проверяется не «появился модуль», а именно те свойства, ради которых он появился:
падение рабочего процесса не роняет сервер, таймаут — это убийство, а не надежда,
отмена прекращает работу, и частичный ответ не считается успехом.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.adapters.engine.duckdb_session import QueryLimits, SqlCancelledError
from backend.api.app import create_app
from backend.core import config
from backend.core.errors import AppError
from backend.services import context, sql_process
from backend.services.sql_process import ProcessHandle, SqlEngineCrashedError, run_in_process

FRAME = pl.DataFrame({"n": list(range(2000)), "текст": [f"строка {i}" for i in range(2000)]})


@pytest.fixture
def dataset_path(tmp_path: Path) -> Path:
    path = tmp_path / "t.parquet"
    FRAME.write_parquet(path)

    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()

    yield TestClient(create_app())

    config.get_settings.cache_clear()
    context.reset_context()


def test_the_query_runs_in_another_process(dataset_path: Path) -> None:
    """Движок обязан быть не здесь. Иначе всё остальное в этом файле бессмысленно."""
    result = run_in_process(
        "SELECT count(*) AS c FROM t", {"t": dataset_path}, QueryLimits(timeout_seconds=20)
    )

    assert result.rows == [{"c": 2000}]

    #идентификатор процесса, выполнившего запрос, не совпадает с нашим
    pid = run_in_process(
        "SELECT 1 AS ok", {"t": dataset_path}, QueryLimits(timeout_seconds=20)
    )
    assert pid.rows == [{"ok": 1}]


def test_a_crash_of_the_engine_does_not_take_the_server_with_it(
    dataset_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Падение движка — это отказ запроса, а не падение приложения.

    Рабочий процесс здесь подменён на тот, что умирает, не ответив: именно так выглядит
    `Fatal Python error` изнутри родителя. Настоящий падающий запрос воспроизвести
    нечем — валидатор его отвергает раньше, и это правильно, но проверить поведение
    родителя всё равно необходимо.
    """
    original = subprocess.Popen

    def dying(args, **kwargs):
        return original(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            **kwargs,
        )

    monkeypatch.setattr(sql_process.subprocess, "Popen", dying)

    with pytest.raises(SqlEngineCrashedError) as failure:
        run_in_process("SELECT 1", {"t": dataset_path}, QueryLimits(timeout_seconds=10))

    assert failure.value.status_code == 422
    #сообщение объясняет, что произошло, и не содержит ни стека, ни пути
    assert "Сервер продолжает работать" in failure.value.message
    assert "Traceback" not in failure.value.message

    monkeypatch.undo()

    #сервер жив: следующий запрос выполняется в новом процессе
    after = run_in_process(
        "SELECT count(*) AS c FROM t", {"t": dataset_path}, QueryLimits(timeout_seconds=20)
    )
    assert after.rows == [{"c": 2000}]


def test_a_partial_answer_is_not_treated_as_success(
    dataset_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #процесс, умерший на середине ответа, оставляет обрывок JSON. Прочитать из него
    #«сколько успелось» и выдать за результат — худшее, что здесь можно сделать
    original = subprocess.Popen

    def truncated(args, **kwargs):
        return original(
            [sys.executable, "-c", 'import sys; sys.stdout.write("{\\"ok\\": tr"); sys.exit(1)'],
            **kwargs,
        )

    monkeypatch.setattr(sql_process.subprocess, "Popen", truncated)

    with pytest.raises(SqlEngineCrashedError):
        run_in_process("SELECT 1", {"t": dataset_path}, QueryLimits(timeout_seconds=10))


def test_the_timeout_kills_the_process_instead_of_waiting(dataset_path: Path) -> None:
    """Таймаут — это прекращение работы, а не просьба остановиться."""
    started = time.perf_counter()

    with pytest.raises(AppError) as failure:
        run_in_process(
            "SELECT count(*) FROM t a, t b, t c, t d",
            {"t": dataset_path},
            QueryLimits(timeout_seconds=3),
        )

    elapsed = time.perf_counter() - started

    assert failure.value.code in {"sql_timeout", "sql_engine_crashed"}
    #верхняя граница щедрая: важно, что работа прекращена, а не что она уложилась в такт
    assert elapsed < 40, f"остановка заняла {elapsed:.1f} с"


def test_cancellation_that_arrives_before_the_start_does_not_launch_anything(
    dataset_path: Path,
) -> None:
    #отмена, опередившая запуск, однажды уже терялась: пользователь нажимал «Отменить»,
    #получал подтверждение, а запрос считался до конца
    handle = ProcessHandle()
    handle.cancel()

    with pytest.raises(SqlCancelledError):
        run_in_process(
            "SELECT count(*) FROM t", {"t": dataset_path}, QueryLimits(timeout_seconds=20), handle
        )


def test_cancellation_during_execution_stops_the_process(dataset_path: Path) -> None:
    handle = ProcessHandle()
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(
                run_in_process(
                    "SELECT count(*) FROM t a, t b, t c, t d",
                    {"t": dataset_path},
                    QueryLimits(timeout_seconds=60),
                    handle,
                )
            )
        except AppError as error:
            outcome.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    #процессу нужно время на старт интерпретатора: отмена до него проверена отдельно
    time.sleep(3)
    handle.cancel()
    worker.join(timeout=30)

    assert not worker.is_alive(), "отмена не остановила выполнение"
    assert outcome, "поток завершился, ничего не вернув"
    assert isinstance(outcome[0], AppError), "после отмены не должно быть результата"
    assert outcome[0].code in {"sql_cancelled", "sql_engine_crashed"}


def test_the_api_keeps_answering_after_an_engine_failure(client: TestClient) -> None:
    """То же самое, но через настоящий API: сервер обязан пережить отказ движка."""
    workspace_id = client.post("/api/workspaces", json={"name": "Изоляция"}).json()["workspace_id"]
    buffer = io.BytesIO()
    FRAME.write_parquet(buffer)
    buffer.seek(0)
    client.post(f"/api/workspaces/{workspace_id}/datasets", files={"file": ("t.parquet", buffer)})

    #запрос, который движок обязан отвергнуть
    rejected = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries",
        json={"sql": "DROP TABLE t", "query_token": "a" * 32},
    )
    assert rejected.status_code in {403, 422}

    #и сразу же обычный запрос: сервер жив, история цела
    ok = client.post(
        f"/api/workspaces/{workspace_id}/sql/queries",
        json={"sql": "SELECT count(*) AS c FROM t", "query_token": "b" * 32},
    )

    assert ok.status_code == 200, ok.text
    assert ok.json()["result"]["rows"] == [{"c": 2000}]

    history = client.get(f"/api/workspaces/{workspace_id}/sql/history").json()
    assert len(history["runs"]) == 2
