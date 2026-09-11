"""DuckDB как движок запросов над разрешёнными датасетами, а не доступ к машине.

Пять независимых слоёв защиты. Ни один не является достаточным сам по себе, и ни один
не построен на поиске запрещённых слов в тексте запроса. Разбор границы Этапа 3 показал,
где именно проходит их разделение: файловую систему, сеть, расширения, настройки движка
и секреты закрывает сам движок (слой 4) — это проверено обращением к соединению в обход
всех остальных слоёв. Интроспекцию, создание объектов и удержание потока движок
не закрывает: они держатся на слоях 1–3, и это записано, а не подразумевается.

**Слой 0 — текст.** Пустота, длина и управляющие символы. Нулевой байт разборщик
принимает, возвращая оператор вместе с хвостом после него, а движок при выполнении
на этом байте обрывает: выполняется одно, в историю попадает другое.

**Слой 1 — разбор.** `duckdb.extract_statements` даёт настоящий разбор: комментарий
`/*ATTACH*/ SELECT 1` остаётся SELECT, а `SELECT 1; DROP TABLE t` распознаётся как два
statement. Допускается ровно один statement типа SELECT. Этим отсекаются ATTACH, COPY,
INSTALL, LOAD, SET, CREATE, UPDATE, DELETE, EXPLAIN и многооператорные запросы. Дальше
проверяется и выполняется текст, который вернул разборщик, а не исходная строка: иначе
проверенное и выполненное могли бы разойтись.

**Слой 2 — дерево запроса.** `json_serialize_sql` даёт настоящий AST, и из него собираются
все имена функций — в списке выбора, в WHERE, в оконных выражениях, в CTE, в подзапросах.
Имена сверяются с белым списком, выведенным из каталога самого движка
(`allowed_sql_functions.txt`, см. tools/generate_sql_function_allowlist.py). Слой нужен
потому, что план не помогает: `SELECT current_setting('temp_directory')` вычисляется ещё
при планировании, и в плане остаётся DUMMY_SCAN с уже подставленным путём. Измерено —
`current_setting` возвращает рабочий каталог, а `secret_directory` реальный путь в домашнем.

Здесь же проверяются постоянные шаблоны LIKE: чередование «%_%_%_…» роняет процесс целиком,
и ни таймаут, ни предел памяти, ни отмена от этого не спасают — они живут в том же процессе.

**Слой 3 — план.** Разбора недостаточно: `PRAGMA database_list` парсится как SELECT,
а `SELECT * FROM duckdb_settings()` — обычный SELECT, который выдаёт пути на сервере
(проверено: `secret_directory`, `temp_directory`, `allowed_directories`). Поэтому запрос
планируется через `EXPLAIN (FORMAT json)`, и каждая функция сканирования сверяется
с белым списком. Измерено: все легитимные аналитические запросы — простой SELECT,
агрегаты, JOIN, CTE, оконные функции, UNION, подзапросы, CROSS JOIN — используют
ровно одну функцию, `ARROW_SCAN`. Любая интроспекция даёт своё имя: `DUCKDB_SETTINGS`,
`DUCKDB_FUNCTIONS`, `DUCKDB_TABLES`, `RANGE`, `GENERATE_SERIES`.

**Слой 4 — движок.** `enable_external_access=false` + `allow_community_extensions=false`
+ `lock_configuration=true`. Проверено: `read_parquet`, `read_csv`, `read_text`, `glob`,
`COPY TO`, `getenv` и загрузка расширений отвечают `PermissionException` на уровне движка.
Порядок обязателен: датасеты регистрируются ДО блокировки.

**Слой 5 — ресурсы.** Память, потоки, таймаут с настоящим прерыванием через
`connection.interrupt()`, ограничение числа строк и объёма результата.

Датасеты подключаются как `pyarrow.dataset`, а не загружаются в память: DuckDB получает
поток с проталкиванием проекций и фильтров, и запрос к миллиону строк не материализует
таблицу целиком.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow
import pyarrow.dataset as arrow_dataset

from backend.core.errors import AppError
from backend.domain.dataset.json_values import json_safe_value

#EXPLAIN сюда намеренно не входит: его нельзя ни спланировать (`EXPLAIN (FORMAT json)
#EXPLAIN ...` — синтаксическая ошибка), ни разложить в AST. Разрешить его означало бы
#выполнять запрос, прошедший меньше проверок, чем все остальные
ALLOWED_STATEMENT_TYPES = frozenset({"SELECT"})
#единственная функция сканирования, которой пользуется легитимная аналитика над нашими
#датасетами. Список намеренно минимален: расширять его следует по одному имени и только
#вместе с объяснением, почему эта функция не даёт доступа к чему-то помимо датасетов
ALLOWED_SCAN_FUNCTIONS = frozenset({"ARROW_SCAN"})

#белый список функций выведен из каталога движка и заморожен в файле рядом с модулем:
#обновление DuckDB, добавившее новую функцию, красит тест и требует решения человека
ALLOWED_FUNCTIONS_FILE = Path(__file__).with_name("allowed_sql_functions.txt")


def _load_allowed_functions() -> frozenset[str]:
    lines = ALLOWED_FUNCTIONS_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip() for line in lines if line.strip() and not line.startswith("#")
    )


ALLOWED_FUNCTIONS = _load_allowed_functions()

#функции семейства LIKE в дереве запроса: ~~ это LIKE, ~~* это ILIKE, !~~ это NOT LIKE
LIKE_FUNCTIONS = frozenset(
    {"~~", "~~*", "!~~", "!~~*", "like_escape", "ilike_escape", "not_like_escape", "not_ilike_escape"}
)
#предел числа «%» в постоянном шаблоне LIKE. Не про вкус: чередование «%_%_%_…» приводит
#к катастрофическому перебору внутри движка, и это не медленный запрос, а падение процесса —
#измерено, что 40 чередований убивают интерпретатор целиком (см. R-46). Порог взят вдвое
#ниже наблюдавшегося безопасного значения; осмысленному шаблону столько подстановок не нужно
MAX_LIKE_WILDCARDS = 20

MAX_QUERY_LENGTH = 20_000
DEFAULT_ROW_LIMIT = 1_000
MAX_ROW_LIMIT = 10_000
#предел объёма результата: тысяча строк по мегабайту каждая — это гигабайт в браузер
MAX_RESULT_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
MAX_THREADS = 8
#значение уходит в SET memory_limit; список закрыт, чтобы в настройку движка
#не попадала произвольная строка
ALLOWED_MEMORY_LIMITS = frozenset({"256MB", "512MB", "1GB", "2GB", "4GB"})
DEFAULT_MEMORY_LIMIT = "512MB"
DEFAULT_THREADS = 2


class SqlError(AppError):
    status_code = 400
    code = "sql_error"


class SqlRejectedError(SqlError):
    #запрос отвергнут до выполнения: он выходит за пределы того, что окну разрешено
    status_code = 403
    code = "sql_rejected"


class SqlInvalidError(SqlError):
    #запрос не удалось разобрать или спланировать: ошибка пользователя, а не запрет
    status_code = 422
    code = "sql_invalid"


class DatasetUnreadableError(SqlError):
    #файл существует и читается постранично, но движок запросов его не открывает.
    #Это не сбой сервера: пользователю надо сказать, какой датасет и почему недоступен
    status_code = 409
    code = "dataset_unreadable"


class SqlTimeoutError(SqlError):
    status_code = 408
    code = "sql_timeout"


class SqlCancelledError(SqlError):
    status_code = 499
    code = "sql_cancelled"


@dataclass(frozen=True, slots=True)
class QueryLimits:
    row_limit: int = DEFAULT_ROW_LIMIT
    result_bytes: int = MAX_RESULT_BYTES
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    threads: int = DEFAULT_THREADS

    def __post_init__(self) -> None:
        """Потолки проверяются здесь, а не у вызывающего.

        До этой проверки MAX_ROW_LIMIT и MAX_TIMEOUT_SECONDS existed как константы,
        на которые никто не смотрел: `QueryLimits(row_limit=10_000_000)` принимался
        молча. Константа, которую не применяют, выглядит защитой и ею не является.
        Проверка стоит в самом объекте, поэтому ошибка в слое выше не может создать
        запрос без ограничений.
        """
        _ensure_within("row_limit", self.row_limit, 1, MAX_ROW_LIMIT)
        _ensure_within("result_bytes", self.result_bytes, 1, MAX_RESULT_BYTES)
        _ensure_within("timeout_seconds", self.timeout_seconds, 1, MAX_TIMEOUT_SECONDS)
        _ensure_within("threads", self.threads, 1, MAX_THREADS)

        if self.memory_limit not in ALLOWED_MEMORY_LIMITS:
            #строка уходит в SET memory_limit='...'; допускаются только заранее
            #перечисленные значения, поэтому подставлять туда нечего
            raise SqlRejectedError(
                f"Недопустимый предел памяти: {self.memory_limit}.",
                details={"allowed": sorted(ALLOWED_MEMORY_LIMITS)},
            )


def _ensure_within(name: str, value: int, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise SqlRejectedError(
            f"Значение {name}={value} вне допустимого диапазона {low}..{high}.",
            details={"parameter": name, "min": low, "max": high},
        )


@dataclass(slots=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    #строк было больше предела: интерфейс обязан сказать это, а не выдать срез за весь ответ
    truncated: bool
    truncated_by: str | None
    elapsed_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(slots=True)
class QueryHandle:
    """Ручка отмены. Прерывание идёт через тот же connection, что выполняет запрос.

    Отмена приходит из другого потока и может опередить сам запрос. Проверено: вызов
    `interrupt()`, когда на соединении ничего не выполняется, **не оставляет следа** —
    следующий запрос отрабатывает целиком. Наивная реализация «поднять флаг и прервать»
    поэтому теряла отмену: пользователь нажимал «Отменить», получал подтверждение,
    и запрос всё равно считался до конца.

    Замок закрывает это окно. Отмена либо застаёт запрос запущенным — и прерывает его, —
    либо приходит раньше, и тогда `begin` не даст запросу начаться вовсе.
    """

    #соединения может ещё не быть: ручка создаётся до открытия сессии, чтобы отмена,
    #пришедшая между «запрос принят» и «запрос начался», не терялась и не выглядела
    #как «отменять нечего»
    connection: duckdb.DuckDBPyConnection | None = field(default=None)
    cancelled: bool = field(default=False)
    running: bool = field(default=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def attach(self, connection: duckdb.DuckDBPyConnection) -> None:
        with self._lock:
            self.connection = connection

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True

            if self.running and self.connection is not None:
                self.connection.interrupt()

    def begin(self) -> None:
        """Отмечает начало выполнения. Возбуждает отмену, если она уже пришла."""
        with self._lock:
            if self.cancelled:
                raise SqlCancelledError("Запрос отменён.")

            self.running = True

    def finish(self) -> None:
        with self._lock:
            self.running = False


def quote_identifier(name: str) -> str:
    #единственный способ подставить имя в SQL: удвоение внутренних кавычек по правилам SQL
    #конкатенация пользовательской строки в запрос не выполняется нигде
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def hardened_session(
    bindings: Mapping[str, Path],
    limits: QueryLimits | None = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Открывает соединение, в котором доступны только переданные датасеты.

    `bindings` — соответствие «имя в SQL → файл датасета». Пользователь имени файла
    не передаёт и не видит: он оперирует псевдонимами, которые backend связал сам.
    """
    effective = limits or QueryLimits()
    connection = duckdb.connect(":memory:")

    try:
        #датасеты подключаются ДО блокировки: после неё внешний доступ выключен полностью,
        #и зарегистрировать что-либо из файловой системы уже нельзя
        for alias, path in bindings.items():
            connection.register(alias, _open_dataset(alias, path))

        connection.execute(f"SET memory_limit='{effective.memory_limit}'")
        connection.execute(f"SET threads={int(effective.threads)}")
        connection.execute("SET enable_external_access=false")
        connection.execute("SET allow_community_extensions=false")
        connection.execute("SET lock_configuration=true")

        yield connection
    finally:
        connection.close()


def _open_dataset(alias: str, path: Path) -> arrow_dataset.Dataset:
    """Открывает артефакт для запроса, превращая отказ источника в доменную ошибку.

    Движок запросов строже постраничного чтения. Измерено: CSV с разным числом полей
    в строках Polars читает (лишние поля дополняются пустыми), а pyarrow — нет. Датасет
    при этом показан в таблице, значится готовым, и запрос к нему падал необработанным
    исключением, то есть ответом 500 без объяснения. Ответ обязан называть датасет
    и причину; путь к файлу в сообщение не попадает.
    """
    try:
        return _arrow_dataset_for(path)
    except (OSError, ValueError, pyarrow.ArrowInvalid, pyarrow.ArrowNotImplementedError) as error:
        raise DatasetUnreadableError(
            f"Датасет «{alias}» не читается движком запросов: {_source_reason(error)}. "
            "Постраничный просмотр такого файла работает, а SQL — нет: движок требует "
            "одинаковой структуры во всех строках.",
            details={"alias": alias},
        ) from error


#путь в сообщении источника: в кавычках и содержит разделитель каталогов
_QUOTED_PATH = re.compile(r"""['"][^'"]*[/\\][^'"]*['"]""")


def _source_reason(error: Exception) -> str:
    """Причина отказа источника без путей на сервере.

    Обрезать сообщение по последнему двоеточию нельзя: от «CSV parse error: Row #3:
    Expected 2 columns, got 1: 3» осталась бы «3». Поэтому путь именно вычищается,
    а осмысленная часть сохраняется целиком.
    """
    text = str(error).splitlines()[0]

    return _QUOTED_PATH.sub("«файл»", text)[:200]


def _arrow_dataset_for(path: Path) -> arrow_dataset.Dataset:
    #поток вместо загрузки: DuckDB получает проекции и фильтры прямо в источник,
    #и запрос к миллиону строк не материализует таблицу целиком
    suffix = path.suffix.lower()
    fmt = "csv" if suffix in {".csv", ".tsv"} else "parquet"
    return arrow_dataset.dataset(str(path), format=fmt)


def validate_query(connection: duckdb.DuckDBPyConnection, sql: str) -> str:
    """Проверяет запрос и возвращает текст, который разрешено выполнить.

    Возвращается не исходная строка, а то, что выделил разборщик. Выполнять что-либо
    другое — ошибка: проверка относится именно к этому тексту. Поэтому `execute_query`
    вызывает проверку сам и работает только с её результатом.
    """
    _check_text(sql)
    checked = _check_single_allowed_statement(sql)
    _check_functions_are_allowed(connection, checked)
    _check_plan_uses_only_allowed_scans(connection, checked)

    return checked


def _check_text(sql: str) -> None:
    """Проверяет сам текст до разбора: пустоту, длину и управляющие символы.

    Управляющие символы отвергаются не из брезгливости. Разборщик DuckDB считает
    `SELECT 1 FROM t\\x00; DROP TABLE t` **одним** оператором SELECT и возвращает его
    целиком, вместе с хвостом после нулевого байта. Движок при выполнении обрывает
    строку на этом байте, и получается расхождение: выполняется одно, а в историю
    и в лог уходит другое — с оператором, который никогда не запускался. Читающий
    историю увидит одобренный DROP, которого не было.

    Табуляция, перевод строки и возврат каретки разрешены: на них держится
    форматирование запроса.
    """
    if not sql.strip():
        raise SqlInvalidError("Запрос пуст.")

    if len(sql) > MAX_QUERY_LENGTH:
        raise SqlRejectedError(
            f"Запрос длиннее {MAX_QUERY_LENGTH} символов.",
            details={"length": len(sql), "limit": MAX_QUERY_LENGTH},
        )

    control = next((character for character in sql if _is_control(character)), None)

    if control is not None:
        raise SqlRejectedError(
            "Запрос содержит управляющий символ. Такой текст движок и журнал понимают "
            "по-разному, поэтому он не выполняется.",
            details={"code_point": f"U+{ord(control):04X}"},
        )


def _is_control(character: str) -> bool:
    #разрешены только те управляющие символы, что образуют форматирование запроса
    return (character < " " and character not in "\t\n\r") or character == "\x7f"


def _check_single_allowed_statement(sql: str) -> str:
    #разбор, а не поиск слов: комментарии, регистр и невидимые пробелы на него не влияют
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        raise SqlInvalidError(_safe_engine_message(error)) from error

    if len(statements) != 1:
        raise SqlRejectedError(
            "Разрешён ровно один запрос. Несколько операторов через «;» не выполняются "
            "даже частично.",
            details={"statements": len(statements)},
        )

    kind = str(statements[0].type).removeprefix("StatementType.")

    if kind not in ALLOWED_STATEMENT_TYPES:
        raise SqlRejectedError(
            f"Оператор {kind} недоступен: окно запросов только читает данные "
            "и не изменяет ни датасеты, ни рабочее пространство.",
            details={"statement_type": kind},
        )

    #текст берётся у разборщика: «SELECT 1;;» и «;SELECT 1» он приводит к одному
    #и тому же запросу, и дальше по стеку идёт уже он, а не пользовательская строка
    return statements[0].query


def _check_functions_are_allowed(connection: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Сверяет все функции запроса с белым списком, разбирая запрос в дерево.

    Это не поиск слов в тексте: строковый литерал с именем функции — в дереве константа,
    и запрос проходит. А сам вызов, в любом регистре, с комментариями и переносами, —
    узел FUNCTION, и он отвергается.
    """
    document = _serialize(connection, sql)
    _check_function_names(document)
    _check_like_patterns(document)


def _serialize(connection: duckdb.DuckDBPyConnection, sql: str) -> Any:
    """Возвращает дерево запроса. Неразобранный запрос не выполняется."""
    try:
        serialized = connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    except duckdb.Error as error:
        raise SqlInvalidError(_safe_engine_message(error)) from error

    if serialized is None or serialized[0] is None:
        #разобрать запрос не удалось, а непроверенный запрос не выполняется
        raise SqlRejectedError("Запрос не удалось разобрать для проверки.")

    document = json.loads(serialized[0])

    if document.get("error"):
        #сериализатор поддерживает только SELECT; всё прочее уже отсечено слоем 1,
        #поэтому сюда попадает либо синтаксическая ошибка, либо неизвестная конструкция
        raise SqlInvalidError("Запрос не удалось разобрать.")

    return document


def _check_function_names(document: Any) -> None:
    forbidden = sorted(_function_names(document) - ALLOWED_FUNCTIONS)

    if forbidden:
        raise SqlRejectedError(
            f"Функции недоступны в окне запросов: {', '.join(forbidden)}.",
            details={"functions": forbidden},
        )


def _check_like_patterns(document: Any) -> None:
    """Отвергает постоянный шаблон LIKE с чрезмерным числом подстановок.

    Это не забота о скорости. Измерено на живом движке: `text LIKE \'%_%_%_…\'` из сорока
    чередований роняет **процесс целиком** с `Fatal Python error`, и происходит это
    независимо от таймаута, ограничения памяти и отмены — все они выполняются в том же
    процессе и вместе с ним умирают. Одиночные серии «%%%» или «___» безопасны: опасно
    именно чередование, дающее перебор с возвратами.

    Проверяется только постоянный шаблон, и этого достаточно: шаблон, приходящий из данных
    (`text LIKE pattern_column`), движок исполняет другим путём и не падает — проверено
    отдельно на файле, где шаблон лежал в самих данных.
    """
    for pattern in _constant_like_patterns(document):
        wildcards = pattern.count("%")

        if wildcards > MAX_LIKE_WILDCARDS:
            raise SqlRejectedError(
                f"Шаблон LIKE содержит {wildcards} подстановок «%» — больше "
                f"{MAX_LIKE_WILDCARDS}. Такой шаблон приводит к перебору с возвратами.",
                details={"wildcards": wildcards, "limit": MAX_LIKE_WILDCARDS},
            )


def _constant_like_patterns(node: Any) -> list[str]:
    #обход всего дерева: LIKE встречается в WHERE, в HAVING, в CASE, в подзапросе
    patterns: list[str] = []

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            if current.get("function_name") in LIKE_FUNCTIONS:
                patterns.extend(_constant_strings(current.get("children")))

            for value in current.values():
                walk(value)
        elif isinstance(current, list):
            for item in current:
                walk(item)

    walk(node)

    return patterns


def _constant_strings(children: Any) -> list[str]:
    if not isinstance(children, list):
        return []

    found: list[str] = []

    for child in children:
        if not isinstance(child, dict) or child.get("class") != "CONSTANT":
            continue

        value = child.get("value")

        if isinstance(value, dict) and isinstance(value.get("value"), str):
            found.append(value["value"])

    return found


def _function_names(node: Any) -> set[str]:
    #обход всего дерева: функция может стоять в WHERE, в HAVING, в окне, в CTE,
    #в подзапросе любой глубины — проверять только список выбора недостаточно
    found: set[str] = set()

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            name = current.get("function_name")

            if isinstance(name, str):
                found.add(name)

            for value in current.values():
                walk(value)
        elif isinstance(current, list):
            for item in current:
                walk(item)

    walk(node)

    return found


def _check_plan_uses_only_allowed_scans(connection: duckdb.DuckDBPyConnection, sql: str) -> None:
    #разбора недостаточно: PRAGMA парсится как SELECT, а duckdb_settings() — обычный SELECT,
    #выдающий пути на сервере. Здесь проверяется, ЧТО запрос собирается читать
    try:
        raw_plan = connection.execute(f"EXPLAIN (FORMAT json) {sql}").fetchall()[0][1]
    except duckdb.Error as error:
        #сюда попадают и PRAGMA (не планируется), и обычные ошибки пользователя.
        #Различать их не нужно: и то и другое означает «этот запрос выполнить нельзя»
        raise SqlInvalidError(_safe_engine_message(error)) from error

    used = _scan_functions(json.loads(raw_plan))
    forbidden = sorted(used - ALLOWED_SCAN_FUNCTIONS)

    if forbidden:
        raise SqlRejectedError(
            "Запрос обращается к источнику, который не является датасетом рабочего "
            f"пространства: {', '.join(forbidden)}.",
            details={"functions": forbidden},
        )


def _scan_functions(plan: Any) -> set[str]:
    #функция сканирования лежит в extra_info узла плана; обход рекурсивный,
    #потому что вложенность плана произвольна
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            info = node.get("extra_info")

            if isinstance(info, dict) and isinstance(info.get("Function"), str):
                found.add(info["Function"])

            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(plan)
    return found


def _safe_engine_message(error: Exception) -> str:
    #наружу уходит одна строка без трассировки и без путей: полный текст остаётся в логе
    text = str(error).strip()
    first_line = text.splitlines()[0] if text else error.__class__.__name__
    return first_line[:300]


def execute_query(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    limits: QueryLimits | None = None,
    handle: QueryHandle | None = None,
) -> QueryResult:
    """Проверяет и выполняет запрос с настоящим таймаутом и ограничением результата.

    Проверка вызывается здесь, а не оставляется на вызывающего: выполняется ровно тот
    текст, который её прошёл. Разнести эти два шага значит однажды выполнить непроверенное.

    Таймаут не «ждём и надеемся»: по его истечении сторожевой поток вызывает
    `connection.interrupt()`, и DuckDB действительно прерывает выполнение. Проверено —
    декартово произведение на восемь миллиардов строк прерывается за доли секунды.
    """
    import time

    checked = validate_query(connection, sql)
    effective = limits or QueryLimits()
    expired = threading.Event()

    def watchdog() -> None:
        if not finished.wait(effective.timeout_seconds):
            expired.set()
            connection.interrupt()

    if handle is not None:
        #окно между отметкой и первой строкой закрыто замком внутри ручки: если отмена
        #успела прийти, запрос не начнётся, а если придёт позже — застанет его запущенным
        handle.begin()

    #сторож запускается ПОСЛЕ отметки. Обратный порядок оставлял поток без хозяина:
    #`begin` возбуждает отмену до входа в try, `finished` не выставлялся, и поток жил
    #до конца таймаута, держа соединение и вызывая на нём `interrupt()` уже после того,
    #как запрос давно завершился. Найдено чужим упавшим тестом, а не рассуждением
    finished = threading.Event()
    timer = threading.Thread(target=watchdog, daemon=True)
    started = time.perf_counter()
    timer.start()

    try:
        #limit+1 строка: лишняя нужна, чтобы отличить «ровно предел» от «строк больше»
        #и честно сказать об усечении, а не выдать срез за весь результат
        relation = connection.execute(checked)
        rows = relation.fetchmany(effective.row_limit + 1)
        columns = [description[0] for description in relation.description or []]
    except duckdb.InterruptException as error:
        #прерывание могло прийти и от таймаута, и от отмены: различает их состояние ручки,
        #и путать их нельзя — «превышено время» на месте отмены выглядит как неисправность
        if handle is not None and handle.cancelled:
            raise SqlCancelledError("Запрос отменён.") from error

        raise SqlTimeoutError(
            f"Запрос выполнялся дольше {effective.timeout_seconds} с и был прерван.",
            details={"timeout_seconds": effective.timeout_seconds},
        ) from error
    except duckdb.Error as error:
        raise SqlInvalidError(_safe_engine_message(error)) from error
    finally:
        finished.set()

        if handle is not None:
            handle.finish()

    if handle is not None and handle.cancelled:
        #прерывание могло не успеть: запрос иногда заканчивается сам раньше, чем движок
        #его остановит. Отдать такой результат значило бы сделать отмену необязательной
        raise SqlCancelledError("Запрос отменён.")

    if expired.is_set():
        raise SqlTimeoutError(
            f"Запрос выполнялся дольше {effective.timeout_seconds} с и был прерван.",
            details={"timeout_seconds": effective.timeout_seconds},
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    truncated_by = "rows" if len(rows) > effective.row_limit else None
    kept = rows[: effective.row_limit]
    payload = [dict(zip(columns, _json_safe_row(row), strict=False)) for row in kept]

    payload, byte_truncated = _apply_byte_limit(payload, effective.result_bytes)

    if byte_truncated:
        truncated_by = "bytes"

    return QueryResult(
        columns=columns,
        rows=payload,
        row_count=len(payload),
        truncated=truncated_by is not None,
        truncated_by=truncated_by,
        elapsed_ms=elapsed_ms,
    )


def _json_safe_row(row: tuple[Any, ...]) -> list[Any]:

    return [json_safe_value(value) for value in row]


def _apply_byte_limit(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    #тысяча строк по мегабайту каждая — это гигабайт в браузер, и предел по числу строк
    #от этого не спасает. Размер считается по мере накопления, а не после сборки всего ответа
    total = 0
    kept: list[dict[str, Any]] = []

    for row in rows:
        total += len(json.dumps(row, ensure_ascii=False, default=str))

        if total > limit:
            return kept, True

        kept.append(row)

    return kept, False


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """Разобранный запрос: проверенный текст и имена датасетов, к которым он обращается."""

    sql: str
    #имена сохраняются ровно так, как написаны в запросе. Приводить их к нижнему регистру
    #здесь нельзя: под этим же именем датасет будет зарегистрирован в соединении, а DuckDB
    #приводит к нижнему регистру только латиницу — «FROM ПРОДАЖИ» не нашёл бы «продажи».
    #Сопоставление с псевдонимами выполняется без учёта регистра выше по стеку
    tables: tuple[str, ...]


def parse_query(sql: str) -> ParsedQuery:
    """Разбирает запрос, не имея доступа к данным.

    Нужен отдельный шаг, потому что связывание датасетов происходит **до** открытия
    рабочего соединения: регистрировать нужно ровно то, к чему обращается запрос,
    а узнать это можно, только разобрав его. Открывать рабочее соединение со всеми
    датасетами рабочего пространства значило бы платить за каждый неиспользованный.

    Соединение здесь служебное: в нём нет ни одного датасета, и прочитать через него
    нечего. Оно нужно лишь затем, что `json_serialize_sql` — функция движка.
    """
    _check_text(sql)
    checked = _check_single_allowed_statement(sql)

    with _parsing_connection() as connection:
        document = _serialize(connection, checked)

    _check_function_names(document)
    _check_like_patterns(document)

    return ParsedQuery(sql=checked, tables=_referenced_tables(document))


@contextmanager
def _parsing_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    #то же соединение, что и рабочее, только без единого датасета: разбор не должен
    #получать возможностей, которых нет у выполнения
    connection = duckdb.connect(":memory:")

    try:
        connection.execute("SET enable_external_access=false")
        connection.execute("SET allow_community_extensions=false")
        connection.execute("SET lock_configuration=true")
        yield connection
    finally:
        connection.close()


def _referenced_tables(document: Any) -> tuple[str, ...]:
    """Имена таблиц запроса без имён CTE.

    Имя CTE выглядит в дереве такой же таблицей, как датасет: «WITH t AS (...) SELECT *
    FROM t» даёт и orders, и t. Не вычтя их, backend пошёл бы искать датасет t и отказал
    бы в выполнении совершенно правильного запроса.
    """
    tables: list[str] = []
    cte_names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "BASE_TABLE":
                _ensure_local_table(node)
                name = node.get("table_name")

                if isinstance(name, str):
                    tables.append(name)

            cte_map = node.get("cte_map")

            if isinstance(cte_map, dict):
                cte_names.update(
                    str(entry.get("key")).casefold()
                    for entry in cte_map.get("map", [])
                    if entry.get("key") is not None
                )

            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)

    #порядок сохраняется, повторы убираются без учёта регистра: «продажи» и «Продажи» —
    #одна и та же таблица, и регистрировать её дважды не нужно
    seen: set[str] = set()
    result: list[str] = []

    for name in tables:
        folded = name.casefold()

        if folded not in cte_names and folded not in seen:
            seen.add(folded)
            result.append(name)

    return tuple(result)


def _ensure_local_table(node: dict[str, Any]) -> None:
    #обращение к другой базе: своих баз у окна запросов нет, появиться они могут
    #только через ATTACH, который недоступен. Отказ здесь понятнее, чем ошибка движка
    catalog = node.get("catalog_name") or ""
    schema = node.get("schema_name") or ""

    if catalog or schema not in {"", "main"}:
        raise SqlRejectedError(
            "Обращение к другой базе данных недоступно.",
            details={"catalog": catalog[:64], "schema": schema[:64]},
        )
