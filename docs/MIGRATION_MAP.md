# Migration map: AutoDataAnalysis → DataArena / ModelArena

Источник: `forit-tech/AutoDataAnalysis` @ `7dd8049`.
Строки кода посчитаны фактически (`wc -l`), состояние проверено запуском — см. [AUDIT.md](../AUDIT.md).

**Легенда решения:**

| Метка | Значение |
|---|---|
| 🟢 **PORT** | переносится почти как есть, правки косметические |
| 🟡 **ADAPT** | логика сохраняется, форма меняется под новую архитектуру |
| 🔵 **REWRITE** | переписывается: старая реализация не годится, но задача остаётся |
| ⚪ **MODELARENA** | уходит в ModelArena |
| ⚫ **DROP** | не переносится |

---

## 1. Backend

### 1.1 Core

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `core/errors.py` | 70 | DataArena `core/errors.py` | 🟢 **PORT** | Единый контракт ошибок с кодами и статусами уже правильный. Добавятся `WorkspaceNotFound`, `StepValidationError`, `QueryTimeout` |
| `core/config.py` | 81 | DataArena `core/config.py` | 🟡 **ADAPT** | Каркас (`@lru_cache`, безопасный разбор env, рабочие умолчания) сохраняется. Уходят AI-настройки, добавляются `workspace_root`, лимиты памяти, таймаут SQL |
| — | — | DataArena `core/logging.py` | 🔵 **NEW** | Structured logging в AutoDataAnalysis нет, а требование п. 19 ТЗ его называет |

### 1.2 Чтение и запись данных

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `datasets/io.py` — `_decode_csv_content`, `_detect_csv_separator` | ~90 | `adapters/formats/csv.py` | 🟢 **PORT** | Каскад кодировок с выбором cp1251/cp1252 по доле кириллицы — самая ценная часть модуля. Такое не пишут дважды |
| `datasets/io.py` — `read_dataset` (диспетчер) | ~40 | `adapters/formats/registry.py` | 🔵 **REWRITE** | `if suffix == ...` заменяется реестром с объявленными capabilities и поддержкой `scan_*` |
| `datasets/io.py` — `_read_excel_dataset` | ~20 | `adapters/formats/xlsx.py` | 🟢 **PORT** | Нормализация трёх возможных типов результата `pl.read_excel` — реальный краевой случай |
| `datasets/io.py` — `_read_json_dataset` | ~10 | `adapters/formats/json.py` | 🟢 **PORT** | Каскад JSON-массив → NDJSON |
| `datasets/io.py` — `fingerprint_file` | ~10 | `adapters/storage/hashing.py` | 🟢 **PORT** | Потоковый SHA-256 нужен для lineage |
| `datasets/io.py` — `fingerprint_dataframe` | ~10 | `domain/lineage/` | 🟡 **ADAPT** | Через `write_csv()` — на 1M строк слишком дорого. Заменить на хеш схемы + выборочных чанков |
| `datasets/io.py` — `to_pandas` | ~25 | ModelArena | ⚪ **MODELARENA** | Нужен только sklearn. В DataArena pandas не будет |
| `datasets/cache.py` | 76 | `adapters/storage/` | 🔵 **REWRITE** | LRU по SHA-256 существует, чтобы смягчить перезагрузку файлов. При серверном workspace задача другая: кеш материализованных версий и `LazyFrame`, ключ — `(dataset_id, version)` |
| — | — | `adapters/formats/tsv.py`, `feather.py`, `arrow.py` | 🔵 **NEW** | Требование п. 2 ТЗ. TSV — тривиально; Feather/Arrow — `write_ipc`/`read_ipc`, round-trip проверен |
| — | — | `adapters/formats/*` writers | 🔵 **NEW** | Сейчас пишется только CSV (AUDIT A-3). Polars умеет 7 форматов без `pyarrow` |

### 1.3 Профилирование

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `profiling/models.py` | 114 | `domain/profiling/models.py` | 🟢 **PORT** | `ColumnProfile` / `DatasetProfile` / `QualityScore` покрывают почти весь п. 6 ТЗ |
| `profiling/profiler.py` — `_detect_probable_id` + `_is_monotonic_counter` | ~70 | `domain/profiling/heuristics.py` | 🟢 **PORT** | Аккуратная эвристика с `id_reason`. Прямо закрывает «potential identifiers» из ТЗ |
| `profiling/profiler.py` — `_detect_semantic_type`, `_looks_like_datetime_values` | ~40 | `domain/profiling/heuristics.py` | 🟢 **PORT** | Определение дат по данным, а не по имени |
| `profiling/profiler.py` — статистики, top values, examples | ~120 | `domain/profiling/columns.py` | 🟡 **ADAPT** | Логика сохраняется, добавляются квантили сверх q1/q3, гистограммы распределений, MAD |
| `profiling/profiler.py` — `_calculate_outlier_count` | ~25 | `domain/profiling/outliers.py` | 🟡 **ADAPT** | Сейчас возвращает **только количество**. Нужен drill-down: `row_id / value / method / threshold` (ТЗ п. 6). Добавить robust z-score / MAD и настраиваемый метод |
| `profiling/quality.py` | 102 | `domain/profiling/quality.py` | 🟢 **PORT** | Разложение штрафов с `explanation` на каждый — именно то, что требует ТЗ («не просто карточка Missing: 7%») |
| `profiling/comparison.py` | 206 | `domain/profiling/comparison.py` | 🟢 **PORT** | Сравнение «до/после» переиспользуется для preview шага pipeline |
| — | — | `domain/profiling/correlation.py` | 🔵 **NEW** | **Отсутствует полностью.** Pearson/Spearman, high-correlation pairs, duplicate features, near-zero variance |
| — | — | `domain/profiling/missingness.py` | 🔵 **NEW** | Паттерны совместных пропусков (ТЗ п. 6) |
| — | — | `domain/profiling/duplicates.py` | 🔵 **NEW** | Сейчас есть только счётчик. Нужны **группы** дублей с примерами (ТЗ п. 12) |
| — | — | `domain/profiling/findings.py` | 🔵 **NEW** | Единая модель находки: что, где, почему, каким методом, что можно сделать + запрос для drill-down |

### 1.4 Преобразования

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `etl/engine.py` — `_cast_column`, `_handle_column_missing_values`, `_handle_column_outliers` | ~110 | `domain/pipeline/ops/*.py` | 🟡 **ADAPT** | Логика операций правильная и покрыта тестами. Переупаковывается в классы-шаги с `validate/apply/describe` |
| `etl/engine.py` — `_encode_one_hot_columns` + `_build_unique_temporary_name` | ~60 | `domain/pipeline/ops/one_hot.py` | 🟢 **PORT** | Разрешение коллизий dummy-имён через `max_horizontal` — решённая реальная проблема |
| `etl/engine.py` — `_add_calculated_columns` | ~20 | `domain/pipeline/ops/derive.py` | 🟢 **PORT** | `pl.sql_expr` вместо Python `eval` — правильное решение по безопасности, сохраняем |
| `etl/engine.py` — `_merge_dataframes` | ~40 | `domain/joins/` | 🔵 **REWRITE** | Один ключ, три типа, суффикс `_file_N` (AUDIT A-6). Нужны составные ключи, `right`, выбор колонок сторон, стратегия конфликтов, предупреждение о размножении строк |
| `etl/engine.py` — `_execute_etl_on_frames` (фиксированный порядок) | ~25 | `domain/pipeline/executor.py` | 🔵 **REWRITE** | Порядок зашит в код. Заменяется replay списка шагов |
| `etl/recipe.py` — валидация правил | 186 | `domain/pipeline/validation.py` | 🟡 **ADAPT** | Проверки допустимых значений и понятные сообщения переносятся, привязка к плоской структуре — нет |
| `etl/recipe.py` — `EtlRecipe.to_dict` | ~25 | `domain/pipeline/serialization.py` | 🔵 **REWRITE** | Становится форматом Recipe: список шагов + версия + требования к схеме источников (ТЗ п. 15) |
| `etl/recommendations.py` — `_suggest_column_rules`, `_suggest_calculated_columns` | ~150 | `domain/profiling/suggestions.py` | 🟢 **PORT** | Формат `reason / expected_effect / confidence / severity / patch` — готовая модель для UI-подсказок |
| `etl/recommendations.py` — `_recommend_merge`, `_select_join_key` | ~80 | `domain/joins/suggestions.py` | 🟡 **ADAPT** | Прототип Smart Join Suggestions. Нужно добавить то, чего нет: value overlap, uniqueness обеих сторон, null rate, тип связи 1:1 / 1:N / N:M, confidence, оценка числа строк результата (ТЗ п. 10) |
| — | — | `domain/pipeline/history.py` | 🔵 **NEW** | Undo/Redo (ТЗ п. 7) — отсутствует полностью |
| — | — | `domain/builder/` | 🔵 **NEW** | Dataset Builder (ТЗ п. 9) — отсутствует |
| — | — | `domain/lineage/` | 🔵 **NEW** | Lineage (ТЗ п. 16) — есть только fingerprint |

### 1.5 SQL

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `dataview/sql_guard.py` | 100 | `adapters/engine/statement_check.py` | 🟡 **ADAPT** | Denylist перестаёт быть основной защитой (AUDIT A-2), но остаётся вторым рубежом: «только читающие statements», лимит длины, запрет `;` |
| `dataview/service.py` — `_run_sql` | ~30 | `adapters/engine/duckdb_session.py` | 🔵 **REWRITE** | `pl.SQLContext` на одну таблицу → DuckDB с регистрацией всех датасетов workspace и hardening |
| `dataview/service.py` — `_sanitize_table_name` | ~18 | `adapters/engine/naming.py` | 🟢 **PORT** | Превращение имени файла в SQL-идентификатор нужно и в новой схеме |
| `dataview/service.py` — `_json_safe_rows` | ~20 | `api/serialization.py` | 🟢 **PORT** | NaN/Inf → null, temporal → строка. Без этого ответ не является валидным JSON |
| `dataview/service.py` — `_apply_search` | ~25 | `domain/dataset/filtering.py` | 🟡 **ADAPT** | `literal=True` сохраняем. Перевести на `LazyFrame`, добавить фильтр по конкретной колонке |
| `dataview/service.py` — `build_data_view` | ~40 | `services/dataset_view.py` | 🔵 **REWRITE** | Сейчас принимает материализованный `DataFrame`. Станет lazy + сортировка + фильтры + серверная пагинация |
| — | — | `adapters/storage/sql_history.py` | 🔵 **NEW** | История и Saved Queries (ТЗ п. 5) — отсутствуют |

### 1.6 API

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `api/app.py` — `_register_error_handlers` | ~50 | `api/app.py` | 🟢 **PORT** | Четыре обработчика, единый формат, вырезание пользовательских значений из ошибок валидации |
| `api/app.py` — `COMMON_ERROR_RESPONSES` | ~10 | `api/app.py` | 🟢 **PORT** | Честный OpenAPI |
| `api/uploads.py` | 92 | `services/upload.py` | 🟡 **ADAPT** | Потоковая запись с лимитом и защита имени сохраняются. Меняется назначение: файл кладётся в `workspace/sources/`, а не в `TemporaryDirectory` |
| `api/routes/datasets.py` | 136 | — | 🔵 **REWRITE** | Всё построено вокруг `UploadFile` на каждый запрос |
| `api/routes/etl.py` | 80 | — | 🔵 **REWRITE** | То же + экспорт только в CSV |
| `api/routes/health.py` | 29 | `api/routes/system.py` | 🟢 **PORT** | Плюс версия package contract и статус движка |
| `api/routes/{training,experiments,inference}.py` | 221 | ModelArena | ⚪ **MODELARENA** | |
| `api/routes/ai.py` | 66 | — | ⚫ **DROP** | См. §3 |
| `schemas/common.py`, `schemas/profiling.py`, `schemas/etl.py` | 224 | `api/schemas/` | 🟡 **ADAPT** | Разделение API-схем и доменных моделей сохраняется как принцип; сами схемы меняются вместе с контрактом |
| `schemas/ml.py` | 226 | ModelArena | ⚪ **MODELARENA** | |
| `cli.py` | 39 | `cli.py` | 🟡 **ADAPT** | `analyze-dataset` расширяется до headless-запуска recipe (полезно для CI и воспроизводимости) |

### 1.7 ML → ModelArena

Переносится **без изменения логики**, целиком, вместе с тестами: 2 984 строки `backend/ml/`
+ 221 строка routes + 226 строк `schemas/ml.py`.

| Модуль | LOC | Комментарий |
|---|---|---|
| `ml/training/` (trainer, catalog, config, preprocessing) | 1 028 | 4 модели на задачу, всегда обучаемый baseline |
| `ml/validation/` (leakage, splits) | 947 | Leakage-инспектор — сильная часть, 487 строк |
| `ml/evaluation/` (metrics, bootstrap, error_analysis, verdict) | 879 | |
| `ml/inference/predictor.py` | 438 | Валидация схемы + проверка новизны строк |
| `ml/explainability/importance.py` | 318 | Permutation importance |
| `ml/experiments/` (store, models) | 342 | `Protocol` + файловое хранилище, `joblib` |

Единственная правка при переносе: вход берётся из **Dataset Package**, а не из `UploadFile`.
Приём данных из package — новый адаптер в ModelArena, ML-код не трогается.

---

## 2. Frontend

| Компонент | LOC | → | Решение | Почему |
|---|---|---|---|---|
| `api/client.ts` | 129 | DataArena `api/client.ts` | 🟢 **PORT** | Разделение `ApiError` / `NetworkError`, корректный разбор не-JSON ответа, `describeError` |
| `components/ErrorBoundary.tsx` | 55 | 🟢 **PORT** | + тест | |
| `components/ui.tsx` | 129 | 🟡 **ADAPT** | Banner/EmptyState переносятся, набор расширяется |
| `utils/format.ts` | 172 | 🟢 **PORT** | Форматирование чисел, размеров, ячеек — с тестами |
| `utils/quality.ts` | 99 | 🟡 **ADAPT** | Привязано к Quality Score, который сам переносится |
| `utils/files.ts` | 95 | 🟡 **ADAPT** | Дедупликация выбранных файлов; часть логики уходит на сервер |
| `styles.css` + `styles/features.css` | 3 605 | 🟡 **ADAPT** | Токены, тёмная тема, плотность — база. Разделить по features, вычистить ML-разделы |
| `hooks/usePersistentState.ts` | 27 | 🟢 **PORT** | Для UI-предпочтений (не для данных) |
| `hooks/useNavigation.ts` | 40 | 🟡 **ADAPT** | Другой набор разделов |
| `hooks/workspaceStorage.ts` (IndexedDB) | 118 | ⚫ **DROP** | Прямое следствие stateless-backend. Workspace переезжает на сервер |
| `hooks/useDatasetWorkspace.ts` | 332 | 🔵 **REWRITE** | Работает с `File`-объектами в браузере |
| `features/data-viewer/DataViewer.tsx` | 443 | 🔵 **REWRITE** | Нет сортировки, виртуализации, resize/reorder, панели колонки (AUDIT A-4). Сохранить: защиту от гонки запросов через счётчик, персист состояния просмотра |
| `features/dataset-overview/DatasetOverview.tsx` | 517 | 🟡 **ADAPT** | Отображение профиля и Quality Score — основа Health. Добавить drill-down |
| `features/etl/EtlWorkspace.tsx` | 607 | 🔵 **REWRITE** | Форма плоского рецепта → редактор списка шагов |
| `features/etl/ColumnRulesTable.tsx` | 160 | 🟡 **ADAPT** | Таблица поколоночных правил — удобная и плотная, идея переиспользуется |
| `features/etl/RecommendationList.tsx` | 71 | 🟢 **PORT** | Карточка предложения с причиной и кнопкой «применить» |
| `features/etl/ComparisonPanel.tsx` | 101 | 🟢 **PORT** | Сравнение «до/после» — для preview шага |
| `features/models/*`, `features/evaluate/*`, `features/test-drive/*`, `components/LeakagePanel.tsx`, `types/ml.ts` | 2 495 | ModelArena | ⚪ **MODELARENA** | |
| `components/AiExplanation.tsx`, `hooks/useAiStatus.ts` | 105 | — | ⚫ **DROP** | См. §3 |

---

## 3. Что не переносится и почему

| Компонент | LOC | Причина |
|---|---|---|
| `ai/provider.py`, `ai/facts.py`, `routes/ai.py`, `AiExplanation.tsx`, `useAiStatus.ts` | ~340 | **Не относится к data layer.** Слой пересказывает уже посчитанные числа человеческим языком. Сделан аккуратно (LLM не имеет права вводить новые числа), но DataArena — инструмент, который показывает данные, а не рассказывает о них. Если такая функция понадобится — её место в ModelArena или в отдельном сервисе. Код остаётся в архиве AutoDataAnalysis |
| `hooks/workspaceStorage.ts` | 118 | Хранение датасетов в браузере — то самое решение, от которого DataArena уходит |
| Демо-датасеты `sample_customers*.csv` | — | Заточены под ML-сценарий с целевой колонкой `churned`. DataArena нужны свои: несколько источников с общими ключами, разные форматы, реальные проблемы качества. **Но `examples/make_demo_holdout.py` полезен как образец генератора** |
| `docs/screenshots/*` | 8 файлов | Снимки другого интерфейса |

---

## 4. Судьба AutoDataAnalysis

Репозиторий **не удаляется и не переписывается**. Он переводится в архивное состояние
и остаётся:

- источником кода при переносе (ссылки на конкретные функции даны выше);
- источником **тест-кейсов**: 148 backend-тестов описывают поведение, которое DataArena
  и ModelArena обязаны сохранить. Это лучшая страховка от регрессии при разделении;
- работающей версией, которой можно пользоваться, пока DataArena не дошла до Этапа 8.

В его README добавляется раздел о том, что проект разделён, со ссылками на оба новых репозитория.

---

## 5. Сводка объёма

| Категория | Backend LOC | Frontend LOC |
|---|---|---|
| 🟢 PORT | ~880 | ~1 050 |
| 🟡 ADAPT | ~1 020 | ~4 800 (из них 3 605 — CSS) |
| 🔵 REWRITE | ~450 → новый код | ~1 900 → новый код |
| ⚪ MODELARENA | ~3 430 | ~2 495 |
| ⚫ DROP | ~340 | ~225 |

Практический вывод: **около 1 900 строк backend и 5 850 строк frontend переиспользуются
полностью или с адаптацией.** Это не «начать с нуля» и не «скопировать проект» —
это извлечение работающего слоя с заменой основания.
