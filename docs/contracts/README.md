# Контракт DataArena ↔ ModelArena

Этот каталог — **источник истины** формата `dataarena.package/1`.
Он побайтово одинаков в репозиториях [DataArena](https://github.com/forit-tech/DataArena)
и [ModelArena](https://github.com/forit-tech/ModelArena). Расхождение — ошибка CI.

**Владелец контракта:** DataArena. Формат описывается там, где производится.
Замечания со стороны потребителя собираются в
[dataset-package-consumer.md](https://github.com/forit-tech/ModelArena/blob/main/docs/contracts/dataset-package-consumer.md)
и вносятся владельцем.

> ## Статус: FROZEN 1.0.0 — 2026-09-06
>
> Все шесть условий §J выполнены. Один и тот же набор из 14 фикстур независимо
> провалидирован обеими сторонами; реализация потребителя написана по этому документу
> и `reference/*.py` не импортирует. Отпечаток воспроизведён на трёх версиях Polars.
>
> Breaking-изменения — только через следующую major-версию.
> Тег: `contract/dataset-package-v1.0.0` в обоих репозиториях.

---

## Состав

| Путь | Что это |
|---|---|
| [`dataset-package-v1.md`](dataset-package-v1.md) | Нормативная спецификация |
| [`RECONCILIATION.md`](RECONCILIATION.md) | История согласования двух независимых предложений |
| [`schema/`](schema/) | JSON Schema draft 2020-12 на каждый файл пакета |
| [`reference/fingerprint.py`](reference/fingerprint.py) | Эталонная реализация `dataarena-logical-sha256-v1` |
| [`reference/validate_package.py`](reference/validate_package.py) | Эталонный валидатор: матрица §H |
| [`reference/build_golden.py`](reference/build_golden.py) | Детерминированная сборка эталонного пакета и фикстур |
| [`reference/run_matrix.py`](reference/run_matrix.py) | Producer-проверка: вся матрица фикстур одним запуском |
| [`reference/check_reproducibility.py`](reference/check_reproducibility.py) | Проверка J-6: отпечаток не зависит от версии библиотек |
| [`examples/`](examples/) | Файлы пакета по отдельности, для чтения глазами |
| [`golden/customers_golden.dapkg`](golden/) | Эталонный пакет, каталогом и `.zip` |
| [`golden/negative/`](golden/negative/) | Одиннадцать фикстур для негативных и краевых тестов |

## Проверка

```bash
python reference/run_matrix.py            # вся матрица, условие J-4
python reference/check_reproducibility.py # отпечаток, условие J-6
python reference/validate_package.py golden/customers_golden.dapkg   # один пакет
```

`run_matrix.py` показывает как **FAIL** фикстуру, которая появилась в каталоге, но не описана
в матрице ожиданий, и наоборот. Иначе пополнение набора прошло бы мимо проверки незамеченным.

### Матрица фикстур

Позитивные — обязаны быть приняты:

| Пакет | Результат |
|---|---|
| `customers_golden.dapkg` | ПРИНЯТ |
| `customers_golden.dapkg.zip` | ПРИНЯТ — каталог и архив эквивалентны |
| `negative/minimal.dapkg` | ПРИНЯТ — только манифест и одна часть данных |
| `negative/future-minor.dapkg` | ПРИНЯТ + warning про более новую minor-версию |
| `negative/sampled-profile.dapkg` | ПРИНЯТ + warning про выборочный профиль |

Негативные — обязаны быть отвергнуты с указанным кодом:

| Пакет | Код |
|---|---|
| `negative/future-major.dapkg` | `package_version_unsupported` |
| `negative/corrupted-part.dapkg` | `package_integrity_failed` |
| `negative/fingerprint-mismatch.dapkg` | `package_fingerprint_mismatch` |
| `negative/malformed-schema.dapkg` | `package_part_invalid` |
| `negative/missing-artifact.dapkg` | `package_part_missing` |
| `negative/duplicate-entry.dapkg` | `package_duplicate_entry` |
| `negative/schema-mismatch.dapkg` | `package_schema_mismatch` |
| `negative/traversal.dapkg.zip` | `package_unsafe_path` |
| `negative/zip-bomb.dapkg.zip` | `package_limit_exceeded` |

Все четырнадцать проверены **дважды**: эталонным валидатором DataArena и независимой
реализацией ModelArena, написанной по спецификации без чтения `reference/*.py`.
Оба прогона совпали с таблицей на Polars 1.20.0 и 1.44.1.

Две фикстуры заслуживают отдельного внимания:

- **`fingerprint-mismatch`** — у неё **корректный `sha256` всех файлов**: данные подменены,
  хеши честно пересчитаны. Её ловит только отпечаток содержимого. Это и есть причина,
  по которой в контракте два разных хеша.
- **`zip-bomb`** — коэффициент распаковки 675 : 1 при пределе 100 : 1. Проверяется
  **до** распаковки: после неё лимит уже не помогает.

## Правила изменения

1. Любое изменение формата → правка `dataset-package-v1.md` **и** соответствующей JSON Schema.
2. Пересобрать фикстуры: `python reference/build_golden.py golden`.
3. Прогнать матрицу и `check_reproducibility.py` на обеих сторонах.
4. Определить тип изменения по §E спецификации и поднять версию.
5. Синхронизировать каталог в оба репозитория и поставить общий тег.

Изменение, ломающее golden package, **обязано** сопровождаться повышением major-версии.

Отдельное правило для отпечатка: после freeze семантика внутри идентификатора алгоритма
**не меняется никогда**. Более удачная канонизация — это новый идентификатор, а не правка
существующего (§E). Отпечаток записан в карточках экспериментов и артефактах моделей;
смена смысла под тем же именем ломает их молча и далеко от места правки.

## Почему `reference/` не линтуется

Эталонные реализации — часть контракта, а не обычный код репозитория. Их изменение
по §E — версионированное событие: пересборка golden package, прогон матрицы обеими
сторонами, перенос тега. Линтер, запущенный на чьей-то машине, не должен уметь
инициировать такое событие правкой ради стиля.

Проверено, что было бы иначе: `ruff` находит здесь 12 замечаний, из них 8 —
о длине функций валидации и диспетчера типов. Разбивать матрицу проверок §H ради метрики
означало бы разнести по функциям порядок, который в контракте задан как единый и нормативный.

Корректность этого кода стережёт не стиль, а матрица из 14 фикстур и проверка
воспроизводимости отпечатка на разных версиях Polars — и то, и другое запускается в CI
обеих сторон.

## Зависимости эталонных реализаций

`polars`, `numpy`, `jsonschema`. Это инструменты контракта, а не зависимости приложений:
ни DataArena, ни ModelArena не обязаны импортировать код из этого каталога — они реализуют
контракт сами, а эталон служит для сверки.
