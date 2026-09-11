"""Враждебный корпус SQL.

Каждый запрос выполняется против настоящего движка, а не сверяется с регулярным выражением.
Проверяется поведение: отвергнут запрос или нет, и каким кодом.

Разделение кодов существенно:
* `sql_rejected` (403) — запрос выходит за пределы разрешённого, и это решение окна;
* `sql_invalid` (422) — запрос нельзя разобрать или спланировать, это ошибка пользователя.
Смешав их, мы либо назвали бы опечатку попыткой взлома, либо запрет — опечаткой.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from backend.adapters.engine.duckdb_session import (
    SqlInvalidError,
    SqlRejectedError,
    _check_plan_uses_only_allowed_scans,
    execute_query,
    hardened_session,
    quote_identifier,
    validate_query,
)


@pytest.fixture(scope="module")
def dataset_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("sql")
    orders = directory / "orders.parquet"
    users = directory / "users.parquet"

    pl.DataFrame(
        {
            "id": range(200),
            "amount": [float(index % 97) for index in range(200)],
            "country": ["RU", "US", "DE", "FR"] * 50,
        }
    ).write_parquet(orders)

    pl.DataFrame({"id": range(200), "name": [f"u{index}" for index in range(200)]}).write_parquet(
        users
    )

    return {"orders": orders, "users": users}


@pytest.fixture
def session(dataset_files: dict[str, Path]) -> Iterator[object]:
    with hardened_session(dataset_files) as connection:
        yield connection


def run(connection: object, sql: str) -> object:
    #проверка встроена в выполнение: тест не может случайно проверить одно, а выполнить другое
    return execute_query(connection, sql)  # type: ignore[arg-type]


# ── доступ к файловой системе ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('C:/Windows/win.ini')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM glob('C:/Users/*')",
    ],
)
def test_filesystem_access_is_refused(session: object, sql: str) -> None:
    """Здесь достаточно представителей, а не перечня всех читающих функций.

    Все они умирают на одном механизме — белом списке функций, — и он доказан
    не примерами, а для **всего** класса табличных функций сразу:
    `test_no_table_function_is_reachable_by_name`. Полный перечень имён живёт там,
    где он действительно что-то добавляет: в проверке границы исполнения, где
    у каждой функции своё поведение движка, и в живом враждебном корпусе.
    """
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "COPY orders TO 'C:/Users/leak.csv'",
        "COPY orders TO '/tmp/leak.csv' (FORMAT CSV)",
        "EXPORT DATABASE 'C:/Users/dump'",
        "IMPORT DATABASE 'C:/Users/dump'",
    ],
)
def test_writing_files_is_refused(session: object, sql: str) -> None:
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


# ── изменение окружения ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        #все умирают на проверке типа оператора, и она мутируется целиком:
        #здесь по одному представителю на вид оператора
        "ATTACH 'other.db' AS other",
        "INSTALL httpfs",
        "SET enable_external_access=true",
        "CREATE SECRET s (TYPE S3, KEY_ID 'x', SECRET 'y')",
    ],
)
def test_environment_changing_statements_are_refused(session: object, sql: str) -> None:
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


@pytest.mark.parametrize(
    "sql",
    [
        #создание, вставка, изменение, удаление — по одному на вид
        "CREATE TABLE evil AS SELECT 1",
        "INSERT INTO orders VALUES (1, 1.0, 'RU')",
        "UPDATE orders SET amount = 0",
        "DROP TABLE orders",
    ],
)
def test_data_modifying_statements_are_refused(session: object, sql: str) -> None:
    #окно запросов не изменяет ни датасеты, ни рабочее пространство
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


# ── интроспекция движка ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        #по одному представителю на слой, который его ловит: табличная функция —
        #белый список, PRAGMA без плана — проверка плана, PRAGMA с планом — дерево,
        #скалярная функция состояния — снова дерево
        "SELECT * FROM duckdb_settings()",
        "PRAGMA database_list",
        "PRAGMA version",
        "SELECT current_setting('memory_limit')",
    ],
)
def test_engine_introspection_is_refused(session: object, sql: str) -> None:
    """Интроспекция выдаёт устройство сервера, а не данные пользователя.

    Проверено на незащищённом движке: `duckdb_settings()` возвращает `secret_directory`,
    `temp_directory` и `allowed_directories` — то есть реальные пути на машине. Разбор
    здесь не помогает: `PRAGMA` парсится как SELECT, а `duckdb_settings()` и есть
    обычный SELECT. Ловит это только проверка плана.
    """
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM range(1000000)",
        "SELECT * FROM generate_series(1, 1000000)",
    ],
)
def test_generators_are_refused(session: object, sql: str) -> None:
    #генераторы не читают данные пользователя, но позволяют построить произвольно большой
    #результат из ничего: они не нужны аналитике над датасетами
    with pytest.raises(SqlRejectedError):
        run(session, sql)


# ── функции ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        #самые опасные из читающих состояние: они возвращают настоящие пути на машине.
        #Остальные функции этого семейства отвергаются тем же белым списком
        "SELECT current_setting('secret_directory')",
        "SELECT current_setting('temp_directory')",
        "SELECT version()",
    ],
)
def test_engine_state_functions_are_refused(session: object, sql: str) -> None:
    """Проверка плана здесь бессильна, и это измерено.

    `SELECT current_setting('temp_directory')` вычисляется ещё при планировании: в плане
    остаётся DUMMY_SCAN с уже подставленным значением, имени функции там нет. Ловит это
    только разбор запроса в дерево.

    Позиции запроса здесь не перебираются: это делает
    `test_a_forbidden_function_is_found_in_every_position`, и раньше два теста
    проверяли одно и то же на двенадцати и шести вариантах.
    """
    with pytest.raises(SqlRejectedError):
        run(session, sql)


@pytest.mark.parametrize("sql", ["SELECT sleep_ms(5000)", "SELECT nextval('s')", "SELECT stats(id) FROM orders"])
def test_functions_with_side_effects_are_refused(session: object, sql: str) -> None:
    #запрос обязан только читать и считать: удерживать поток или двигать счётчик он не должен
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


def test_function_name_inside_a_string_literal_is_just_data(session: object) -> None:
    #в дереве это константа, а не вызов: запрет построен на разборе, а не на поиске слов
    result = run(session, "SELECT 'current_setting' AS word, id FROM orders LIMIT 1")

    assert result.rows[0]["word"] == "current_setting"  # type: ignore[attr-defined]


def test_column_named_like_a_forbidden_function_is_readable(dataset_files: dict[str, Path]) -> None:
    #имя колонки не является вызовом функции, и владелец данных не обязан их переименовывать
    directory = dataset_files["orders"].parent
    path = directory / "odd.parquet"
    pl.DataFrame({"current_setting": [1, 2], "version": ["a", "b"]}).write_parquet(path)

    with hardened_session({"odd": path}) as connection:
        result = execute_query(connection, 'SELECT "current_setting", "version" FROM odd')

    assert result.row_count == 2


def test_explain_is_refused_because_it_cannot_be_checked(session: object) -> None:
    """EXPLAIN отвергается сознательно, а не по недосмотру.

    Его нельзя ни спланировать (`EXPLAIN (FORMAT json) EXPLAIN ...` — синтаксическая
    ошибка), ни разложить в дерево. Выполнять запрос, прошедший меньше проверок,
    чем все остальные, нельзя, поэтому он недоступен целиком.
    """
    with pytest.raises(SqlRejectedError):
        run(session, "EXPLAIN SELECT * FROM orders")


# ── обход разбора ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders; DROP TABLE orders",
        "SELECT 1; SELECT 2",
    ],
)
def test_multiple_statements_are_refused_entirely(session: object, sql: str) -> None:
    #частичное выполнение недопустимо: первый оператор не должен отработать до отказа
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


@pytest.mark.parametrize("sql", ["SELECT * FROM orders;;", "  ;SELECT * FROM orders"])
def test_stray_semicolons_are_one_statement_not_an_attack(session: object, sql: str) -> None:
    """Лишняя точка с запятой — это опечатка, а не попытка обхода.

    Разборщик приводит «SELECT ...;;» и «;SELECT ...» к одному и тому же запросу.
    Дальше по стеку идёт именно его текст, поэтому проверенное и выполненное совпадают,
    и отвергать такой запрос не за что.
    """
    assert run(session, sql).row_count == 200  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "sql",
    [
        "/*ATTACH*/ SELECT * FROM orders",
        "-- ATTACH 'x'\nSELECT * FROM orders",
        "SELECT * FROM orders -- COPY TO 'x'",
        "SELECT 'ATTACH' AS word FROM orders LIMIT 1",
        "SELECT 'DROP TABLE orders' AS text FROM orders LIMIT 1",
    ],
)
def test_forbidden_words_inside_comments_and_strings_do_not_block_valid_queries(
    session: object, sql: str
) -> None:
    """Запрет построен на разборе, а не на поиске слов.

    Проверка «в тексте есть ATTACH» отвергала бы эти запросы, хотя все они безобидны:
    слово находится в комментарии или в строковом литерале. Обратная сторона той же
    ошибки — `/*ATTACH*/ ATTACH 'x'` она бы пропустила при чуть другой реализации.
    """
    result = run(session, sql)

    assert result.row_count >= 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "sql",
    [
        "aTtAcH 'x.db' AS z",
        "\u00a0ATTACH 'x.db' AS z",
        "\t\nATTACH 'x.db' AS z",
        "/*c*/ATTACH/*c*/'x.db' AS z",
    ],
)
def test_case_and_whitespace_tricks_do_not_bypass_the_check(session: object, sql: str) -> None:
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


# ── легитимная аналитика работает ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        #формы, устроенные в плане по-разному: срез, агрегат, соединение, CTE,
        #оконная функция, подзапрос, объединение, самосоединение. ORDER BY, CASE
        #и HAVING проходят тем же путём, что агрегат и срез
        "SELECT * FROM orders LIMIT 5",
        "SELECT country, count(*) AS n, avg(amount) AS a FROM orders GROUP BY country",
        "SELECT o.id, u.name FROM orders o JOIN users u ON o.id = u.id LIMIT 10",
        "WITH t AS (SELECT * FROM orders WHERE amount > 50) SELECT count(*) FROM t",
        "SELECT id, amount, row_number() OVER (PARTITION BY country ORDER BY amount) FROM orders",
        "SELECT * FROM orders WHERE amount > (SELECT avg(amount) FROM orders) LIMIT 5",
        "SELECT id FROM orders UNION SELECT id FROM users",
        "SELECT count(*) FROM orders o1 JOIN orders o2 ON o1.country = o2.country",
    ],
)
def test_analytics_queries_are_allowed(session: object, sql: str) -> None:
    #белый список не должен мешать работать: если аналитика ломается, им перестанут пользоваться
    result = run(session, sql)

    assert result.columns  # type: ignore[attr-defined]


def test_query_reads_the_real_data(session: object) -> None:
    result = run(session, "SELECT country, count(*) AS n FROM orders GROUP BY country ORDER BY country")

    assert [row["country"] for row in result.rows] == ["DE", "FR", "RU", "US"]  # type: ignore[attr-defined]
    assert all(row["n"] == 50 for row in result.rows)  # type: ignore[attr-defined]


# ── идентификаторы ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("simple", '"simple"'),
        ("with space", '"with space"'),
        ('with"quote', '"with""quote"'),
        ("колонка", '"колонка"'),
        ("select", '"select"'),
        #одна кавычка в начале удваивается: результат остаётся одним идентификатором
        ('"; DROP TABLE x; --', '"""; DROP TABLE x; --"'),
    ],
)
def test_identifier_quoting_escapes_everything(name: str, expected: str) -> None:
    #единственный способ подставить имя в SQL: конкатенации пользовательской строки нет нигде
    assert quote_identifier(name) == expected


def test_quoted_identifier_with_hostile_name_is_safe(dataset_files: dict[str, Path]) -> None:
    hostile = 'orders"; DROP TABLE users; --'

    with hardened_session({hostile: dataset_files["orders"]}) as connection:
        #имя проходит через quote_identifier, и ровно поэтому подстановка безопасна.
        #Без экранирования этот тест обязан упасть
        sql = f"SELECT count(*) FROM {quote_identifier(hostile)}"
        result = execute_query(connection, sql)

    assert result.rows[0]["count_star()"] == 200


# ── слои проверяются по отдельности ───────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM duckdb_secrets()",
        "SELECT * FROM range(10)",
        "SELECT * FROM generate_series(1, 10)",
        "SELECT * FROM read_parquet('/etc/passwd')",
    ],
)
def test_the_plan_layer_refuses_foreign_sources_on_its_own(session: object, sql: str) -> None:
    """Проверка плана вызывается напрямую, в обход проверки дерева.

    Слои перекрывают друг друга, и из-за этого верхний скрывает нижний: после появления
    белого списка функций ни один враждебный запрос не доходил до проверки плана, и она
    могла бы сломаться незамеченной. Мутация, расширившая ALLOWED_SCAN_FUNCTIONS, выжила
    именно поэтому. Здесь нижний слой проверяется сам по себе.
    """
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        _check_plan_uses_only_allowed_scans(session, sql)  # type: ignore[arg-type]


def test_the_plan_layer_passes_ordinary_analytics(session: object) -> None:
    _check_plan_uses_only_allowed_scans(  # type: ignore[arg-type]
        session, "SELECT country, count(*) FROM orders GROUP BY country"
    )


def test_only_the_checked_text_reaches_the_engine(session: object) -> None:
    """Выполняется ровно то, что прошло проверку.

    Разрыв между проверенным и выполненным — самостоятельный класс уязвимостей: проверка
    смотрит на одну строку, движок получает другую. Здесь запись о вызовах ведётся на самом
    соединении, поэтому тест краснеет от подмены аргумента, а не от конкретного запроса.
    """
    executed: list[str] = []

    class RecordingConnection:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def execute(self, sql: str, *args: object, **kwargs: object) -> object:
            executed.append(sql)
            return self._wrapped.execute(sql, *args, **kwargs)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    recording = RecordingConnection(session)
    raw = "  ;SELECT country FROM orders LIMIT 1"
    checked = validate_query(recording, raw)  # type: ignore[arg-type]

    executed.clear()
    execute_query(recording, raw)  # type: ignore[arg-type]

    assert checked == "SELECT country FROM orders LIMIT 1"
    assert checked != raw, "тест бессмыслен, если проверенный текст совпадает с исходным"
    assert executed[-1] == checked, (
        f"в движок ушёл не проверенный текст: {executed[-1]!r} вместо {checked!r}"
    )


# ── попытки обмануть разбор ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        #невидимые пробелы: неразрывный и нулевой ширины ведут себя по-разному,
        #остальные из этого же семейства эквивалентны первому
        "\u00a0\u2009\u3000ATTACH ':memory:' AS m",
        "SELECT\u200bcurrent_setting('temp_directory')",
        #омоглифы и полноширинные знаки: похоже на SELECT, но это другие символы
        "ЅЕLЕСТ current_setting('temp_directory')",
        "ＳＥＬＥＣＴ current_setting('temp_directory')",
        "SELECT сurrent_setting('temp_directory')",
        #несколько операторов, поданных по-разному
        "SELECT 1 FROM orders; ATTACH ':memory:' AS m",
        "SELECT 1 FROM orders;\n\nPRAGMA database_list",
        "SELECT 1 FROM orders -- \n; PRAGMA version",
        "SELECT 1 FROM orders\u0000; DROP TABLE orders",
    ],
)
def test_no_encoding_trick_gets_past_the_parser(session: object, sql: str) -> None:
    """Ни один способ записи не превращает запрещённое в разрешённое.

    Проверка построена на разборе, а не на поиске слов, поэтому регистр, комментарии,
    невидимые пробелы и похожие на латиницу кириллические буквы ничего не меняют:
    омоглиф даёт не «SELECT», а неизвестный разборщику идентификатор.
    """
    with pytest.raises((SqlRejectedError, SqlInvalidError)):
        run(session, sql)


@pytest.mark.parametrize(
    "sql",
    [
        #позиции, устроенные в дереве по-разному: список выбора, условие, HAVING,
        #оконное выражение, лямбда, вложенный CTE. ORDER BY и IN(...) дублируют
        #первые две по способу обхода
        "SELECT (SELECT current_setting('temp_directory')) AS x FROM orders LIMIT 1",
        "SELECT id FROM orders WHERE country = current_setting('temp_directory')",
        "SELECT country FROM orders GROUP BY country HAVING count(*) > length(version())",
        "SELECT id, row_number() OVER (ORDER BY length(version())) FROM orders",
        "SELECT list_transform([1,2], x -> x + length(version())) FROM orders LIMIT 1",
        "WITH a AS (SELECT 1), b AS (SELECT version() AS v FROM a) SELECT * FROM b",
    ],
)
def test_a_forbidden_function_is_found_in_every_position(session: object, sql: str) -> None:
    #дерево обходится целиком: WHERE, HAVING, окно, ORDER BY, лямбда, вложенный CTE
    with pytest.raises(SqlRejectedError):
        run(session, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "FROM orders SELECT country",
        "FROM orders",
        "SELECT COLUMNS('.*') FROM orders LIMIT 1",
        "SELECT * EXCLUDE (amount) FROM orders LIMIT 1",
        "SELECT * REPLACE (amount * 2 AS amount) FROM orders LIMIT 1",
    ],
)
def test_duckdb_specific_syntax_is_not_mistaken_for_an_attack(session: object, sql: str) -> None:
    #сокращённый синтаксис DuckDB — обычная аналитика, и запрещать её не за что
    assert run(session, sql).row_count >= 0  # type: ignore[attr-defined]
