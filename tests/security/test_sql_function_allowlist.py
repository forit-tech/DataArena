"""Замороженный белый список функций и его связь с каталогом движка.

Список нельзя оставить «живым», то есть вычислять на старте: тогда обновление DuckDB,
добавившее функцию чтения файлов, молча разрешило бы её. Нельзя и вести вручную: он
разъедется с реальностью. Поэтому список выведен из каталога, заморожен в файле,
а этот тест краснеет, как только каталог перестаёт ему соответствовать.
"""

from __future__ import annotations

import duckdb

from backend.adapters.engine.duckdb_session import ALLOWED_FUNCTIONS
from tools.generate_sql_function_allowlist import (
    ENGINE_INTROSPECTION,
    EXCLUDED_TYPES,
    SIDE_EFFECTS,
    build_allowlist,
)


def test_frozen_file_matches_the_engine_catalogue() -> None:
    expected, _ = build_allowlist()

    missing = sorted(set(expected) - ALLOWED_FUNCTIONS)
    extra = sorted(ALLOWED_FUNCTIONS - set(expected))

    assert not missing and not extra, (
        "Список функций разошёлся с каталогом DuckDB. Это не повод править файл руками: "
        "нужно посмотреть, что за функции появились, решить по каждой и перегенерировать "
        "командой python tools/generate_sql_function_allowlist.py.\n"
        f"нет в файле: {missing}\nлишние в файле: {extra}"
    )


def test_the_list_is_not_empty_and_covers_ordinary_analytics() -> None:
    #защита от вырожденного случая: пустой файл прошёл бы проверку выше, разрешив ноль функций,
    #а файл со звёздочкой — все
    assert len(ALLOWED_FUNCTIONS) > 500

    for name in ("count_star", "avg", "sum", "upper", "date_trunc", "row_number", "regexp_matches"):
        assert name in ALLOWED_FUNCTIONS, f"обычная аналитическая функция {name} запрещена"


def test_every_excluded_function_is_actually_absent() -> None:
    """Ни одна исключённая функция не попала в список.

    Одним утверждением по всему множеству, а не перебором по имени. Перебор давал
    двадцать четыре случая, проверяющих арифметику множеств: список **выводится**
    как «каталог минус исключения», и проверка соответствия файла этому вычислению
    уже есть выше. Одно утверждение к тому же сильнее: оно показывает всех
    нарушителей сразу, а не первого попавшегося.
    """
    leaked = sorted((ENGINE_INTROSPECTION | SIDE_EFFECTS) & ALLOWED_FUNCTIONS)

    assert not leaked, f"исключённые функции оказались разрешены: {leaked}"


def test_no_table_function_is_reachable_by_name() -> None:
    """Табличных функций в списке нет ни одной, и это главное свойство файла.

    Через них запрос получил бы собственный источник данных помимо связанных датасетов:
    read_parquet, read_csv, glob, duckdb_secrets, read_duckdb.
    """
    connection = duckdb.connect()

    try:
        table_functions = {
            name
            for name, kind in connection.execute(
                "SELECT DISTINCT function_name, function_type FROM duckdb_functions()"
            ).fetchall()
            if kind in EXCLUDED_TYPES
        }
    finally:
        connection.close()

    leaked = sorted(table_functions & ALLOWED_FUNCTIONS)

    assert not leaked, f"табличные функции попали в белый список: {leaked}"
    for name in ("read_parquet", "read_csv_auto", "glob", "duckdb_settings", "duckdb_secrets"):
        assert name in table_functions and name not in ALLOWED_FUNCTIONS
