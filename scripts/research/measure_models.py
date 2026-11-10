"""Измерение характеристик ML-моделей для исследовательского стенда Decision Lab.

Запуск требует scikit-learn и **не** входит в зависимости DataArena: DataArena моделей
не обучает. Скрипт — отдельный измерительный стенд, который выдаёт матрицы решений
в формате, принимаемом `POST /api/decision/import` (JSON-матрица).

    python scripts/research/measure_models.py --out docs/research/data

Датасеты встроены в scikit-learn и не скачиваются:

* breast_cancer — Wisconsin Diagnostic Breast Cancer (UCI), бинарная классификация;
* digits_binary — UCI optical digits; производная бинарная задача «цифра ≥ 5»;
* wine — UCI Wine, многоклассовая классификация (3 класса);
* diabetes — Efron et al. (2004), регрессия.

Для каждой модели измеряется:

* качество: среднее и стандартное отклонение по 5 фолдам (стратифицированным
  для классификации), seed 42; модели внутри Pipeline, поэтому масштабирование
  обучается только на train-части фолда — утечки test → train нет;
* fit_seconds: среднее время обучения на фолде (perf_counter), std — по фолдам;
* inference_ms: время предсказания в пересчёте на 1000 строк, медиана 5 повторов
  на test-части каждого фолда, затем среднее по фолдам; std — по фолдам;
* model_mb: размер pickle модели, обученной на всём датасете (bytes / 10⁶).

Время зависит от машины: окружение записывается в каждый файл, и сравнивать
времена между файлами, снятыми на разных машинах, нельзя.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import platform
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import sklearn
from sklearn.datasets import load_breast_cancer, load_diabetes, load_digits, load_wine
from sklearn.ensemble import (
    GradientBoostingClassifier,
    GradientBoostingRegressor,
    RandomForestClassifier,
    RandomForestRegressor,
)
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    mean_absolute_error,
    r2_score,
    roc_auc_score,
    root_mean_squared_error,
)
from sklearn.model_selection import KFold, StratifiedKFold
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, SVR
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

SEED = 42
FOLDS = 5
REPEATS = 5


def classifiers() -> dict[str, tuple[str, Any]]:
    return {
        "logreg": ("Логистическая регрессия", make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))),
        "tree": ("Дерево решений", DecisionTreeClassifier(random_state=SEED)),
        "rf": ("Случайный лес (300)", RandomForestClassifier(n_estimators=300, random_state=SEED, n_jobs=1)),
        "gbm": ("Градиентный бустинг", GradientBoostingClassifier(random_state=SEED)),
        "knn": ("k ближайших соседей", make_pipeline(StandardScaler(), KNeighborsClassifier(n_neighbors=7))),
        "svm": ("SVM (RBF)", make_pipeline(StandardScaler(), SVC(probability=True, random_state=SEED))),
        "nb": ("Наивный Байес", GaussianNB()),
        "mlp": (
            "Нейросеть MLP (64)",
            make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(64,), max_iter=1000, random_state=SEED)),
        ),
    }


def regressors() -> dict[str, tuple[str, Any]]:
    return {
        "ridge": ("Ридж-регрессия", make_pipeline(StandardScaler(), Ridge())),
        "tree": ("Дерево решений", DecisionTreeRegressor(random_state=SEED, min_samples_leaf=5)),
        "rf": ("Случайный лес (300)", RandomForestRegressor(n_estimators=300, random_state=SEED, n_jobs=1)),
        "gbm": ("Градиентный бустинг", GradientBoostingRegressor(random_state=SEED)),
        "knn": ("k ближайших соседей", make_pipeline(StandardScaler(), KNeighborsRegressor(n_neighbors=7))),
        "svr": ("SVR (RBF)", make_pipeline(StandardScaler(), SVR(C=50))),
        "mlp": (
            "Нейросеть MLP (64)",
            make_pipeline(StandardScaler(), MLPRegressor(hidden_layer_sizes=(64,), max_iter=3000, random_state=SEED)),
        ),
    }


def datasets() -> dict[str, dict[str, Any]]:
    digits = load_digits()
    return {
        "breast_cancer": {
            "task": "binary",
            "data": load_breast_cancer(return_X_y=True),
            "origin": "UCI WDBC via sklearn",
        },
        "digits_binary": {
            "task": "binary",
            "data": (digits.data, (digits.target >= 5).astype(int)),
            "origin": "UCI optical digits via sklearn; target = digit >= 5 (производная задача)",
        },
        "wine": {"task": "multiclass", "data": load_wine(return_X_y=True), "origin": "UCI Wine via sklearn"},
        "diabetes": {
            "task": "regression",
            "data": load_diabetes(return_X_y=True),
            "origin": "Efron et al. 2004 via sklearn",
        },
    }


def _timed_predict(model: Any, x: np.ndarray, method: str) -> float:
    runs = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        getattr(model, method)(x)
        runs.append(time.perf_counter() - start)
    return statistics.median(runs) * 1000 / len(x) * 1000


def measure(task: str, x: np.ndarray, y: np.ndarray, label: str, model: Any) -> dict[str, Any]:
    from sklearn.base import clone

    splitter = (
        KFold(n_splits=FOLDS, shuffle=True, random_state=SEED)
        if task == "regression"
        else StratifiedKFold(n_splits=FOLDS, shuffle=True, random_state=SEED)
    )
    scores: dict[str, list[float]] = {}
    fits: list[float] = []
    infers: list[float] = []
    for train, test in splitter.split(x, y):
        fitted = clone(model)
        start = time.perf_counter()
        fitted.fit(x[train], y[train])
        fits.append(time.perf_counter() - start)
        xt, yt = x[test], y[test]
        if task == "regression":
            infers.append(_timed_predict(fitted, xt, "predict"))
            pred = fitted.predict(xt)
            fold = {
                "r2": r2_score(yt, pred),
                "rmse": root_mean_squared_error(yt, pred),
                "mae": mean_absolute_error(yt, pred),
            }
        else:
            infers.append(_timed_predict(fitted, xt, "predict_proba"))
            proba = fitted.predict_proba(xt)
            pred = fitted.classes_[proba.argmax(axis=1)]
            if task == "binary":
                fold = {
                    "roc_auc": roc_auc_score(yt, proba[:, 1]),
                    "f1": f1_score(yt, pred),
                    "accuracy": accuracy_score(yt, pred),
                }
            else:
                fold = {
                    "roc_auc": roc_auc_score(yt, proba, multi_class="ovr"),
                    "f1_macro": f1_score(yt, pred, average="macro"),
                    "accuracy": accuracy_score(yt, pred),
                }
        for key, value in fold.items():
            scores.setdefault(key, []).append(float(value))

    final = clone(model).fit(x, y)
    size_mb = len(pickle.dumps(final)) / 1_000_000

    def summary(values: list[float]) -> dict[str, float]:
        return {"value": round(statistics.fmean(values), 6), "std": round(statistics.stdev(values), 6)}

    values = {key: summary(items) for key, items in scores.items()}
    values["fit_seconds"] = summary(fits)
    values["inference_ms"] = summary(infers)
    values["model_mb"] = {"value": round(size_mb, 6), "std": None}
    return {"label": label, "values": values}


CRITERIA = {
    "roc_auc": ("ROC-AUC", "max", "", "quality"),
    "f1": ("F1", "max", "", "quality"),
    "f1_macro": ("F1 macro", "max", "", "quality"),
    "accuracy": ("Accuracy", "max", "", "quality"),
    "r2": ("R²", "max", "", "quality"),
    "rmse": ("RMSE", "min", "", "quality"),
    "mae": ("MAE", "min", "", "quality"),
    "fit_seconds": ("Время обучения на фолде", "min", "с", "time"),
    "inference_ms": ("Инференс на 1000 строк", "min", "мс", "time"),
    "model_mb": ("Размер модели (pickle)", "min", "МБ", "size"),
}


def splitter_name(task: str) -> str:
    return "KFold" if task == "regression" else "StratifiedKFold"


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
        "numpy": np.__version__,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "seed": SEED,
        "folds": FOLDS,
        "inference_repeats": REPEATS,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("docs/research/data"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    env = environment()

    for name, spec in datasets().items():
        x, y = spec["data"]
        task = spec["task"]
        models = regressors() if task == "regression" else classifiers()
        alternatives = []
        for key, (label, model) in models.items():
            started = time.perf_counter()
            row = measure(task, np.asarray(x, dtype=float), np.asarray(y), label, model)
            alternatives.append({"key": key, **row, "meta": {"family": type(model).__name__}})
            print(f"{name:14s} {label:28s} {time.perf_counter() - started:6.1f} c")
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
                "origin": spec["origin"],
                "rows": len(y),
                "features": int(np.asarray(x).shape[1]),
                "protocol": f"{FOLDS}-fold {splitter_name(task)}, shuffle, seed {SEED}",
                "environment": env,
            },
        }
        (args.out / f"{name}.json").write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
