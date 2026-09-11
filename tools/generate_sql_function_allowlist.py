"""Собирает белый список SQL-функций из каталога самого движка.

Список не пишется руками: он выводится из `duckdb_functions()` вычитанием явно
обоснованных исключений и замораживается в репозитории. Тест сверяет замороженный
файл с живым каталогом, поэтому обновление DuckDB, добавившее новую функцию,
красит сборку и требует решения человека, а не молча расширяет разрешённое.

Запуск: python tools/generate_sql_function_allowlist.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb

TARGET = Path(__file__).resolve().parents[1] / "backend/adapters/engine/allowed_sql_functions.txt"

#Табличные функции недоступны целиком и намеренно: любые данные попадают в запрос
#только через зарегистрированные датасеты. Разрешить хотя бы одну означало бы дать
#запросу собственный источник данных помимо связанных артефактов.
EXCLUDED_TYPES = frozenset({"table", "table_macro", "pragma"})

#Функции, читающие состояние движка и машины. Они не работают с данными пользователя,
#а рассказывают об устройстве сервера: current_setting('secret_directory') возвращает
#настоящий путь в домашнем каталоге, current_setting('temp_directory') — рабочий каталог.
#Проверка плана их не ловит: значение подставляется ещё при планировании, и в плане
#остаётся DUMMY_SCAN с готовой строкой.
ENGINE_INTROSPECTION = frozenset({
    "current_setting",
    "current_catalog",
    "current_database",
    "current_schema",
    "current_schemas",
    "current_role",
    "current_user",
    "current_query",
    "current_query_id",
    "current_connection_id",
    "current_transaction_id",
    "session_user",
    "user",
    "txid_current",
    "in_search_path",
    "has_database_privilege",
    "has_schema_privilege",
    "parse_duckdb_log_message",
    "version",
})

#Функции, у которых есть последствия за пределами вычисления значения: они пишут,
#двигают счётчики или удерживают поток. Запрос обязан только читать и считать.
SIDE_EFFECTS = frozenset({
    "sleep_ms",
    "write_log",
    "stats",
    "setseed",
    "nextval",
    "currval",
})

EXCLUDED_NAMES = ENGINE_INTROSPECTION | SIDE_EFFECTS


def catalog_function_names() -> tuple[set[str], set[str]]:
    """Возвращает имена обычных функций и отдельно имена табличных."""
    connection = duckdb.connect()

    try:
        rows = connection.execute(
            "SELECT DISTINCT function_name, function_type FROM duckdb_functions()"
        ).fetchall()
    finally:
        connection.close()

    ordinary = {name for name, kind in rows if kind not in EXCLUDED_TYPES}
    tabular = {name for name, kind in rows if kind in EXCLUDED_TYPES}

    return ordinary, tabular


def build_allowlist() -> tuple[list[str], set[str]]:
    catalog, tabular = catalog_function_names()
    #одно и то же имя бывает и обычной, и табличной функцией: range, generate_series,
    #repeat, histogram, version. В дереве запроса они неотличимы, поэтому такое имя
    #исключается целиком. Цена известна и принята: скалярный repeat и агрегат histogram
    #становятся недоступны. Обратное решение стоило бы дороже — имя в белом списке
    #пропустило бы «FROM repeat(...)» через проверку дерева
    catalog -= tabular
    #исключения из семейства pg_*: совместимость с PostgreSQL, вся она про устройство базы
    postgres_introspection = {name for name in catalog if name.startswith("pg_")}
    excluded = EXCLUDED_NAMES | postgres_introspection | tabular

    return sorted(catalog - excluded), excluded


def main() -> None:
    allowed, excluded = build_allowlist()
    version = duckdb.__version__
    header = [
        "# Белый список SQL-функций DataArena.",
        "# Файл создан tools/generate_sql_function_allowlist.py — руками не править.",
        f"# Каталог DuckDB {version}; исключено имён: {len(sorted(excluded))}.",
        "",
    ]
    TARGET.write_text("\n".join(header + allowed) + "\n", encoding="utf-8")
    print(f"{TARGET}: {len(allowed)} имён (DuckDB {version})")


if __name__ == "__main__":
    main()
