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
FILTER_VALUES = ROOT / "backend/services/health/filter_values.py"
PIPELINE_SERVICE = ROOT / "backend/services/pipeline/workspace.py"
PIPELINE_OPERATIONS = ROOT / "backend/services/pipeline/operations.py"
JOIN_ANALYSIS = ROOT / "backend/services/joins/analysis.py"
EXPORT = ROOT / "backend/services/export.py"
SQL_PROCESS = ROOT / "backend/services/sql_process.py"
LINEAGE = ROOT / "backend/services/lineage.py"
JOIN_EXECUTE = ROOT / "backend/services/joins/execute.py"
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
        "        and stored.checks_version == CHECKS_VERSION\n"
        "        and stored.artifact_stamp == stamp",
        "        and stored.artifact_stamp == stamp",
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
        "значение предиката сохраняется как есть, без приведения типа",
        CHECKS,
        "        stored: Any | None = encode_filter_value(dominant, schema[facts.name])",
        "        stored = dominant",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "значение предиката восстанавливается без типа колонки",
        PREDICATES,
        "    value = decode_filter_value(spec.values[0], schema[spec.columns[0]])",
        "    value = pl.lit(spec.values[0])",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "двоичное значение предиката теряет точность (описание вместо base64)",
        FILTER_VALUES,
        '        return base64.b64encode(_require(value, bytes)).decode("ascii")',
        '        return f"<{len(_require(value, bytes))} \u0431\u0430\u0439\u0442>"',
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "словарные колонки снова минуют текстовые проверки",
        CHECKS,
        "    if _is_textual(dtype):\n        expressions += [",
        "    if dtype == pl.Utf8:\n        expressions += [",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "находка о повторах выпускается и при нуле повторов",
        CHECKS,
        "        if affected > 0:\n            findings.append(",
        "        if True:\n            findings.append(",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "постоянство, выведенное из оценки, выдаётся за точный подсчёт",
        CHECKS,
        (
            "                #постоянство выведено из кардинальности, а за порогом она"
            " приближённая:\n"
            "                #подписать такую находку «точный подсчёт» — значит выдать оценку"
            " за факт\n"
            "                exactness=exactness,\n"
            "                sampled_rows=sampled_rows,\n"
        ),
        "",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "число расходящихся групп сообщается усечённым",
        CHECKS,
        '    total_groups = grouped.height',
        '    total_groups = min(grouped.height, THRESHOLDS.max_filter_values)',
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "подменённый мимо DataArena артефакт не вызывает пересчёта",
        HEALTH_REPORT,
        "        and stored.artifact_stamp == stamp\n    )",
        "    )",
        "tests/unit/test_health_checks.py",
    ),
    Mutation(
        "потолок точного подсчёта строк возвращён",
        TABLE,
        "    total_rows = int(plan.select(pl.len()).collect().item())",
        "    total_rows = min(int(plan.select(pl.len()).collect().item()), 5_000_000)",
        "tests/performance/test_table_scaling.py",
    ),
    Mutation(
        "сломанный конвейер сохраняется без проверки",
        PIPELINE_SERVICE,
        "        view = _view(store, workspace_root, workspace_id, dataset_id, updated)\n"
        "        store.save_pipeline(updated)\n\n"
        "    return view\n\n\ndef undo(",
        "        store.save_pipeline(updated)\n"
        "        view = _view(store, workspace_root, workspace_id, dataset_id, updated)\n\n"
        "    return view\n\n\ndef undo(",
        "tests/integration/test_pipeline_api.py",
    ),
    Mutation(
        "частичная перестановка колонок молча удаляет остальные",
        PIPELINE_OPERATIONS,
        "    if set(order) != set(schema.names()):",
        "    if False:",
        "tests/integration/test_pipeline_api.py",
    ),
    Mutation(
        "число строк соединения берётся с одной стороны, а не считается",
        JOIN_ANALYSIS,
        "    if kind is JoinKind.INNER:\n        return inner_rows",
        "    if kind is JoinKind.INNER:\n        return left_rows",
        "tests/integration/test_joins_api.py",
    ),
    Mutation(
        "значения-формулы уходят в таблицу как есть",
        EXPORT,
        "    prepared = neutralise_formulas(lazy, schema) if fmt in SPREADSHEET_FORMATS else lazy",
        "    prepared = lazy",
        "tests/integration/test_export_api.py",
    ),
    Mutation(
        "формат с пределом строк молча обрезает данные",
        EXPORT,
        "        raise ExportError(\n"
        "            f\"В этот формат помещается {limit} строк, а в датасете {rows}. \"",
        "        rows = limit\n"
        "        raise_nothing = ExportError(\n"
        "            f\"В этот формат помещается {limit} строк, а в датасете {rows}. \"",
        "tests/integration/test_export_api.py",
    ),
    Mutation(
        "падение рабочего процесса считается успехом",
        SQL_PROCESS,
        "    if code != 0 or not text:",
        "    if False:",
        "tests/security/test_sql_process_isolation.py",
    ),
    Mutation(
        "усечение результата не определяется",
        ENGINE,
        "        rows = relation.fetchmany(effective.row_limit + 1)",
        "        rows = relation.fetchmany(effective.row_limit)",
        "tests/security/test_sql_limits.py",
    ),
    Mutation(
        "колонка соединения выдаётся за колонку собственного файла",
        LINEAGE,
        "    if provenance.is_upload:",
        "    if True:",
        "tests/integration/test_lineage_api.py tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "история колонки берётся у левого родителя независимо от стороны",
        LINEAGE,
        "    frozen = frozen_columns.get((entry.side, entry.source))",
        "    frozen = frozen_columns.get((JoinSide.LEFT, entry.source))",
        "tests/integration/test_lineage_api.py tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "родитель не замораживается: снимок без истории колонок",
        LINEAGE,
        "        columns=tuple(item for item in lineage.columns if item.name in wanted),",
        "        columns=(),",
        "tests/integration/test_lineage_api.py tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "соединение встаёт в историю раньше шагов родителя",
        LINEAGE,
        "            events=[*frozen.events, join_event],",
        "            events=[join_event, *frozen.events],",
        "tests/integration/test_lineage_api.py tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "чтение колонки засчитывается как её изменение",
        LINEAGE,
        "            name for name in dict.fromkeys(operation.reads(step.params)) if name not in changes",
        "            name for name in dict.fromkeys(operation.reads(step.params)) if name in changes",
        "tests/integration/test_lineage_api.py tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "родителя, изменённого во время сборки, не замечают",
        ROOT / "backend/services/joins/workspace.py",
        "        if not before[snapshot.dataset_id] == snapshot.pipeline_fingerprint == current:",
        "        if False:",
        "tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "повреждённая запись о происхождении роняет раздел",
        ROOT / "backend/adapters/storage/workspace_store.py",
        "        except (json.JSONDecodeError, ProvenanceError, RecursionError):\n"
        "            #запись повреждена. Здесь, в отличие от конвейера, это не мешает работать:",
        "        except json.JSONDecodeError:\n"
        "            #запись повреждена. Здесь, в отличие от конвейера, это не мешает работать:",
        "tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "отсутствие записи о происхождении выдаётся за загрузку",
        ROOT / "backend/adapters/storage/workspace_store.py",
        "            return Provenance.unknown(UnknownReason.MISSING_RECORD)",
        '            return Provenance.upload(UploadSource("?", "csv", "0" * 64, 0))',
        "tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "правый ключ полного соединения не разводится суффиксом пользователя",
        JOIN_EXECUTE,
        "    right_keys_out = spec.kind in {JoinKind.FULL, JoinKind.RIGHT}",
        "    right_keys_out = spec.kind in {JoinKind.RIGHT}",
        "tests/integration/test_lineage_api.py tests/integration/test_lineage_release_gate.py",
    ),
    Mutation(
        "шаги, меняющие только строки, попадают в историю колонки",
        PIPELINE_OPERATIONS,
        "def _rows_only(_: dict[str, Any]) -> ColumnEffect:\n    return DatasetOnlyEffect()",
        "def _rows_only(params: dict[str, Any]) -> ColumnEffect:\n"
        '    return ColumnMutation((str(params.get("column", "")),))',
        "tests/integration/test_lineage_api.py",
    ),
    Mutation(
        "сборка с теми же байтами выдаётся за существующий датасет",
        ROOT / "backend/services/joins/workspace.py",
        "            content_addressed=False,",
        "            content_addressed=True,",
        "tests/integration/test_rc_audit.py",
    ),
    Mutation(
        "правое соединение не разводит левую колонку с именем правого ключа",
        JOIN_EXECUTE,
        "    left_out = set(left_columns) - key_left if spec.kind is JoinKind.RIGHT else set(left_columns)",
        "    left_out = set(left_columns)",
        "tests/integration/test_rc_audit.py",
    ),
    Mutation(
        "прогноз соединения считается не в том типе ключей, что само соединение",
        ROOT / "backend/services/joins/analysis.py",
        "    left, right = normalized_keys(left, right, spec)",
        "    pass",
        "tests/unit/test_join_prediction.py",
    ),
    Mutation(
        "имя сборки не очищается",
        ROOT / "backend/services/joins/workspace.py",
        "        return safe_display_name(stem)",
        "        return stem",
        "tests/integration/test_rc_audit.py",
    ),
    Mutation(
        "«удалить строки» удаляет и строки с пропуском",
        PIPELINE_OPERATIONS,
        "lazy.filter(~_predicate(params, schema).fill_null(value=False))",
        "lazy.filter(~_predicate(params, schema))",
        "tests/integration/test_rc_pipeline.py",
    ),
    Mutation(
        "неразбираемое значение из формы становится null",
        PIPELINE_OPERATIONS,
        "            return pl.Series([number], dtype=pl.Utf8).cast(dtype, strict=True).item()",
        "            return pl.Series([number], dtype=pl.Utf8).cast(dtype, strict=False).item()",
        "tests/integration/test_rc_pipeline.py",
    ),
    Mutation(
        "отчёт диагностики помечается конвейером из второго чтения",
        HEALTH_REPORT,
        "        json.dumps(pipeline.to_storage()[\"steps\"], ensure_ascii=False),",
        "        json.dumps(store.get_pipeline(dataset_id).to_storage()[\"steps\"], ensure_ascii=False),",
        "tests/integration/test_rc_concurrency.py",
    ),
    Mutation(
        "глубоко вложенный JSON доходит до нативного читателя",
        ROOT / "backend/adapters/formats/registry.py",
        "                    if depth > maximum:",
        "                    if False:",
        "tests/unit/test_json_depth.py",
    ),
    Mutation(
        "выгрузка помечает апострофом обычные числа и телефоны",
        EXPORT,
        'FORMULA_PATTERN = r"^([=@\\t\\r]|[+-].*[^0-9\\s.,()/+\\-])"',
        'FORMULA_PATTERN = r"^([=@\\t\\r+-])"',
        "tests/integration/test_rc_export_package.py",
    ),
    Mutation(
        "рецепт требует от источника колонки, созданные шагами",
        ROOT / "backend/services/package/builder.py",
        "            source = origin.get(name, name)",
        "            source = name",
        "tests/integration/test_rc_export_package.py",
    ),
    Mutation(
        "отказ выгрузки оставляет временный каталог",
        ROOT / "backend/api/routes/export.py",
        "        shutil.rmtree(directory, ignore_errors=True)\n        raise",
        "        raise",
        "tests/integration/test_rc_export_package.py",
    ),
    Mutation(
        "соединение родителя ссылается на узел соединения этого пакета",
        ROOT / "backend/services/package/builder.py",
        "            if event.dataset_id != lineage.dataset_id:\n                return None\n",
        "",
        "tests/contract/test_package_lineage_columns.py",
    ),
    Mutation(
        "история колонок сверх пределов схемы пишется в пакет",
        ROOT / "backend/services/package/builder.py",
        "            or len(column[\"events\"]) > MAX_EVENTS\n",
        "",
        "tests/contract/test_package_lineage_columns.py",
    ),
    Mutation(
        "неизвестное или неполное происхождение объявляется полной историей",
        ROOT / "backend/services/package/builder.py",
        "        \"history\": \"complete\" if known and not lineage.warnings else \"incomplete\",",
        "        \"history\": \"complete\",",
        "tests/contract/test_package_lineage_columns.py",
    ),
    Mutation(
        "колонка с именем длиннее предела контракта уходит в невалидный пакет",
        ROOT / "backend/services/package/builder.py",
        "    if too_long:\n",
        "    if False:\n",
        "tests/contract/test_package_lineage_columns.py",
    ),
]


class AnchorMissingError(RuntimeError):
    """Мутацию нельзя применить: якорь не совпал с исходником.

    Это не «мутация выжила», а «проверка не выполнялась». Пока эти два исхода
    выглядели одинаково, рефакторинг незаметно выключал мутации: якорь переставал
    совпадать, кампания печатала «ВЫЖИЛА», и это списывали на слабость набора.
    """


def run(mutation: Mutation) -> bool:
    """Возвращает True, если тесты покраснели, то есть мутация поймана."""
    original = mutation.file.read_text(encoding="utf-8")
    occurrences = original.count(mutation.before)

    if occurrences == 0:
        raise AnchorMissingError(
            f"якорь не найден в {mutation.file.name} — «{mutation.name}». "
            "Скорее всего, код изменился: обновите мутацию, иначе защита не проверяется."
        )

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
    unusable = []

    for mutation in MUTATIONS:
        try:
            caught = run(mutation)
        except AnchorMissingError as error:
            #неприменимая мутация — отдельный исход: она ничего не проверила
            print(f"ЯКОРЬ    {mutation.name}")
            unusable.append(str(error))
            continue

        print(f"{'поймана ' if caught else 'ВЫЖИЛА  '} {mutation.name}")

        if not caught:
            survived.append(mutation.name)

    print()

    if unusable:
        #кампания, часть которой не выполнялась, не является пройденной: «все пойманы»
        #при неприменимой мутации — это отчёт о проверке, которой не было
        print(f"не применено мутаций: {len(unusable)} из {len(MUTATIONS)}")
        for message in unusable:
            print(f"  - {message}")

    if survived:
        print(f"выжило мутаций: {len(survived)} из {len(MUTATIONS)}")
        for name in survived:
            print(f"  - {name}")

    if survived or unusable:
        return 1

    print(f"все {len(MUTATIONS)} мутаций пойманы")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
