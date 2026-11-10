"""Decision Lab: сборка задачи выбора из запроса, импорт матриц, чтение ModelArena.

Хранилища у раздела нет намеренно: матрица — это измерения, которые принадлежат
ModelArena или файлу пользователя. Вторая копия тех же результатов здесь быстро
разошлась бы с источником. Воспроизводимость обеспечивает отчёт: в нём лежат
входная матрица, параметры, seed и отпечаток входа.
"""

from __future__ import annotations

from typing import Any

from backend.adapters.modelarena import client as modelarena
from backend.core.config import get_settings
from backend.domain.decision.analysis import analyse
from backend.domain.decision.importers import detect_and_parse, parse_modelarena_leaderboard
from backend.domain.decision.models import (
    Alternative,
    Constraint,
    Criterion,
    DecisionInputError,
    DecisionProblem,
    Measurement,
    ScaleOverride,
    SensitivitySettings,
)

MAX_IMPORT_BYTES = 5 * 1024 * 1024


def build_problem(payload: dict[str, Any]) -> DecisionProblem:
    criteria = tuple(
        Criterion(
            key=item["key"],
            label=item["label"],
            direction=item["direction"],
            weight=float(item["weight"]),
            unit=item.get("unit", ""),
            group=item.get("group", ""),
        )
        for item in payload["criteria"]
    )
    keys = {item.key for item in criteria}
    alternatives = tuple(
        Alternative(
            key=item["key"],
            label=item["label"],
            #значения по невыбранным критериям не участвуют: матрица строится только
            #по тому, что пользователь решил сравнивать
            values={
                key: Measurement(value.get("value"), value.get("std"), value.get("note", ""))
                for key, value in item["values"].items()
                if key in keys
            },
            meta=dict(item.get("meta") or {}),
        )
        for item in payload["alternatives"]
    )
    constraints = tuple(
        Constraint(item["criterion"], item["op"], float(item["bound"])) for item in payload.get("constraints", [])
    )
    sensitivity = SensitivitySettings(**payload.get("sensitivity", {}))
    return DecisionProblem(
        criteria=criteria,
        alternatives=alternatives,
        constraints=constraints,
        normalization=payload.get("normalization", "vector"),
        primary=payload.get("primary"),
        sensitivity=sensitivity,
        scales=tuple(
            ScaleOverride(item["criterion"], item["kind"], float(item["worst"]), float(item["best"]))
            for item in payload.get("scales", [])
            if item["criterion"] in keys
        ),
    )


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    return analyse(build_problem(payload))


def import_file(name: str, content: bytes) -> dict[str, Any]:
    if len(content) > MAX_IMPORT_BYTES:
        raise DecisionInputError("Файл матрицы больше 5 МБ: для матрицы альтернатив это явно не тот файл.")
    if not content.strip():
        raise DecisionInputError("Файл пуст.")
    return detect_and_parse(name, content).to_dict()


def modelarena_runs() -> dict[str, Any]:
    settings = get_settings()
    return {"base_url": settings.modelarena_base_url, "runs": modelarena.list_runs(settings.modelarena_base_url)}


def modelarena_matrix(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    leaderboard = modelarena.fetch_leaderboard(settings.modelarena_base_url, run_id)
    imported = parse_modelarena_leaderboard(leaderboard).to_dict()
    imported["context"]["run_id"] = run_id
    for item in imported["alternatives"]:
        item["meta"]["run_id"] = run_id
    return imported
