"""Умная сборка: связи между датасетами, опасные соединения, назначение и целевая переменная.

Всё считается по данным, а не по названиям:

* **связи** — по реальному пересечению значений (существующий подбор ключей) и точному
  прогнозу числа строк (существующий анализ соединения через группировку). Одинаковое
  имя колонки — только один из признаков, но не доказательство связи;
* **составные ключи** пробуются, когда одиночный ключ даёт связь «многие ко многим»:
  если добавление колонок с совпадающими именами убирает размножение строк,
  это показывается как отдельный кандидат;
* **вердикт** — по явным правилам: безопасно (строк не больше, чем в большей таблице),
  опасно (N:M, строк больше обеих таблиц), слабо (совпала малая доля строк), связи нет;
* **назначение и цель** — по типам, числу уникальных значений, пропускам и балансу
  классов. Это гипотезы, а не выбор: смысл задачи знает человек, и система показывает
  несколько вариантов с причинами и рисками утечки.

Ничего не создаёт и не меняет. Выполнение — существующее соединение, создающее новый
датасет с происхождением; откат — удаление созданного датасета (исходники не трогаются).
"""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path
from typing import Any

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.core.errors import AppError
from backend.domain.joins.models import JoinKind, JoinSpec, KeyPair
from backend.services.joins import analysis as key_analysis
from backend.services.joins import suggestions as key_suggestions
from backend.services.joins.execute import prepared

MAX_DATASETS = 8
PROFILE_SAMPLE = 100_000
TOP_SINGLE_KEYS = 3
MAX_COMPOSITE = 3
MAX_COMPOSITE_TRIES = 12
#совпала меньшая доля строк меньшей таблицы — связь слабая
WEAK_MATCH_SHARE = 0.1
CLASS_MIN = 2
CLASS_MAX = 20
ID_UNIQUE = 0.95
LEAK_CORRELATION = 0.98
MIN_ROWS_FOR_ML = 200

TARGET_HINTS = re.compile(
    r"(price|цен|cost|стоим|amount|сумм|revenue|выручк|income|доход|sales|продаж|target|label|класс|class|"
    r"status|статус|churn|отток|default|дефолт|fraud|accident|авари|outcome|result|итог|score|rating|оценк)",
    re.IGNORECASE,
)
ID_HINTS = re.compile(r"(^id$|_id$|^id_|\bid\b|номер|^no$|_no$|code$|код|uuid|phone|телефон|email)", re.IGNORECASE)


#── профиль ───────────────────────────────────────────────────────────────────


def _profile(lazy: pl.LazyFrame, rows: int) -> list[dict[str, Any]]:
    schema = lazy.collect_schema()
    sample = lazy.head(PROFILE_SAMPLE).collect()
    sampled = sample.height
    profile = []
    for name in schema.names():
        dtype = schema[name]
        column = sample[name]
        nulls = column.null_count()
        unique = column.drop_nulls().n_unique()
        non_null = max(sampled - nulls, 1)
        unique_ratio = unique / non_null
        role = _role(name, dtype, unique, unique_ratio, sampled)
        profile.append(
            {
                "name": name,
                "dtype": str(dtype),
                "null_ratio": round(nulls / sampled, 4) if sampled else 0.0,
                "unique": unique,
                "unique_ratio": round(unique_ratio, 4),
                "role": role,
            }
        )
    _ = rows
    return profile


def _role(name: str, dtype: pl.DataType, unique: int, unique_ratio: float, sampled: int) -> str:
    if unique <= 1:
        return "constant"
    if dtype.is_temporal():
        return "datetime"
    if dtype == pl.Boolean:
        return "boolean"
    if (unique_ratio >= ID_UNIQUE and sampled > CLASS_MAX) or (ID_HINTS.search(name) and unique_ratio > 0.5):  # noqa: PLR2004
        return "identifier"
    if dtype.is_numeric():
        return "numeric" if unique > CLASS_MAX else "categorical"
    return "categorical" if unique <= max(50, sampled // 20) else "text"


#── цель и назначение ─────────────────────────────────────────────────────────


def _targets(lazy: pl.LazyFrame, profile: list[dict[str, Any]], rows: int) -> list[dict[str, Any]]:
    sample = lazy.head(PROFILE_SAMPLE).collect()
    found: list[dict[str, Any]] = []
    for column in profile:
        name, role = column["name"], column["role"]
        if role in ("identifier", "constant", "text", "datetime") or column["null_ratio"] > 0.5:  # noqa: PLR2004
            continue
        task: str | None = None
        balance: float | None = None
        if role in ("categorical", "boolean") and CLASS_MIN <= column["unique"] <= CLASS_MAX:
            task = "classification"
            counts = sample[name].drop_nulls().value_counts()
            share = counts["count"] / counts["count"].sum()
            balance = round(float(share.min()), 4) if counts.height else None
        elif role == "numeric":
            task = "regression"
        if task is None:
            continue
        hinted = bool(TARGET_HINTS.search(name))
        warnings: list[str] = []
        if column["null_ratio"] > 0:
            warnings.append(f"Пропусков в цели {column['null_ratio']:.0%}: строки без цели в обучение не войдут.")
        if balance is not None and balance < 0.05:  # noqa: PLR2004
            warnings.append(
                f"Редкий класс — {balance:.1%} строк: нужна стратификация и метрики, устойчивые к дисбалансу."
            )
        if rows < MIN_ROWS_FOR_ML:
            warnings.append(f"Всего {rows} строк — для обучения и честной проверки этого мало.")
        leaks = _leak_suspects(sample, name, profile)
        found.append(
            {
                "column": name,
                "task": task,
                "classes": column["unique"] if task == "classification" else None,
                "minority_share": balance,
                "null_ratio": column["null_ratio"],
                "name_hint": hinted,
                "leakage_suspects": leaks,
                "warnings": warnings,
                #ранг — порядок показа, а не утверждение «лучшая цель»: подсказка по имени
                #поднимает выше, пропуски и дисбаланс опускают
                "rank_score": round(
                    (0.5 if hinted else 0.0)
                    + (0.3 if task == "regression" else 0.25)
                    - column["null_ratio"]
                    - (0.2 if balance is not None and balance < 0.05 else 0.0),  # noqa: PLR2004
                    3,
                ),
            }
        )
    found.sort(key=lambda item: item["rank_score"], reverse=True)
    return found[:6]


def _leak_suspects(sample: pl.DataFrame, target: str, profile: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Признаки, подозрительно похожие на цель: общий корень имени или почти полная корреляция."""
    suspects: list[dict[str, str]] = []
    root = _root(target)
    for column in profile:
        name = column["name"]
        if name == target:
            continue
        if root and len(root) >= 3 and root in _root(name):  # noqa: PLR2004
            suspects.append(
                {"column": name, "reason": "имя похоже на цель — возможно, это та же величина в другой форме"}
            )
            continue
        if column["role"] == "numeric" and sample[target].dtype.is_numeric():
            pair = sample.select(target, name).drop_nulls()
            if pair.height > 10:  # noqa: PLR2004
                correlation = pair.select(pl.corr(target, name)).item()
                if correlation is not None and not math.isnan(correlation) and abs(correlation) >= LEAK_CORRELATION:
                    suspects.append(
                        {"column": name, "reason": f"почти полная корреляция с целью (r = {correlation:.3f})"}
                    )
    return suspects


def _root(name: str) -> str:
    return re.sub(r"[^a-zа-я]", "", name.lower())[:6]


def _purposes(profile: list[dict[str, Any]], targets: list[dict[str, Any]], rows: int) -> list[dict[str, Any]]:
    roles = [column["role"] for column in profile]
    dims = roles.count("categorical") + roles.count("boolean")
    measures = roles.count("numeric")
    has_time = "datetime" in roles
    result = []
    if dims and measures:
        result.append(
            {
                "purpose": "analytics",
                "title": "Аналитика и отчёты",
                "reason": f"Есть разрезы ({dims}) и показатели ({measures}): группировки и сводные таблицы.",
            }
        )
    if any(t["task"] == "classification" for t in targets) and rows >= MIN_ROWS_FOR_ML:
        result.append(
            {
                "purpose": "classification",
                "title": "Классификация",
                "reason": "Есть колонки с небольшим числом классов — кандидаты в цель.",
            }
        )
    if any(t["task"] == "regression" for t in targets) and rows >= MIN_ROWS_FOR_ML:
        result.append(
            {
                "purpose": "regression",
                "title": "Регрессия",
                "reason": "Есть числовые колонки — кандидаты в непрерывную цель.",
            }
        )
    if has_time and measures:
        result.append(
            {
                "purpose": "forecasting",
                "title": "Прогнозирование во времени",
                "reason": "Есть дата и числовые показатели. Нужна агрегация по периодам и разбиение по времени.",
            }
        )
    if measures >= 2 and rows >= MIN_ROWS_FOR_ML:  # noqa: PLR2004
        result.append(
            {
                "purpose": "clustering",
                "title": "Кластеризация и поиск аномалий",
                "reason": "Несколько числовых признаков — можно искать группы и нетипичные строки без цели.",
            }
        )
    return result


#── связи ─────────────────────────────────────────────────────────────────────


def _relation(
    left: pl.LazyFrame, right: pl.LazyFrame, left_id: str, right_id: str, keys: tuple[KeyPair, ...]
) -> dict[str, Any] | None:
    spec = JoinSpec(left_dataset_id=left_id, right_dataset_id=right_id, keys=keys, kind=JoinKind.INNER)
    try:
        analysis = key_analysis.analyse(left, right, spec)
    except AppError:
        return None
    left_rows, right_rows = analysis.left_rows, analysis.right_rows
    matched = min(analysis.matched_left_rows / max(left_rows, 1), analysis.matched_right_rows / max(right_rows, 1))
    smaller_matched = max(
        analysis.matched_left_rows / max(left_rows, 1), analysis.matched_right_rows / max(right_rows, 1)
    )
    estimated = analysis.estimated_rows
    cardinality = analysis.cardinality.value
    if estimated == 0:
        verdict, text = "none", "Ни одной пары: значения ключа не совпадают."
    elif cardinality == "N:M" and estimated > max(left_rows, right_rows):
        verdict = "dangerous"
        text = (
            f"Связь «многие ко многим»: строк станет {estimated:,} — больше, чем в обеих таблицах "
            f"({left_rows:,} и {right_rows:,}). Скорее всего, таблицы описывают сущности разной "
            "детализации (например, модели и отдельные продажи), и построчное соединение размножит строки."
        ).replace(",", " ")
    elif cardinality == "N:M":
        #«многие ко многим» безопасной не бывает: ключ не определяет сущность ни с одной стороны,
        #и то, что строк не стало больше, — совпадение этих данных, а не свойство связи
        verdict = "weak"
        text = (
            "Связь «многие ко многим»: ключ не определяет запись ни с одной стороны. Число строк сейчас "
            "не выросло, но пары строк сопоставлены произвольно."
        )
    elif smaller_matched < WEAK_MATCH_SHARE:
        verdict = "weak"
        text = f"Пару находит лишь {smaller_matched:.0%} строк: связь слабая или ключ не тот."
    else:
        verdict = "safe"
        text = f"Связь {cardinality}: строк станет {estimated:,}, без размножения.".replace(",", " ")
    return {
        "keys": [key.to_dict() for key in keys],
        "cardinality": cardinality,
        "estimated_rows": estimated,
        "left_rows": left_rows,
        "right_rows": right_rows,
        "matched_left_rows": analysis.matched_left_rows,
        "matched_right_rows": analysis.matched_right_rows,
        "match_share": round(matched, 4),
        "verdict": verdict,
        "explanation": text,
    }


def _plausible_key(suggestion: Any) -> bool:
    """Дробные числа — измерения, а не ключи: совпадение значений у них случайно.

    Найдено сравнением с локальной моделью: «мощность» и «сумма продажи» пересекались
    по значениям и получали связь 1:1 с высокой уверенностью. Если имена колонок
    совпадают (например, «price» в обеих таблицах), пара остаётся — решает человек.
    """
    floats = suggestion.left_type.startswith("Float") and suggestion.right_type.startswith("Float")
    return not floats or key_suggestions._same_name(suggestion.left, suggestion.right)  # noqa: SLF001


def _shared_columns(left: pl.Schema, right: pl.Schema) -> list[tuple[str, str]]:
    def norm(name: str) -> str:
        return re.sub(r"[^a-zа-я0-9]", "", name.lower())

    by_norm = {norm(name): name for name in right.names()}
    pairs = []
    for name in left.names():
        other = by_norm.get(norm(name))
        if other is not None and key_suggestions._comparable(left[name], right[other]):  # noqa: SLF001
            pairs.append((name, other))
    return pairs


def _pair(left_item: dict[str, Any], right_item: dict[str, Any]) -> dict[str, Any]:
    left, right = left_item["lazy"], right_item["lazy"]
    left_schema, right_schema = left.collect_schema(), right.collect_schema()
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    for suggestion in [item for item in key_suggestions.suggest(left, right) if _plausible_key(item)][:TOP_SINGLE_KEYS]:
        keys: tuple[KeyPair, ...] = (KeyPair(suggestion.left, suggestion.right),)
        relation = _relation(left, right, left_item["id"], right_item["id"], keys)
        if relation:
            relation["reasons"] = list(suggestion.reasons)
            relation["confidence"] = round(suggestion.confidence, 3)
            candidates.append(relation)
            seen.add(tuple((k.left, k.right) for k in keys))

    #составной ключ: одиночный дал N:M — пробуем добавить колонки с совпадающими именами
    shared = _shared_columns(left_schema, right_schema)
    tries = 0
    for base in [c for c in candidates if c["verdict"] == "dangerous"]:
        base_pair = (base["keys"][0]["left"], base["keys"][0]["right"])
        #сначала пробуются колонки с наибольшим числом разных значений (модель, год),
        #а не почти константные (коробка передач, цвет): они различают сущности
        extra = sorted(
            (pair for pair in shared if pair != base_pair),
            key=lambda pair: -left_item["unique"].get(pair[0], 0),
        )
        for size in range(1, MAX_COMPOSITE):
            for combo in itertools.combinations(extra, size):
                if tries >= MAX_COMPOSITE_TRIES:
                    break
                tries += 1
                pairs = (base_pair, *combo)
                if pairs in seen:
                    continue
                seen.add(pairs)
                keys = tuple(KeyPair(a, b) for a, b in pairs)
                relation = _relation(left, right, left_item["id"], right_item["id"], keys)
                if relation and relation["estimated_rows"] < base["estimated_rows"] and relation["estimated_rows"] > 0:
                    relation["reasons"] = [
                        "составной ключ: добавлены колонки с совпадающими именами, чтобы убрать размножение строк",
                        "это гипотеза — проверьте, что колонки действительно описывают одну и ту же сущность",
                    ]
                    relation["confidence"] = None
                    candidates.append(relation)

    order = {"safe": 0, "weak": 1, "dangerous": 2, "none": 3}
    candidates.sort(key=lambda c: (order[c["verdict"]], -(c["match_share"]), c["estimated_rows"]))
    best = candidates[0] if candidates else None
    if best is None:
        verdict = "none"
        advice = (
            "Надёжной связи не найдено: общих значений в похожих колонках нет. Таблицы лучше анализировать отдельно."
        )
    elif best["verdict"] == "safe":
        verdict = "join"
        advice = "Можно соединять по предложенному ключу: строки не размножаются."
    elif any(c["cardinality"] == "N:M" for c in candidates) and best["verdict"] != "safe":
        verdict = "aggregate"
        advice = (
            "Не соединяйте построчно: любой найденный ключ размножает строки. Варианты: свести одну из таблиц "
            "до уровня другой (например, средняя цена и число продаж по модели) и соединять уже агрегат; "
            "или оставить таблицы раздельными и сравнивать их в аналитике."
        )
    else:
        verdict = "review"
        advice = "Связь есть, но совпадает малая доля строк. Проверьте ключ и написание значений (регистр, пробелы)."
    return {
        "left_dataset_id": left_item["id"],
        "right_dataset_id": right_item["id"],
        "left_name": left_item["name"],
        "right_name": right_item["name"],
        "verdict": verdict,
        "advice": advice,
        "candidates": candidates[:5],
    }


def discover(store: WorkspaceStore, workspace_root: Path, workspace_id: str) -> dict[str, Any]:
    datasets = [d for d in store.list_datasets(workspace_id) if d.status.value == "ready"][:MAX_DATASETS]
    items = []
    for dataset in datasets:
        #каждый датасет читается с диска один раз: дальше все подсчёты идут по кадру в памяти.
        #Прежде каждый анализ пары заново читал файл, и подбор на трёх таблицах шёл минутами
        frame = prepared(store, workspace_root, workspace_id, dataset.dataset_id)[0].collect()
        lazy = frame.lazy()
        rows = frame.height
        profile = _profile(lazy, rows)
        targets = _targets(lazy, profile, rows)
        items.append(
            {
                "id": dataset.dataset_id,
                "name": dataset.name,
                "lazy": lazy,
                "rows": rows,
                "profile": profile,
                "targets": targets,
                "purposes": _purposes(profile, targets, rows),
                "unique": {column["name"]: column["unique"] for column in profile},
            }
        )

    pairs = [_pair(a, b) for a, b in itertools.combinations(items, 2)]
    return {
        "datasets": [
            {
                "dataset_id": item["id"],
                "name": item["name"],
                "rows": item["rows"],
                "columns": item["profile"],
                "purposes": item["purposes"],
                "targets": item["targets"],
            }
            for item in items
        ],
        "pairs": pairs,
        "limits": {"max_datasets": MAX_DATASETS, "profile_sample": PROFILE_SAMPLE},
        "note": (
            "Связи и прогноз числа строк посчитаны по данным. Назначение и цель — гипотезы: "
            "смысл задачи и то, известен ли признак в момент прогноза, решает человек."
        ),
    }
