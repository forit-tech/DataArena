"""Разбор измеренных характеристик моделей в матрицу решений.

Источники:

* **лидерборд ModelArena** (`GET /api/arena/runs/{run_id}/leaderboard`) — один прогон,
  то есть один датасет, одна задача и один протокол по построению. Берутся метрики
  кросс-валидации со своим разбросом по фолдам, wall-clock процесса участника
  и размер сохранённого `model.joblib`. **Время инференса ModelArena не измеряет** —
  оно не выдумывается, а может быть добавлено отдельным столбцом;
* **JSON-матрица** `{"criteria": [...], "alternatives": [...], "context": {...}}`;
* **CSV**: строка — альтернатива, столбец — критерий. Направление задаётся суффиксом
  `:max` / `:min` в заголовке или выводится по известным именам; столбец `X_std` —
  неопределённость столбца `X`.

Отсутствующее значение остаётся отсутствующим. Пустая ячейка не превращается в ноль.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from typing import Any

from backend.domain.decision.models import DecisionInputError

MAX_IMPORT_ROWS = 200

#имена, направление которых однозначно; остальные требуют суффикса :max/:min
_KNOWN: dict[str, tuple[str, str, str, str]] = {
    #ключ: (направление, группа, подпись, единица)
    "roc_auc": ("max", "quality", "ROC-AUC", ""),
    "pr_auc": ("max", "quality", "PR-AUC", ""),
    "average_precision": ("max", "quality", "Average precision", ""),
    "f1": ("max", "quality", "F1", ""),
    "f1_macro": ("max", "quality", "F1 macro", ""),
    "f1_weighted": ("max", "quality", "F1 weighted", ""),
    "accuracy": ("max", "quality", "Accuracy", ""),
    "balanced_accuracy": ("max", "quality", "Balanced accuracy", ""),
    "precision": ("max", "quality", "Precision", ""),
    "recall": ("max", "quality", "Recall", ""),
    "r2": ("max", "quality", "R²", ""),
    "log_loss": ("min", "quality", "Log loss", ""),
    "brier": ("min", "quality", "Brier", ""),
    "rmse": ("min", "quality", "RMSE", ""),
    "mae": ("min", "quality", "MAE", ""),
    "mape": ("min", "quality", "MAPE", ""),
    "inference_ms": ("min", "time", "Инференс", "мс"),
    "latency_ms": ("min", "time", "Задержка", "мс"),
    "train_seconds": ("min", "time", "Обучение", "с"),
    "fit_seconds": ("min", "time", "Обучение", "с"),
    "model_mb": ("min", "size", "Размер модели", "МБ"),
    "size_mb": ("min", "size", "Размер модели", "МБ"),
    "memory_mb": ("min", "memory", "Память", "МБ"),
    "cost": ("min", "cost", "Стоимость", ""),
    "cv_std": ("min", "stability", "Разброс по фолдам", ""),
}

_KEY_PATTERN = re.compile(r"[^0-9a-zA-Zа-яА-Я_]+")


@dataclass
class ImportedMatrix:
    source: str
    criteria: list[dict[str, Any]]
    alternatives: list[dict[str, Any]]
    primary: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "criteria": self.criteria,
            "alternatives": self.alternatives,
            "primary": self.primary,
            "context": self.context,
            "notes": self.notes,
        }


def _criterion(key: str, direction: str | None = None, label: str | None = None) -> dict[str, Any]:
    known = _KNOWN.get(key.lower())
    resolved = direction or (known[0] if known else None)
    if resolved not in ("max", "min"):
        raise DecisionInputError(
            f"Неизвестно направление критерия «{key}». Укажите его суффиксом в заголовке: «{key}:max» или «{key}:min»."
        )
    return {
        "key": key,
        "label": label or (known[2] if known else key),
        "direction": resolved,
        "unit": known[3] if known else "",
        "group": known[1] if known else "",
        "weight": 1.0 if (known and known[1] == "quality") else 0.0,
    }


def _number(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise DecisionInputError("Логическое значение не может быть измерением.")
    if isinstance(raw, int | float):
        value = float(raw)
    else:
        text = str(raw).strip().replace(" ", "").replace(",", ".")
        if text == "" or text.lower() in {"na", "nan", "null", "none", "—", "-"}:
            return None
        try:
            value = float(text)
        except ValueError as error:
            raise DecisionInputError(f"Значение «{raw}» не является числом.") from error
    return value if math.isfinite(value) else None


def _key(raw: str) -> str:
    return _KEY_PATTERN.sub("_", raw.strip()).strip("_") or "criterion"


def detect_and_parse(name: str, content: bytes) -> ImportedMatrix:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DecisionInputError("Файл должен быть в кодировке UTF-8.") from error

    if name.lower().endswith(".csv") or name.lower().endswith(".tsv"):
        return parse_csv(text, delimiter="\t" if name.lower().endswith(".tsv") else None)

    import json

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise DecisionInputError(f"JSON не читается: {error.msg} (строка {error.lineno}).") from error
    return parse_json(payload)


def parse_json(payload: Any) -> ImportedMatrix:
    if not isinstance(payload, dict):
        raise DecisionInputError("Ожидался JSON-объект: лидерборд ModelArena или матрица {criteria, alternatives}.")
    if "rows" in payload and "objective" in payload:
        return parse_modelarena_leaderboard(payload)
    if "criteria" in payload and "alternatives" in payload:
        return parse_matrix(payload)
    raise DecisionInputError(
        "Формат не распознан: нужен лидерборд ModelArena (поля rows и objective) "
        "или матрица (поля criteria и alternatives)."
    )


def parse_matrix(payload: dict[str, Any]) -> ImportedMatrix:
    criteria_raw = payload.get("criteria")
    alternatives_raw = payload.get("alternatives")
    if not isinstance(criteria_raw, list) or not isinstance(alternatives_raw, list):
        raise DecisionInputError("Поля criteria и alternatives должны быть списками.")
    if len(alternatives_raw) > MAX_IMPORT_ROWS:
        raise DecisionInputError(f"Альтернатив больше {MAX_IMPORT_ROWS}.")

    criteria = []
    for item in criteria_raw:
        if not isinstance(item, dict) or not item.get("key"):
            raise DecisionInputError("Каждый критерий должен иметь поле key.")
        base = _criterion(str(item["key"]), item.get("direction"), item.get("label"))
        base.update({k: item[k] for k in ("unit", "group", "weight") if k in item})
        criteria.append(base)

    keys = {item["key"] for item in criteria}
    alternatives = []
    for index, item in enumerate(alternatives_raw):
        if not isinstance(item, dict):
            raise DecisionInputError("Каждая альтернатива должна быть объектом.")
        values: dict[str, dict[str, Any]] = {}
        raw_values = item.get("values") or {}
        if not isinstance(raw_values, dict):
            raise DecisionInputError("Поле values альтернативы должно быть объектом.")
        for key in keys:
            raw = raw_values.get(key)
            if isinstance(raw, dict):
                values[key] = {
                    "value": _number(raw.get("value")),
                    "std": _number(raw.get("std")),
                    "note": str(raw.get("note") or ""),
                }
            else:
                values[key] = {"value": _number(raw), "std": None, "note": ""}
        key = str(item.get("key") or f"alt_{index + 1}")
        alternatives.append(
            {"key": key, "label": str(item.get("label") or key), "values": values, "meta": dict(item.get("meta") or {})}
        )

    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    primary = payload.get("primary")
    return ImportedMatrix(
        source="matrix",
        criteria=criteria,
        alternatives=alternatives,
        primary=str(primary) if primary in keys else None,
        context=dict(context or {}),
    )


def parse_csv(text: str, delimiter: str | None = None) -> ImportedMatrix:
    if delimiter is None:
        sample = text[:4096]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise DecisionInputError("В CSV нужна строка заголовка и хотя бы одна альтернатива.")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise DecisionInputError(f"Альтернатив больше {MAX_IMPORT_ROWS}.")

    header = [cell.strip() for cell in rows[0]]
    name_index = next(
        (
            i
            for i, cell in enumerate(header)
            if cell.lower() in {"model", "name", "label", "модель", "alternative", "key"}
        ),
        0,
    )
    columns: list[tuple[int, str, str | None]] = []
    std_columns: dict[str, int] = {}
    for index, cell in enumerate(header):
        if index == name_index:
            continue
        raw, _, suffix = cell.partition(":")
        key = _key(raw)
        if key.lower().endswith("_std"):
            std_columns[key[:-4]] = index
            continue
        columns.append((index, key, suffix.strip().lower() or None))

    criteria = [{**_criterion(key, direction, None), "weight": 1.0} for _, key, direction in columns]
    alternatives = []
    for number, row in enumerate(rows[1:], start=1):
        label = row[name_index].strip() if name_index < len(row) else ""
        values = {}
        for index, key, _ in columns:
            std_index = std_columns.get(key)
            values[key] = {
                "value": _number(row[index]) if index < len(row) else None,
                "std": _number(row[std_index]) if std_index is not None and std_index < len(row) else None,
                "note": "",
            }
        alternatives.append(
            {"key": f"row_{number}", "label": label or f"Альтернатива {number}", "values": values, "meta": {}}
        )

    primary = next((item["key"] for item in criteria if item["group"] == "quality"), None)
    return ImportedMatrix(source="csv", criteria=criteria, alternatives=alternatives, primary=primary)


def parse_modelarena_leaderboard(payload: dict[str, Any]) -> ImportedMatrix:
    rows = payload.get("rows")
    objective = payload.get("objective") or {}
    if not isinstance(rows, list) or not rows:
        raise DecisionInputError("В лидерборде ModelArena нет участников.")

    metric_info: dict[str, dict[str, Any]] = {}
    for row in rows:
        for metric in row.get("cross_validated") or []:
            key = metric.get("key")
            if key and key not in metric_info:
                metric_info[key] = metric

    criteria = []
    for key, metric in metric_info.items():
        direction = "max" if metric.get("direction") == "higher" else "min"
        item = _criterion(key, direction, metric.get("label") or None)
        item["group"] = "quality"
        item["weight"] = 0.0
        criteria.append(item)

    primary = objective.get("metric") if objective.get("metric") in metric_info else None
    for item in criteria:
        if item["key"] == primary:
            item["weight"] = 1.0

    criteria.append({**_criterion("train_seconds", "min", "Время процесса участника"), "weight": 0.0})
    criteria.append({**_criterion("model_mb", "min", "Размер артефакта"), "weight": 0.0})
    criteria.append({**_criterion("cv_std", "min", "Разброс основной метрики по фолдам"), "weight": 0.0})

    alternatives = []
    unsucceeded = 0
    for row in rows:
        metrics = {item.get("key"): item for item in row.get("cross_validated") or []}
        values: dict[str, dict[str, Any]] = {}
        for key in metric_info:
            metric = metrics.get(key) or {}
            values[key] = {
                "value": _number(metric.get("value")),
                #разброс по фолдам используется как неопределённость среднего —
                #консервативно: стандартная ошибка была бы меньше в √k раз, но фолды
                #коррелированы, и честная ошибка лежит между этими величинами
                "std": _number(metric.get("std")),
                "note": str(metric.get("note") or ""),
            }
        model_bytes = row.get("model_bytes")
        values["train_seconds"] = {
            "value": _number(row.get("elapsed_seconds")),
            "std": None,
            "note": "wall-clock всего процесса: фолды, финальное обучение и запись артефакта",
        }
        values["model_mb"] = {
            "value": None if model_bytes is None else round(int(model_bytes) / 1_000_000, 6),
            "std": None,
            "note": "размер model.joblib (compress=3)",
        }
        values["cv_std"] = {"value": _number(row.get("std")), "std": None, "note": ""}
        if not metrics:
            unsucceeded += 1
        alternatives.append(
            {
                "key": str(row.get("contender_key")),
                "label": str(row.get("label") or row.get("contender_key")),
                "values": values,
                "meta": {
                    "family": str(row.get("family") or ""),
                    "baseline": "да" if row.get("is_baseline") else "",
                    "modelarena_rank": "" if row.get("rank") is None else str(row.get("rank")),
                    "reason": str(row.get("reason") or ""),
                },
            }
        )

    notes = [
        "Все участники взяты из одного прогона ModelArena: датасет, задача, фолды и seed у них общие.",
        "Время инференса ModelArena не измеряет. Его можно добавить столбцом через CSV-импорт.",
        "«Время процесса участника» — wall-clock всего процесса, а не чистое время обучения.",
    ]
    if unsucceeded:
        notes.append(
            f"{unsucceeded} участн. без метрик (не обучились или не запускались) — они видны, но не сравниваются."
        )
    champion = (payload.get("champion") or {}).get("contender_key")
    return ImportedMatrix(
        source="modelarena",
        criteria=criteria,
        alternatives=alternatives,
        primary=primary,
        context={
            "objective_metric": objective.get("metric"),
            "modelarena_champion": champion,
            "baseline_key": payload.get("baseline_key"),
        },
        notes=notes,
    )
