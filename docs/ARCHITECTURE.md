# Целевая архитектура DataArena

Документ описывает, **как** DataArena устроена и **почему** именно так. Каждое ключевое решение
опирается на измерение из [AUDIT.md](../AUDIT.md), а не на предпочтение.

---

## 1. Продуктовая граница

```
файлы  →  workspace  →  осмотр  →  профиль  →  правки  →  преобразования
                                                                  ↓
   ModelArena  ←  dataset package  ←  экспорт  ←  валидация  ←  сборка / join
```

DataArena заканчивается на готовом датасете. Она **не обучает модели, не считает ML-метрики
и не выбирает алгоритмы**. Всё, что относится к обучению, живёт в ModelArena.

Единственная точка соприкосновения — **Dataset Package** (см. [contracts/dataset-package-v1.md](contracts/dataset-package-v1.md)).
ModelArena не импортирует Python-модули DataArena, DataArena не знает о существовании ModelArena
ничего, кроме опционального URL. Если ModelArena не запущена, кнопка «Open in ModelArena»
показывает понятное сообщение, и ничего больше не ломается.

Проверяется тестом: полный сценарий DataArena проходит при недоступном ModelArena.

---

## 2. Решение №1: серверный workspace вместо stateless multipart

**Проблема (AUDIT A-1):** в AutoDataAnalysis состояние живёт в браузере, а сервер получает
весь файл на каждую операцию. Это блокирует 8 из 10 ключевых требований.

**Решение.** Workspace — серверная сущность с собственным каталогом на диске:

```
~/.dataarena/workspaces/<workspace_id>/
    workspace.json          манифест: имя, создан, список датасетов
    sources/                исходные файлы, как их загрузил пользователь
        <dataset_id>.<ext>
    derived/                материализованные результаты шагов и сборок
        <node_id>.parquet
    state.db                SQLite: датасеты, pipeline, SQL history, saved queries, lineage
```

Клиент оперирует **идентификаторами**, а не байтами:

```
POST   /api/workspaces/{ws}/datasets            загрузка файла   → dataset_id
GET    /api/workspaces/{ws}/datasets/{id}/rows  ?offset&limit&sort&filter
POST   /api/workspaces/{ws}/sql                 { sql }  → результат + запись в history
POST   /api/workspaces/{ws}/datasets/{id}/steps { step } → новая версия pipeline
```

Файл поднимается на сервер **один раз**. Дальше передаются только страницы по 50–200 строк.

### Почему SQLite, а не JSON-файлы

SQL history, saved queries и pipeline — это записи, которые нужно фильтровать, сортировать
по времени и удалять поштучно. Транзакции нужны, чтобы прерванный запрос не оставил
полузаписанный pipeline. SQLite в стандартной библиотеке Python, ноль дополнительных зависимостей,
одна файловая единица на workspace, которую можно скопировать или удалить целиком.

### Почему не БД-сервер

DataArena — локальный инструмент одного пользователя. Postgres потребовал бы установки,
конфигурации и миграций ради данных, которые целиком помещаются в один файл. Слой хранилища
описан `Protocol`-интерфейсом, поэтому замена возможна без переписывания домена — тот же приём,
который в AutoDataAnalysis уже применён к `ExperimentStore`.

---

## 3. Решение №2: lazy-first чтение

**Измерено на 1M строк:**

| Операция | Eager (`pl.read_*`) | Lazy (`pl.scan_*`) |
|---|---|---|
| количество строк, CSV | ~0.6 с (полный разбор) | **0.02 с** |
| страница `slice(500000, 50)`, CSV | 0.34 с | **0.03 с** |
| страница `slice(500000, 50)`, Parquet | — | **0.007 с** |
| projection + predicate, CSV | полный разбор | **0.09 с** |

**Правило:** `LazyFrame` — способ существования датасета по умолчанию. Материализация
(`.collect()`) — осознанное решение конкретной операции, а не побочный эффект чтения.

Операции, которым материализация действительно нужна (полный профиль, корреляции, некоторые
трансформации), выполняют её явно и с бюджетом памяти. Операции просмотра, схемы, счётчиков,
фильтрации и SQL — не выполняют.

Форматы, для которых `scan_*` недоступен (XLSX, JSON), при загрузке **конвертируются
в Parquet во внутренний `derived/`** один раз. Исходный файл сохраняется нетронутым.
Так lazy-путь становится доступен для всех форматов, а не только для колоночных.

---

## 4. Решение №3: DuckDB в режиме allowlist для SQL

**Проблема (AUDIT A-2):** regex-denylist защищает включённую по умолчанию опасную возможность.

**Решение и порядок, который проверен экспериментально:**

```python
con = duckdb.connect(":memory:")
for dataset in workspace.datasets:                 # 1. регистрируем только разрешённое
    con.register(dataset.sql_name, dataset.arrow())
con.execute("SET enable_external_access = false")  # 2. отключаем файловую систему и сеть
con.execute("SET allow_community_extensions = false")
con.execute("SET lock_configuration = true")       # 3. запираем конфигурацию
```

После шага 3 попытки `read_csv('C:/Windows/win.ini')`, `glob(...)`, `COPY ... TO ...`,
`ATTACH`, `INSTALL httpfs` и `SET enable_external_access=true` возвращают `PermissionException`
на уровне движка — не регулярки, а отсутствующей возможности.

> **Важно и контринтуитивно:** `SET allowed_directories=[...]` **не является песочницей**.
> Проверено на DuckDB 1.5.5: при включённом `enable_external_access` эта настройка не помешала
> ни прочитать `C:/Windows/win.ini`, ни получить листинг домашней папки, ни записать файл наружу.
> Единственная рабочая конфигурация — полное отключение внешнего доступа.

Дополнительно, как второй рубеж: разбор statement и отказ на всём, что не `SELECT`/`WITH`
(в том числе на DDL, который внутри сессии остаётся технически возможен), лимит длины запроса,
таймаут и отмена выполнения.

Соединение эфемерное — создаётся на запрос, живёт в пределах запроса.

**Что это даёт помимо безопасности:** SQL сразу становится кросс-датасетным. Замерено:
`JOIN` 1M × 100k по ключу — 25 мс, `GROUP BY` на 1M — 10 мс. То есть SQL Workspace
и Dataset Builder могут опираться на один движок.

**Роли:** Polars — чтение, схема, трансформации, запись. DuckDB — исполнение пользовательского
SQL и тяжёлые join. Обмен между ними — через Arrow, без копирования.

---

## 5. Решение №4: pipeline как список шагов

**Проблема (AUDIT A-5):** `EtlRecipe` — плоский конфиг с зашитым порядком; шага как объекта нет.

**Решение.** Датасет = источник + **упорядоченный список шагов**:

```json
{
  "dataset_id": "ds_users",
  "source": { "kind": "file", "path": "sources/ds_users.csv", "sha256": "..." },
  "steps": [
    { "id": "s1", "op": "rename_column",   "params": { "from": "userid", "to": "user_id" } },
    { "id": "s2", "op": "cast_column",     "params": { "column": "created_at", "to": "datetime" } },
    { "id": "s3", "op": "drop_columns",    "params": { "columns": ["temporary"] } },
    { "id": "s4", "op": "deduplicate",     "params": { "subset": ["user_id"], "keep": "first" } },
    { "id": "s5", "op": "filter_rows",     "params": { "expr": "status != 'deleted'" } }
  ]
}
```

Свойства, которые из этого следуют бесплатно:

- **исходный файл никогда не меняется** — шаги применяются к копии при `collect()`;
- **undo/redo** — перемещение указателя по списку, а не откат мутаций;
- **удаление шага** — удаление элемента и повторный replay;
- **reorder** — разрешён только там, где шаги не зависят друг от друга; зависимость вычисляется
  по колонкам, которые шаг читает и пишет, и при конфликте UI объясняет, почему перестановка запрещена;
- **preview любого шага** — replay первых N шагов;
- **recipe** — это и есть `steps`, сериализованные отдельно от данных;
- **lineage** — `steps` + sha256 источников + timestamp.

Каждый `op` — отдельный класс с методами `validate(schema) → errors`,
`apply(LazyFrame) → LazyFrame`, `describe() → человекочитаемая строка`, `preview_impact()`.
Новая операция добавляется одним файлом и одной записью в реестр.

Логика операций переносится из `backend/etl/engine.py` — там она уже написана и покрыта тестами;
меняется только упаковка.

---

## 6. Слои backend

```
api/          HTTP. Роутеры, request/response-схемы Pydantic, обработчики ошибок.
              Не содержит предметной логики. Схемы API отделены от доменных моделей:
              внутреннее представление можно менять, не ломая контракт.

services/     Сценарии: «загрузить датасет в workspace», «выполнить SQL и записать в history»,
              «собрать датасет по плану builder», «экспортировать». Оркестрация, транзакции.

domain/       Предметные модели и чистые вычисления. Не знает про HTTP, файлы и БД.
    dataset/      Dataset, DatasetVersion, Schema, ColumnType
    pipeline/     Step, StepRegistry, реализации операций, replay
    profiling/    профиль, health-проверки, drill-down находок  ← перенос из AutoDataAnalysis
    joins/        анализ ключей, оценка кардинальности, предупреждения
    builder/      план сборки и его валидация
    lineage/      граф происхождения

adapters/     Всё внешнее.
    formats/      readers и writers по одному модулю на формат + реестр возможностей
    storage/      WorkspaceStore (SQLite), файловое хранилище
    engine/       DuckDBSession — создание, регистрация, hardening, отмена запроса

core/         Конфиг, ошибки, логирование.  ← перенос из AutoDataAnalysis
```

Правило зависимостей: `api → services → domain`, `services → adapters`.
`domain` не импортирует ничего из `api` и `adapters`. Это проверяется тестом на граф импортов,
а не только договорённостью.

### Реестр форматов

Каждый формат объявляет свои **возможности**, а не просто наличие:

```python
FormatCapabilities(
    extensions=(".parquet",),
    can_read=True, can_write=True,
    supports_lazy_scan=True,       # доступен ли scan_*
    preserves_schema=True,         # выживают ли типы при round-trip
    preserves_nulls=True,
    row_limit=None,                # у XLSX — 1_048_576
    warnings=(),
)
```

Export Dialog строит предупреждения **из этой таблицы**, а не из зашитых строк. Значения
`preserves_schema` подтверждены round-trip-тестами (AUDIT A-3), и каждый новый формат обязан
такой тест иметь — иначе он не попадает в реестр.

---

## 7. Frontend

React 19 + TypeScript + Vite, как в AutoDataAnalysis — этот выбор себя оправдал (build 2.75 с,
320 кБ JS, три зависимости).

Разделы:

| Раздел | Назначение |
|---|---|
| **Workspace** | список датасетов, загрузка, схема, размеры, состояние изменений |
| **Dataset** | таблица: virtual scrolling, сортировка, фильтр, resize/reorder колонок, панель колонки |
| **SQL** | редактор, выполнение, отмена, время, история, сохранённые запросы |
| **Health** | профиль и находки, у каждой — «Подробнее» с конкретными строками |
| **Builder** | визуальная сборка: источники, join, выбор колонок, preview |
| **Recipes** | сохранённые рецепты, повторное применение, lineage |

Технические решения:

- **виртуализация — своя**, на `position: absolute` поверх измеренной высоты строки.
  Обоснование: единственная реальная задача — не рисовать невидимые строки; готовая
  библиотека принесла бы 30–60 кБ и собственную модель колонок, с которой пришлось бы воевать
  при resize/reorder. Если своя реализация окажется хуже — заменим на `@tanstack/react-virtual`,
  и это будет зафиксировано в README как решение, а не умолчание;
- **состояние**: серверное состояние — источник истины, клиент кеширует ответы по ключу
  `(workspace, dataset, version, query)`. Ничего похожего на IndexedDB-снимок из
  AutoDataAnalysis: после перезагрузки состояние приходит с сервера, а не восстанавливается из браузера;
- **дизайн**: тёмная база, фиолетовые и wine-red акценты, плотная desktop-first сетка.
  Никаких градиентных карточек, анимаций и landing-разметки. CSS-переменные, свой CSS —
  как в AutoDataAnalysis, где это дало 55 кБ на всё приложение.

---

## 8. Структура репозитория

```
DataArena/
├── README.md                      русский, technical terms по-английски
├── AUDIT.md                       аудит AutoDataAnalysis (этот этап)
├── CHANGELOG.md
├── pyproject.toml
├── docker-compose.yml
├── .github/workflows/ci.yml
│
├── docs/
│   ├── ARCHITECTURE.md            этот файл
│   ├── MIGRATION_MAP.md           что откуда переносится
│   ├── IMPLEMENTATION_PLAN.md     этапы и definition of done
│   ├── RISKS.md                   технические риски
│   ├── contracts/
│   │   └── dataset-package-v1.md  контракт с ModelArena
│   ├── decisions/                 ADR: короткие записи о принятых решениях
│   └── screenshots/               только реальные снимки
│
├── backend/
│   ├── main.py
│   ├── core/                      config, errors, logging
│   ├── api/
│   │   ├── app.py
│   │   ├── routes/                workspaces, datasets, sql, health, pipeline,
│   │   │                          builder, export, recipes, packages, system
│   │   └── schemas/               Pydantic-схемы API (отдельно от domain)
│   ├── services/
│   ├── domain/
│   │   ├── dataset/  pipeline/  profiling/  joins/  builder/  lineage/
│   └── adapters/
│       ├── formats/               csv, tsv, parquet, json, jsonl, xlsx, feather, arrow
│       ├── storage/               sqlite_store, file_store
│       └── engine/                duckdb_session
│
├── frontend/
│   ├── package.json  vite.config.ts  tsconfig.json  eslint.config.js
│   └── src/
│       ├── api/                   client, endpoints (единственное место про транспорт)
│       ├── app/                   оболочка, навигация, layout
│       ├── features/
│       │   ├── workspace/  dataset-table/  sql/  health/  builder/  recipes/  export/
│       ├── components/            переиспользуемый UI + ErrorBoundary
│       ├── hooks/  types/  utils/  styles/
│
├── tests/
│   ├── unit/                      formats, transformations, joins, profiling, recipes
│   ├── integration/               upload→inspect→transform→export, join, sql, replay, package
│   ├── security/                  sql sandbox, path traversal, upload limits
│   └── performance/               10k / 100k / 1M — время и память, с порогами
│
├── examples/                      демо-датасеты
└── scripts/                       генерация тестовых данных, снятие скриншотов
```

---

## 9. Стек и обоснование каждой зависимости

| Зависимость | Зачем | Обоснование выбора |
|---|---|---|
| Python 3.12 | runtime | проверено на 3.12.10; нижняя граница 3.11 |
| FastAPI | HTTP + OpenAPI | уже используется, контракт ошибок переносится целиком |
| Pydantic v2 | схемы API | отделение контракта от домена |
| **Polars** | чтение, схема, трансформации, запись | измерено: 1M строк профилируются за 0.98 с; `scan_*` даёт срез на 500k за 7 мс; пишет 7 форматов без `pyarrow` |
| **DuckDB** | пользовательский SQL, тяжёлые join | измерено: join 1M×100k за 25 мс; единственный проверенный способ получить allowlist-песочницу |
| PyArrow | обмен Polars ↔ DuckDB без копирования | добавляется, только если zero-copy реально потребует; **пока не подтверждено, что нужен** — Polars и DuckDB работают и без него |
| xlsxwriter + fastexcel | Excel | подтверждено round-trip-тестом, Date переживает запись |
| SQLite (stdlib) | workspace state | без внешних зависимостей |
| React 19 + TS + Vite | frontend | оправдал себя в AutoDataAnalysis |
| lucide-react | иконки | единственная UI-зависимость |

**Что сознательно не берётся:**

- pandas — не нужен в DataArena; он был нужен ML-слою, а тот уходит в ModelArena;
- scikit-learn — по той же причине;
- UI-фреймворк (MUI/AntD) — свой CSS дал 55 кБ и полный контроль над плотностью интерфейса;
- ORM — SQLite-схема на десяток таблиц не окупает SQLAlchemy;
- Redis/очереди — локальный однопользовательский инструмент.

---

## 10. Безопасность

| Поверхность | Мера |
|---|---|
| SQL | DuckDB allowlist (§4) + разрешены только читающие statements + таймаут + лимит длины |
| Пути | все пути строятся из `workspace_id` и `dataset_id`, проверенных по регулярному выражению; пользовательская строка никогда не участвует в сборке пути — приём взят из `ExperimentStore` AutoDataAnalysis |
| Загрузки | лимит на файл и на запрос, проверка расширения до записи, потоковая запись с прерыванием, `Path(name).name` |
| Содержимое файла | расширение не является доказательством; при ошибке чтения — понятное сообщение без внутренностей стека |
| Ошибки | наружу уходит код и сообщение; трассировка только в лог |
| Сеть | backend слушает `127.0.0.1`; CORS ограничен адресом frontend |

Аутентификации нет — это локальный инструмент, и это будет прямо сказано в README
как ограничение, а не умолчание.

---

## 11. Как проверяется, что архитектура соблюдается

- тест на граф импортов: `domain` не импортирует `api` и `adapters`;
- тест песочницы: набор попыток обхода SQL, все обязаны падать;
- round-trip-тест на каждый формат из реестра, значения `preserves_schema` сверяются с реальностью;
- performance-тесты с порогами на 10k / 100k / 1M — падают при регрессии;
- тест «DataArena работает без ModelArena».
