"""Ограничения ресурсов проверяются исполнением, а не наличием настройки.

Настройка, которую никто не проверил в деле, — это надежда. Здесь запускаются запросы,
которые действительно должны быть прерваны, усечены и отменены, и проверяется, что это
произошло: за отведённое время, с честным признанием усечения, без выдачи среза
за полный результат.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import duckdb
import polars as pl
import pytest

from backend.adapters.engine.duckdb_session import (
    DEFAULT_ROW_LIMIT,
    QueryHandle,
    QueryLimits,
    SqlCancelledError,
    SqlRejectedError,
    SqlTimeoutError,
    execute_query,
    hardened_session,
)


@pytest.fixture(scope="module")
def wide_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("limits") / "rows.parquet"
    #пятьдесят тысяч строк: достаточно, чтобы декартово произведение стало неподъёмным,
    #и достаточно мало, чтобы подготовка теста была мгновенной
    pl.DataFrame(
        {
            "id": range(50_000),
            "text": ["значение" * 8] * 50_000,
            "amount": [float(index) for index in range(50_000)],
        }
    ).write_parquet(path)
    return path


@pytest.fixture
def session(wide_dataset: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    with hardened_session({"rows": wide_dataset}) as connection:
        yield connection


# ── таймаут ───────────────────────────────────────────────────────────────────


def test_a_runaway_query_is_actually_interrupted(session: duckdb.DuckDBPyConnection) -> None:
    """Тяжёлый запрос прерывается, а не доводится до конца.

    Проверяется не факт исключения, а время и причина. Измерено: без прерывания этот
    запрос считается около двенадцати секунд и заканчивается сам, после чего проверка
    «время вышло» рапортует таймаут — при уже потраченном ресурсе. Прежний ассерт
    «уложились в пятнадцать секунд» такую подмену пропускал; вскрыто мутацией, убравшей
    вызов `connection.interrupt()`.
    """
    limits = QueryLimits(timeout_seconds=2)
    started = time.perf_counter()

    with pytest.raises(SqlTimeoutError) as raised:
        execute_query(
            session,
            "SELECT count(*) FROM rows a JOIN rows b ON a.amount < b.amount",
            limits=limits,
        )

    elapsed = time.perf_counter() - started

    assert elapsed < 6, (
        f"запрос доработал до конца за {elapsed:.1f} с вместо прерывания на второй секунде: "
        "об истечении времени сообщили задним числом"
    )
    #причина обязана быть прерыванием от движка, а не проверкой постфактум
    assert isinstance(raised.value.__cause__, duckdb.InterruptException)


def test_a_query_that_overran_never_returns_its_rows(
    session: duckdb.DuckDBPyConnection,
) -> None:
    """Запрос, вышедший за время, не отдаёт результат, даже если успел досчитаться.

    Прерывание не мгновенно, и между истечением времени и остановкой запрос может
    закончиться сам. Отдать такой результат значило бы сделать таймаут необязательным:
    достаточно оказаться чуть быстрее сторожевого потока.
    """
    with pytest.raises(SqlTimeoutError):
        execute_query(
            session,
            "SELECT count(*) FROM rows a JOIN rows b ON a.amount < b.amount",
            limits=QueryLimits(timeout_seconds=1),
        )


def test_the_connection_still_works_after_a_timeout(session: duckdb.DuckDBPyConnection) -> None:
    #прерывание не должно ломать соединение: следующий запрос пользователя обязан работать
    with pytest.raises(SqlTimeoutError):
        execute_query(
            session,
            "SELECT count(*) FROM rows a JOIN rows b ON a.amount < b.amount",
            limits=QueryLimits(timeout_seconds=1),
        )

    result = execute_query(session, "SELECT count(*) AS n FROM rows")

    assert result.rows[0]["n"] == 50_000


# ── отмена ────────────────────────────────────────────────────────────────────


def test_cancel_stops_the_query_and_is_reported_as_cancellation(
    session: duckdb.DuckDBPyConnection,
) -> None:
    """Отмена отличается от таймаута, и это не косметика.

    Пользователь, нажавший «Отменить», не должен видеть «превышено время ожидания»:
    это разные события, и второе выглядит как неисправность там, где её нет.
    """
    handle = QueryHandle(connection=session)
    canceller = threading.Timer(0.5, handle.cancel)
    canceller.start()

    try:
        with pytest.raises(SqlCancelledError):
            execute_query(
                session,
                "SELECT count(*) FROM rows a JOIN rows b ON a.amount < b.amount",
                limits=QueryLimits(timeout_seconds=60),
                handle=handle,
            )
    finally:
        canceller.cancel()


def test_a_cancel_arriving_before_the_query_starts_is_not_lost(
    session: duckdb.DuckDBPyConnection, wide_dataset: Path
) -> None:
    """Отмена, опередившая запрос, обязана его остановить, а не потеряться.

    Измерено на живом движке: `interrupt()`, вызванный когда на соединении ничего
    не выполняется, **не оставляет следа** — следующий запрос отрабатывает целиком.
    Наивная реализация «поднять флаг и прервать» из-за этого теряла отмену: пользователь
    нажимал «Отменить», получал подтверждение, и запрос всё равно считался до конца.
    Найдено сквозным тестом после обновления HTTP-клиента, изменившего порядок доставки.
    """
    handle = QueryHandle()
    handle.attach(session)
    handle.cancel()

    started = time.perf_counter()

    with pytest.raises(SqlCancelledError):
        execute_query(
            session,
            "SELECT count(*) FROM rows a JOIN rows b ON a.amount < b.amount",
            limits=QueryLimits(timeout_seconds=120),
            handle=handle,
        )

    elapsed = time.perf_counter() - started

    #запрос обязан не начаться вовсе, а не отработать и получить отказ задним числом
    assert elapsed < 1, f"отменённый запрос всё равно выполнялся {elapsed:.1f} с"


def test_a_cancelled_query_never_returns_its_rows(
    session: duckdb.DuckDBPyConnection,
) -> None:
    #отмена пришла до старта: запрос не должен начаться
    handle = QueryHandle()
    handle.attach(session)

    canceller = threading.Timer(0.0, handle.cancel)
    canceller.start()
    canceller.join()

    with pytest.raises(SqlCancelledError):
        execute_query(session, "SELECT count(*) FROM rows", handle=handle)


def test_a_query_cancelled_after_it_started_does_not_return_its_rows(
    session: duckdb.DuckDBPyConnection, wide_dataset: Path
) -> None:
    """Отмена пришла во время работы, а прерывание не успело подействовать.

    Такое бывает: между установкой признака отмены и остановкой движка запрос иногда
    заканчивается сам. Отдать его результат значило бы сделать отмену необязательной —
    достаточно оказаться быстрее сторожевого потока.

    Чтобы это состояние получить надёжно, ручка привязана к **другому** соединению:
    отмена вызывается по-настоящему, со всеми её замками, но прерывание уходит туда,
    где ничего не выполняется, и работающий запрос доходит до конца. Проверяется ровно
    то, что остаётся, — решение после выполнения. Без него запрос вернул бы строки,
    будучи отменённым.
    """
    with hardened_session({"rows": wide_dataset}) as idle:
        handle = QueryHandle()
        handle.attach(idle)

        def cancel_once_it_runs() -> None:
            #ждём, пока запрос действительно начнётся: отмена до старта проверяется
            #другим тестом и сработала бы раньше, не дойдя до нужной ветки
            deadline = time.perf_counter() + 10

            while not handle.running and time.perf_counter() < deadline:
                time.sleep(0.001)

            handle.cancel()

        canceller = threading.Thread(target=cancel_once_it_runs)
        canceller.start()

        try:
            with pytest.raises(SqlCancelledError):
                execute_query(
                    session,
                    "SELECT count(*) AS n FROM rows a JOIN rows b ON a.id = b.id",
                    limits=QueryLimits(timeout_seconds=60),
                    handle=handle,
                )
        finally:
            canceller.join(timeout=15)

        assert handle.cancelled


# ── объём результата ──────────────────────────────────────────────────────────


def test_row_limit_truncates_and_says_so(session: duckdb.DuckDBPyConnection) -> None:
    #срез, выданный за полный результат, — это неверный ответ, а не экономия
    result = execute_query(session, "SELECT * FROM rows", limits=QueryLimits(row_limit=100))

    assert len(result.rows) == 100
    assert result.truncated is True
    assert result.truncated_by == "rows"


def test_a_result_that_fits_is_not_marked_as_truncated(
    session: duckdb.DuckDBPyConnection,
) -> None:
    #обратная сторона: честность работает в обе стороны, иначе предупреждение обесценится
    result = execute_query(session, "SELECT * FROM rows LIMIT 10", limits=QueryLimits(row_limit=100))

    assert len(result.rows) == 10
    assert result.truncated is False
    assert result.truncated_by is None


def test_exactly_the_limit_is_not_reported_as_truncated(
    session: duckdb.DuckDBPyConnection,
) -> None:
    #граничный случай: ровно предел — это полный результат, а не усечённый
    result = execute_query(session, "SELECT * FROM rows LIMIT 100", limits=QueryLimits(row_limit=100))

    assert len(result.rows) == 100
    assert result.truncated is False


def test_wide_rows_are_cut_by_size_not_only_by_count(
    session: duckdb.DuckDBPyConnection,
) -> None:
    """Тысяча строк по мегабайту — это гигабайт в браузер.

    Ограничения по числу строк недостаточно: считать нужно и объём.
    """
    result = execute_query(
        session,
        "SELECT id, text, text AS t2, text AS t3, text AS t4 FROM rows",
        limits=QueryLimits(row_limit=10_000, result_bytes=64 * 1024),
    )

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert len(result.rows) < 10_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"row_limit": 10_000_000},
        {"row_limit": 0},
        {"row_limit": -1},
        {"result_bytes": 1024 * 1024 * 1024},
        {"timeout_seconds": 86_400},
        {"timeout_seconds": 0},
        {"threads": 256},
        {"memory_limit": "100GB"},
        {"memory_limit": "512MB'; SET enable_external_access=true; --"},
    ],
)
def test_limits_above_the_ceiling_are_refused(kwargs: dict) -> None:
    """Потолок проверяется при создании ограничений, а не у вызывающего.

    Раньше MAX_ROW_LIMIT был константой, на которую никто не смотрел: запрос на десять
    миллионов строк принимался молча. Предел памяти проверяется отдельно, потому что
    его значение подставляется в SET: список допустимых значений закрыт.
    """
    with pytest.raises(SqlRejectedError):
        QueryLimits(**kwargs)


def test_ordinary_limits_are_accepted() -> None:
    #проверка не должна мешать обычной работе
    limits = QueryLimits(row_limit=5_000, timeout_seconds=60, threads=4, memory_limit="1GB")

    assert limits.row_limit == 5_000


def test_default_row_limit_applies_without_asking(session: duckdb.DuckDBPyConnection) -> None:
    result = execute_query(session, "SELECT * FROM rows")

    assert len(result.rows) == DEFAULT_ROW_LIMIT
    assert result.truncated is True


# ── настройки движка нельзя изменить изнутри ──────────────────────────────────


def test_engine_settings_are_locked_for_the_whole_session(
    session: duckdb.DuckDBPyConnection,
) -> None:
    """Блокировка настроек проверяется движком, а не намерением.

    Без lock_configuration запрос мог бы вернуть себе внешний доступ и обойти сразу
    несколько слоёв. Проверяется поведение самого соединения, минуя валидатор:
    даже если бы проверка запроса когда-нибудь пропустила SET, движок обязан отказать.
    """
    for statement in (
        "SET enable_external_access=true",
        "SET allow_community_extensions=true",
        "SET memory_limit='100GB'",
        "SET lock_configuration=false",
    ):
        with pytest.raises(duckdb.Error):
            session.execute(statement)


def test_the_engine_refuses_to_read_files_by_itself(
    session: duckdb.DuckDBPyConnection,
) -> None:
    #тот же приём: обращаемся к движку напрямую, чтобы убедиться, что защита не держится
    #на одной лишь проверке текста запроса
    for statement in (
        "SELECT * FROM read_csv_auto('C:/Windows/win.ini')",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "SELECT * FROM glob('C:/*')",
        "INSTALL httpfs",
        "ATTACH 'x.db' AS x",
    ):
        with pytest.raises(duckdb.Error):
            session.execute(statement)
