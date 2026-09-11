"""Мутационная проверка защит SQL-окна.

Тест, который зелёный и с защитой, и без неё, не проверяет ничего. Здесь каждая защита
по очереди убирается из исходника, тесты запускаются заново, и ожидается красный.
Мутация, пережившая набор тестов, — это дыра в тестах, а не мелочь.

Запуск: python tools/mutation_check.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/adapters/engine/duckdb_session.py"
CONTEXT = ROOT / "backend/api/request_context.py"
PREDICATES = ROOT / "backend/services/health/predicates.py"
CHECKS = ROOT / "backend/services/health/checks.py"
TABLE = ROOT / "backend/services/table_view.py"
HEALTH_REPORT = ROOT / "backend/services/health/report.py"
HEALTH_MODELS = ROOT / "backend/domain/health/models.py"
#мутация, снимающая защиту от падения движка, роняет и сам pytest: прогон обязан
#это пережить, поэтому у каждого запуска есть предел времени
CASE_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class Mutation:
    name: str
    file: Path
    before: str
    after: str
    tests: str


MUTATIONS = [
    Mutation(
        "разрешены любые типы операторов",
        ENGINE,
        'ALLOWED_STATEMENT_TYPES = frozenset({"SELECT"})',
        'ALLOWED_STATEMENT_TYPES = frozenset({"SELECT", "ATTACH", "COPY", "INSTALL", '
        '"LOAD", "SET", "CREATE_TABLE", "INSERT", "UPDATE", "DELETE", "DROP", "PRAGMA", '
        '"EXPLAIN", "EXPORT", "ALTER", "TRANSACTION", "CREATE_MACRO", "DETACH"})',
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "снята проверка количества операторов",
        ENGINE,
        "    if len(statements) != 1:",
        "    if False:",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "снята проверка функций по дереву запроса",
        ENGINE,
        "    _check_functions_are_allowed(connection, checked)",
        "    pass",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "белый список функций подменён на «всё разрешено»",
        ENGINE,
        "    forbidden = sorted(_function_names(document) - ALLOWED_FUNCTIONS)",
        "    forbidden = []",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "дерево обходится не целиком, а только на верхнем уровне",
        ENGINE,
        (
            "            if isinstance(name, str):\n"
            "                found.add(name)\n\n"
            "            for value in current.values():\n"
            "                walk(value)"
        ),
        "            if isinstance(name, str):\n                found.add(name)",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "дерево шаблонов LIKE обходится не целиком",
        ENGINE,
        (
            '                patterns.extend(_constant_strings(current.get("children")))\n\n'
            "            for value in current.values():\n"
            "                walk(value)"
        ),
        '                patterns.extend(_constant_strings(current.get("children")))',
        "tests/security/test_sql_review_findings.py",
    ),
    Mutation(
        "снята проверка плана",
        ENGINE,
        "    _check_plan_uses_only_allowed_scans(connection, checked)",
        "    pass",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "белый список функций сканирования расширен",
        ENGINE,
        'ALLOWED_SCAN_FUNCTIONS = frozenset({"ARROW_SCAN"})',
        'ALLOWED_SCAN_FUNCTIONS = frozenset({"ARROW_SCAN", "DUCKDB_SETTINGS", '
        '"DUCKDB_FUNCTIONS", "DUCKDB_TABLES", "RANGE", "GENERATE_SERIES", '
        '"PARQUET_SCAN", "READ_CSV", "READ_CSV_AUTO", "GLOB", "GENERATE_SERIES"})',
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "снято ограничение движка на внешний доступ",
        ENGINE,
        (
            '        connection.execute(f"SET threads={int(effective.threads)}")\n'
            '        connection.execute("SET enable_external_access=false")'
        ),
        '        connection.execute(f"SET threads={int(effective.threads)}")',
        #именно набор границы исполнения: остальные тесты идут через валидатор,
        #и запрет там срабатывает раньше, чем дело доходит до самого движка
        "tests/security/test_sql_execution_boundary.py",
    ),
    Mutation(
        "снята блокировка настроек движка",
        ENGINE,
        (
            '        connection.execute("SET allow_community_extensions=false")\n'
            '        connection.execute("SET lock_configuration=true")\n\n'
            "        yield connection"
        ),
        (
            '        connection.execute("SET allow_community_extensions=false")\n\n'
            "        yield connection"
        ),
        "tests/security/test_sql_execution_boundary.py tests/security/test_sql_limits.py",
    ),
    Mutation(
        "экранирование идентификаторов убрано",
        ENGINE,
        '    return \'"\' + name.replace(\'"\', \'""\') + \'"\'',
        "    return '\"' + name + '\"'",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "выполняется исходный текст, а не проверенный",
        ENGINE,
        "        relation = connection.execute(checked)",
        "        relation = connection.execute(sql)",
        "tests/security/test_sql_sandbox.py",
    ),
    Mutation(
        "таймаут не прерывает запрос",
        ENGINE,
        "            expired.set()\n            connection.interrupt()",
        "            expired.set()",
        "tests/security/test_sql_limits.py",
    ),
    Mutation(
        "отмена, пришедшая до старта запроса, теряется",
        ENGINE,
        """        with self._lock:
            if self.cancelled:
                raise SqlCancelledError("Запрос отменён.")

            self.running = True""",
        """        with self._lock:
            self.running = True""",
        "tests/security/test_sql_limits.py",
    ),
    Mutation(
        "результат отменённого запроса всё равно отдаётся",
        ENGINE,
        (
            "    if handle is not None and handle.cancelled:\n"
            "        #прерывание могло не успеть"
        ),
        "    if False:\n        #прерывание могло не успеть",
        "tests/security/test_sql_limits.py",
    ),
    Mutation(
        "прерывание от отмены не отличается от таймаута",
        ENGINE,
        (
            "        if handle is not None and handle.cancelled:\n"
            '            raise SqlCancelledError("Запрос отменён.") from error'
        ),
        "        if False:\n            raise SqlCancelledError(\"Запрос отменён.\") from error",
        "tests/security/test_sql_limits.py",
    ),
    Mutation(
        "снята защита от шаблона LIKE, роняющего процесс",
        ENGINE,
        "    _check_like_patterns(document)\n\n    return ParsedQuery",
        "\n    return ParsedQuery",
        "tests/security/test_sql_review_findings.py",
    ),
    Mutation(
        "предел подстановок в шаблоне LIKE снят",
        ENGINE,
        "MAX_LIKE_WILDCARDS = 20",
        "MAX_LIKE_WILDCARDS = 10_000",
        "tests/security/test_sql_review_findings.py",
    ),
    Mutation(
        "управляющие символы в запросе разрешены",
        ENGINE,
        "    control = next((character for character in sql if _is_control(character)), None)",
        "    control = None",
        "tests/security/test_sql_review_findings.py",
    ),
    Mutation(
        "нечитаемый движком датасет снова даёт необработанное исключение",
        ENGINE,
        "            connection.register(alias, _open_dataset(alias, path))",
        "            connection.register(alias, _arrow_dataset_for(path))",
        "tests/security/test_sql_review_findings.py",
    ),
    Mutation(
        "идентификатор запроса берётся только из contextvar",
        CONTEXT,
        (
            '    stored = getattr(request.state, "request_id", "")\n\n'
            "    return stored if isinstance(stored, str) and stored else _request_id.get()"
        ),
        "    return _request_id.get()",
        "tests/security/test_sql_review_findings.py",
    ),
    Mutation(
        "обнаружение дубликатов сломано",
        PREDICATES,
        "    if len(spec.columns) == 1:\n        return pl.col(spec.columns[0]).is_duplicated()",
        "    if len(spec.columns) == 1:\n        return pl.lit(value=False)",
        "tests/unit/test_health_checks.py tests/integration/test_row_identity.py",
    ),
    Mutation(
        "обнаружение пропусков сломано",
        PREDICATES,
        "def _is_null(spec: RowFilter, _: pl.Schema) -> pl.Expr:\n"
        "    return pl.col(spec.columns[0]).is_null()",
        "def _is_null(spec: RowFilter, _: pl.Schema) -> pl.Expr:\n"
        "    return pl.col(spec.columns[0]).is_not_null()",
        "tests/unit/test_health_checks.py tests/integration/test_health_api.py",
    ),
    Mutation(
        "провал в строки не применяет предикат находки",
        TABLE,
        "    if prefilter is not None:\n        plan = plan.filter(prefilter)",
        "    if prefilter is not None:\n        pass",
        "tests/integration/test_health_api.py",
    ),
    Mutation(
        "номер строки берётся от страницы, а не от файла",
        TABLE,
        "    ordinals = [int(value) for value in page[ordinal].to_list()]",
        "    ordinals = list(range(request.offset, request.offset + page.height))",
        "tests/integration/test_row_identity.py",
    ),
    Mutation(
        "имя служебной колонки перестало быть свободным",
        TABLE,
        '    candidate = "__row__"\n    suffix = 0\n\n    while candidate in schema:',
        '    candidate = "__row__"\n    suffix = 0\n\n    while False:',
        "tests/integration/test_row_identity.py",
    ),
    Mutation(
        "приближённая кардинальность выдаётся за точную",
        CHECKS,
        "    exactness = Exactness.SAMPLED if approximate else Exactness.EXACT\n"
        "    sampled_rows = total_rows if approximate else None",
        "    exactness = Exactness.EXACT\n    sampled_rows = None",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "полные дубликаты считаются по всем колонкам плана",
        CHECKS,
        "    duplicates = RowFilter(kind=RowFilterKind.DUPLICATED_BY, columns=names)",
        "    duplicates = RowFilter(kind=RowFilterKind.DUPLICATED_BY, columns=(*names, \"__row__\"))",
        "tests/integration/test_row_identity.py",
    ),
    Mutation(
        "сигнал можно объявить дефектом",
        ROOT / "backend/domain/health/models.py",
        "        if self.code in NOTICE_ONLY_CHECKS and self.severity is not Severity.NOTICE:",
        "        if False:",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "выборочная находка может не сказать про выборку",
        ROOT / "backend/domain/health/models.py",
        "        if self.exactness is Exactness.SAMPLED and self.sampled_rows is None:",
        "        if False:",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "отчёт не пересчитывается при смене артефакта",
        HEALTH_REPORT,
        "        and stored.artifact_fingerprint == fingerprint\n"
        "        and stored.checks_version == CHECKS_VERSION",
        "        and stored.checks_version == CHECKS_VERSION",
        "tests/integration/test_health_api.py tests/unit/test_health_checks.py",
    ),
    Mutation(
        "отчёт не пересчитывается при смене версии проверок",
        HEALTH_REPORT,
        "        and stored.checks_version == CHECKS_VERSION\n    ):",
        "    ):",
        "tests/unit/test_health_checks.py tests/integration/test_health_api.py",
    ),
    Mutation(
        "отпечаток артефакта не входит в идентификатор находки",
        HEALTH_MODELS,
        (
            "        material = \"|\".join(\n"
            "            [dataset_id, artifact_fingerprint, str(CHECKS_VERSION), "
            "self.code.value, *self.columns]\n        )"
        ),
        (
            "        material = \"|\".join(\n"
            "            [dataset_id, str(CHECKS_VERSION), self.code.value, *self.columns]\n"
            "        )"
        ),
        "tests/unit/test_health_checks.py tests/integration/test_health_api.py",
    ),
    Mutation(
        "усечение результата не определяется",
        ENGINE,
        "        rows = relation.fetchmany(effective.row_limit + 1)",
        "        rows = relation.fetchmany(effective.row_limit)",
        "tests/security/test_sql_limits.py",
    ),
]


def run(mutation: Mutation) -> bool:
    """Возвращает True, если тесты покраснели, то есть мутация поймана."""
    original = mutation.file.read_text(encoding="utf-8")
    occurrences = original.count(mutation.before)

    if occurrences == 0:
        print(f"  ПРОПУЩЕНА: исходный фрагмент не найден — {mutation.name}")
        return False

    if occurrences > 1:
        #неоднозначный якорь ломает не тот код, что задумано, и мутация «выживает»
        #по причине, не имеющей отношения к защите. Один раз это уже произошло:
        #после появления второго обхода дерева подмена попадала в соседнюю функцию
        print(
            f"  ПРОПУЩЕНА: фрагмент встречается {occurrences} раза, "
            f"подмена попала бы не туда — {mutation.name}"
        )
        return False

    mutation.file.write_text(
        original.replace(mutation.before, mutation.after, 1), encoding="utf-8"
    )

    try:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", *mutation.tests.split(), "-x", "-q"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=CASE_TIMEOUT_SECONDS,
        )
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        #зависший прогон — тоже красный: снятая защита не должна проходить незамеченной
        returncode = -1
    finally:
        mutation.file.write_text(original, encoding="utf-8")

        if mutation.file.read_text(encoding="utf-8") != original:
            #молча оставить изменённый исходник опаснее любой невыловленной мутации
            raise RuntimeError(f"не удалось восстановить {mutation.file} после «{mutation.name}»")

    return returncode != 0


def ensure_clean(mutations: list[Mutation]) -> bool:
    """Проверяет, что ни одна мутация не осталась применённой с прошлого раза.

    Прерванный прогон оставляет исходник изменённым, и тогда все последующие проверки —
    и мутационные, и обычные — идут против подменённого кода. Это уже происходило:
    неоднозначный якорь подменил не ту ветку, восстановление вернуло не то, и часть
    прогонов прошла по мутированному файлу незамеченной.
    """
    dirty = [
        mutation.name
        for mutation in mutations
        if mutation.before not in mutation.file.read_text(encoding="utf-8")
        and mutation.after in mutation.file.read_text(encoding="utf-8")
    ]

    if dirty:
        print("ОСТАНОВ: в исходниках осталась применённая мутация:")

        for name in dirty:
            print(f"  - {name}")

        print("Восстановите файлы (git checkout) и повторите.")
        return False

    return True


def main() -> int:
    if not ensure_clean(MUTATIONS):
        return 2

    survived = []

    for mutation in MUTATIONS:
        caught = run(mutation)
        print(f"{'поймана ' if caught else 'ВЫЖИЛА  '} {mutation.name}")

        if not caught:
            survived.append(mutation.name)

    print()

    if survived:
        print(f"выжило мутаций: {len(survived)} из {len(MUTATIONS)}")
        for name in survived:
            print(f"  - {name}")
        return 1

    print(f"все {len(MUTATIONS)} мутаций пойманы")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
