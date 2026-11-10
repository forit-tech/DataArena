"""Расширенный измерительный стенд: открытые датасеты OpenML.

    python scripts/research/measure_openml.py --out docs/research/data_openml

Те же модели, протокол и измерения, что в measure_models.py (5 фолдов, seed 42, качество,
время обучения, инференс на 1000 строк, размер pickle). Отличия, вызванные данными:

* категориальные признаки кодируются one-hot **до** разбиения на фолды. Это не утечка
  целевой переменной: кодирование не смотрит на цель и не обучается на статистиках
  (только перечень категорий), но формально набор столбцов известен по всему датасету;
* пропуски заполняются медианой **внутри** Pipeline — на train-части каждого фолда;
* в бинарных задачах положительным считается более редкий класс (так ROC-AUC и F1
  описывают распознавание «интересного» класса); соответствие записывается в файл;
* для каждого датасета записываются идентификатор, версия и лицензия OpenML.

Датасеты скачиваются один раз в кэш scikit-learn (~/scikit_learn_data).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.datasets import fetch_openml
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_models import CRITERIA, FOLDS, SEED, classifiers, environment, measure, regressors, splitter_name

#(OpenML data_id, имя в результатах, задача). Отбор: известные открытые наборы
#с разной предметной областью и размером, без многоклассовых целей
SUITE: list[tuple[int, str, str]] = [
    (31, "credit_g", "binary"),
    (40981, "australian_credit", "binary"),
    (37, "pima_diabetes", "binary"),
    (1464, "blood_transfusion", "binary"),
    (1462, "banknote", "binary"),
    (1489, "phoneme", "binary"),
    (1067, "kc1", "binary"),
    (1049, "pc4", "binary"),
    (1480, "ilpd", "binary"),
    (1494, "qsar_biodeg", "binary"),
    (44, "spambase", "binary"),
    (1487, "ozone_8hr", "binary"),
    (1467, "climate_crashes", "binary"),
    (189, "kin8nm", "regression"),
    (197, "cpu_act", "regression"),
]


def load(data_id: int, task: str) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    bunch = fetch_openml(data_id=data_id, as_frame=True, parser="auto")
    frame: pd.DataFrame = bunch.data
    categorical = [c for c in frame.columns if not pd.api.types.is_numeric_dtype(frame[c])]
    x = pd.get_dummies(frame, columns=categorical, dtype=float).to_numpy(dtype=float)
    target = bunch.target
    info: dict[str, Any] = {
        "openml_id": data_id,
        "openml_name": bunch.details.get("name"),
        "openml_version": bunch.details.get("version"),
        "licence": bunch.details.get("licence"),
        "categorical_features": len(categorical),
        "missing_cells": int(frame.isna().sum().sum()),
    }
    if task == "regression":
        y = pd.to_numeric(target).to_numpy(dtype=float)
    else:
        counts = target.value_counts()
        positive = counts.index[-1]
        y = (target == positive).to_numpy().astype(int)
        info["positive_class"] = str(positive)
        info["positive_share"] = round(float(y.mean()), 4)
    return x, y, info


def with_imputer(models: dict[str, tuple[str, Any]]) -> dict[str, tuple[str, Any]]:
    return {
        key: (label, make_pipeline(SimpleImputer(strategy="median"), model)) for key, (label, model) in models.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("docs/research/data_openml"))
    parser.add_argument("--only", nargs="*", help="имена датасетов из SUITE")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    env = environment()

    for data_id, name, task in SUITE:
        if args.only and name not in args.only:
            continue
        if (args.out / f"{name}.json").exists():
            print(f"{name}: уже измерен, пропуск")
            continue
        started = time.perf_counter()
        x, y, info = load(data_id, task)
        models = with_imputer(regressors() if task == "regression" else classifiers())
        alternatives = []
        for key, (label, model) in models.items():
            row = measure(task, x, y, label, model)
            alternatives.append({"key": key, **row, "meta": {"family": type(model.steps[-1][1]).__name__}})
        keys = list(alternatives[0]["values"].keys())
        matrix = {
            "criteria": [
                {
                    "key": key,
                    "label": CRITERIA[key][0],
                    "direction": CRITERIA[key][1],
                    "unit": CRITERIA[key][2],
                    "group": CRITERIA[key][3],
                }
                for key in keys
            ],
            "alternatives": alternatives,
            "primary": "r2" if task == "regression" else "roc_auc",
            "context": {
                "dataset": name,
                "task": task,
                "origin": f"OpenML #{data_id}",
                "rows": len(y),
                "features": int(x.shape[1]),
                "protocol": f"{FOLDS}-fold {splitter_name(task)}, shuffle, seed {SEED}",
                "openml": info,
                "environment": env,
            },
        }
        (args.out / f"{name}.json").write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name:20s} {len(y):6d} строк {x.shape[1]:4d} признаков  {time.perf_counter() - started:7.1f} c")


if __name__ == "__main__":
    main()
