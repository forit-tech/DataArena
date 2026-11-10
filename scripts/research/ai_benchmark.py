"""Сравнение локальных языковых моделей на задачах ИИ-советника DataArena.

    .venv/Scripts/python scripts/research/ai_benchmark.py --models qwen3:8b other:tag ...

Каждая модель получает одни и те же задачи — те же запросы, что отправляет приложение:

* **находки качества** на «грязном» датасете с известными дефектами (пробелы, разное
  написание категорий, пустая колонка, пропуски, дубликаты): правильный ответ — безопасный
  вариант, если он есть; если безопасного нет — любой вариант или «none»;
* **пары таблиц** с известной связью (клиенты–заказы 1:N; модели–продажи N:M; таблицы
  без связи): правильный ответ выводится из вердикта приложения.

Измеряется: совпадение с правильным ответом, доля ответов со ссылками на несуществующие
колонки или варианты (отброшенными проверкой), русский язык ответа, задержка, скорость
генерации и размер модели. Результат — results/ai_benchmark.json и матрица для Decision Lab
(ai/models_matrix.json): выбрать модель можно тем же методом, что и ML-модель.

Числа — измерения на этой машине; на другой машине скорость будет другой.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import polars as pl  # noqa: E402

OUT = ROOT / "docs" / "research"

DIRTY = pl.DataFrame(
    {
        "id": list(range(1, 13)),
        "город": [
            "Москва",
            "москва",
            " Москва",
            "Казань",
            "Казань ",
            "казань",
            "Москва",
            "Омск",
            "Москва",
            "Казань",
            "Омск",
            "Москва",
        ],
        "менеджер": [
            "Иванова",
            "Петров ",
            " Сидорова",
            "Иванова",
            "Петров",
            "Сидорова",
            "Иванова",
            "Петров",
            "Сидорова",
            "Иванова",
            "Петров",
            "Сидорова",
        ],
        "сумма": [100.0, None, 300.0, None, 500.0, 600.0, 700.0, None, 900.0, 1000.0, 1100.0, 1200.0],
        "комментарий": [None] * 12,
    },
    schema_overrides={"комментарий": pl.Utf8},
)
CLIENTS = pl.DataFrame({"client_id": list(range(1, 301)), "сегмент": ["A", "B", "C"] * 100})
ORDERS = pl.DataFrame(
    {
        "order_id": list(range(1, 901)),
        "client_id": [i % 300 + 1 for i in range(900)],
        "price": [float(100 + i % 50) for i in range(900)],
        "status": ["paid", "paid", "refund"] * 300,
    }
)
MODELS = pl.DataFrame(
    {
        "model": ["Golf", "Polo", "Camry"] * 10,
        "trim": [f"t{i}" for i in range(30)],
        "hp": [float(90 + i) for i in range(30)],
    }
)
SALES = pl.DataFrame({"model": ["Golf", "Polo", "Camry"] * 40, "amount": [float(i) for i in range(120)]})
WEATHER = pl.DataFrame({"city_code": [f"X{i}" for i in range(50)], "temp": [float(i) for i in range(50)]})

#что считается правильным советом по сборке при данном вердикте правил
ACCEPTABLE_ASSEMBLY = {
    "join": {"join"},
    "aggregate": {"aggregate", "keep_separate"},
    "none": {"keep_separate"},
    "review": {"review", "keep_separate"},
}


def cyrillic_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in letters) / len(letters)


def ollama_sizes(base_url: str) -> dict[str, float]:
    with urllib.request.urlopen(f"{base_url}/api/tags", timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {item["name"]: round(item.get("size", 0) / 1e9, 2) for item in payload.get("models", [])}


def unload(base_url: str, model: str) -> None:
    #модель выгружается из памяти перед следующей: две модели по 15 ГБ в 32 ГБ не помещаются
    data = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/generate", data=data, headers={"Content-Type": "application/json"}
    )
    with contextlib.suppress(OSError):
        urllib.request.urlopen(request, timeout=60).read()


def build_tasks(client: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def upload(workspace_id: str, frame: pl.DataFrame, name: str) -> str:
        buffer = io.BytesIO()
        frame.write_parquet(buffer)
        buffer.seek(0)
        return client.post(f"/api/workspaces/{workspace_id}/datasets", files={"file": (name, buffer)}).json()[
            "dataset_id"
        ]

    workspace = client.post("/api/workspaces", json={"name": "Сравнение моделей"}).json()["workspace_id"]
    dirty = upload(workspace, DIRTY, "dirty.parquet")
    report = client.get(f"/api/workspaces/{workspace}/datasets/{dirty}/health").json()
    repair_tasks = []
    for finding in report["findings"]:
        advice = client.get(
            f"/api/workspaces/{workspace}/datasets/{dirty}/health/findings/{finding['finding_id']}/repair"
        ).json()
        if not advice["options"]:
            continue
        safe = {o["option_id"] for o in advice["options"] if o["safety"] == "safe"}
        acceptable = safe or {o["option_id"] for o in advice["options"]} | {"none"}
        repair_tasks.append(
            {
                "workspace": workspace,
                "dataset": dirty,
                "finding": finding,
                "advice": advice,
                "acceptable": sorted(acceptable),
            }
        )

    pairs_workspace = client.post("/api/workspaces", json={"name": "Пары"}).json()["workspace_id"]
    for frame, name in (
        (CLIENTS, "clients"),
        (ORDERS, "orders"),
        (MODELS, "models"),
        (SALES, "sales"),
        (WEATHER, "weather"),
    ):
        upload(pairs_workspace, frame, f"{name}.parquet")
    discovery = client.get(f"/api/workspaces/{pairs_workspace}/joins/discover").json()
    datasets = {d["dataset_id"]: d for d in discovery["datasets"]}
    wanted = {("clients", "orders"), ("models", "sales"), ("clients", "weather"), ("orders", "weather")}
    assembly_tasks = []
    for pair in discovery["pairs"]:
        names = (pair["left_name"].split(".")[0], pair["right_name"].split(".")[0])
        if names in wanted or names[::-1] in wanted:
            assembly_tasks.append(
                {
                    "pair": pair,
                    "left": datasets[pair["left_dataset_id"]],
                    "right": datasets[pair["right_dataset_id"]],
                    "acceptable": sorted(ACCEPTABLE_ASSEMBLY[pair["verdict"]]),
                }
            )
    return repair_tasks, assembly_tasks


def run_model(
    model: str, settings: Any, repair_tasks: list[dict[str, Any]], assembly_tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    from backend.adapters.ai.llm import AiUnavailableError, complete_json
    from backend.services import ai_advisor

    calls: list[dict[str, Any]] = []
    for task in repair_tasks:
        option_ids = [o["option_id"] for o in task["advice"]["options"]]
        record: dict[str, Any] = {"kind": "repair", "code": task["finding"]["code"], "acceptable": task["acceptable"]}
        try:
            completion = complete_json(
                settings,
                system=ai_advisor.SYSTEM,
                user=ai_advisor.finding_prompt(task["finding"], task["advice"]),
                schema=ai_advisor.finding_schema(option_ids),
                model=model,
            )
            answer = ai_advisor.validate_finding_answer(completion.content, option_ids)
            record.update(
                answer=answer["recommended_option"],
                correct=answer["recommended_option"] in task["acceptable"],
                rejected=bool(answer["rejected"]),
                russian=cyrillic_share(answer["summary"] + " " + answer["reasoning"]),
                seconds=completion.seconds,
                tokens_per_second=completion.tokens_per_second,
                summary=answer["summary"],
            )
        except AiUnavailableError as error:
            record.update(error=error.message, correct=False, rejected=False, russian=0.0)
        calls.append(record)
        print(
            f"  {model} repair {record['code']}: {record.get('answer')} "
            f"ok={record['correct']} {record.get('seconds')} c"
        )

    for task in assembly_tasks:
        record = {
            "kind": "assembly",
            "pair": f"{task['pair']['left_name']}–{task['pair']['right_name']}",
            "acceptable": task["acceptable"],
        }
        try:
            completion = complete_json(
                settings,
                system=ai_advisor.SYSTEM,
                user=ai_advisor.assembly_prompt(task["pair"], task["left"], task["right"]),
                schema=ai_advisor.assembly_schema(),
                model=model,
            )
            answer = ai_advisor.validate_assembly_answer(completion.content, task["left"], task["right"])
            record.update(
                answer=answer["recommendation"],
                correct=answer["recommendation"] in task["acceptable"],
                rejected=bool(answer["rejected"]),
                russian=cyrillic_share(answer["summary"]),
                seconds=completion.seconds,
                tokens_per_second=completion.tokens_per_second,
                summary=answer["summary"],
            )
        except AiUnavailableError as error:
            record.update(error=error.message, correct=False, rejected=False, russian=0.0)
        calls.append(record)
        print(
            f"  {model} assembly {record['pair']}: {record.get('answer')} "
            f"ok={record['correct']} {record.get('seconds')} c"
        )

    ok = [c for c in calls if "error" not in c]
    return {
        "model": model,
        "calls": calls,
        "errors": len(calls) - len(ok),
        "accuracy": round(sum(c["correct"] for c in calls) / len(calls), 4) if calls else None,
        "repair_accuracy": round(statistics.fmean(c["correct"] for c in calls if c["kind"] == "repair"), 4),
        "assembly_accuracy": round(statistics.fmean(c["correct"] for c in calls if c["kind"] == "assembly"), 4),
        "rejected_share": round(sum(c["rejected"] for c in calls) / len(calls), 4) if calls else None,
        "russian_share": round(statistics.fmean(c["russian"] for c in ok), 4) if ok else 0.0,
        "median_seconds": round(statistics.median(c["seconds"] for c in ok), 2) if ok else None,
        "median_tokens_per_second": round(
            statistics.median(c["tokens_per_second"] for c in ok if c.get("tokens_per_second")), 2
        )
        if any(c.get("tokens_per_second") for c in ok)
        else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    os.environ["DATAARENA_WORKSPACE_ROOT"] = tempfile.mkdtemp(prefix="dataarena_ai_bench_")
    from fastapi.testclient import TestClient

    from backend.adapters.ai.llm import AiSettings
    from backend.api.app import create_app
    from backend.core import config
    from backend.services import context

    config.get_settings.cache_clear()
    context.reset_context()
    client = TestClient(create_app())
    repair_tasks, assembly_tasks = build_tasks(client)
    print(f"Задач: находок {len(repair_tasks)}, пар {len(assembly_tasks)}")

    settings = AiSettings(provider="ollama", base_url=args.url, model="", enabled=True)
    sizes = ollama_sizes(args.url)
    results = []
    started = time.perf_counter()
    for model in args.models:
        print(f"== {model}")
        result = run_model(model, settings, repair_tasks, assembly_tasks)
        result["size_gb"] = sizes.get(model)
        results.append(result)
        unload(args.url, model)
        #промежуточная запись: прогон долгий, и упавшая на середине модель не должна стирать предыдущие
        (OUT / "results").mkdir(parents=True, exist_ok=True)
        (OUT / "results" / "ai_benchmark.json").write_text(
            json.dumps(
                {
                    "environment": {
                        "platform": platform.platform(),
                        "processor": platform.processor(),
                        "python": platform.python_version(),
                    },
                    "tasks": {"repair": len(repair_tasks), "assembly": len(assembly_tasks)},
                    "results": results,
                    "elapsed_seconds": round(time.perf_counter() - started, 1),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    matrix = {
        "criteria": [
            {
                "key": "accuracy",
                "label": "Доля правильных советов",
                "direction": "max",
                "group": "quality",
                "weight": 4,
            },
            {
                "key": "rejected_share",
                "label": "Доля ответов с выдуманными ссылками",
                "direction": "min",
                "group": "quality",
                "weight": 2,
            },
            {
                "key": "median_seconds",
                "label": "Время ответа (медиана)",
                "direction": "min",
                "unit": "с",
                "group": "time",
                "weight": 2,
            },
            {
                "key": "size_gb",
                "label": "Размер модели",
                "direction": "min",
                "unit": "ГБ",
                "group": "size",
                "weight": 1,
            },
        ],
        "alternatives": [
            {
                "key": r["model"],
                "label": r["model"].split("/")[-1],
                "values": {
                    "accuracy": {"value": r["accuracy"]},
                    "rejected_share": {"value": r["rejected_share"]},
                    "median_seconds": {"value": r["median_seconds"]},
                    "size_gb": {"value": r["size_gb"]},
                },
            }
            for r in results
        ],
        "primary": "accuracy",
        "context": {"source": "scripts/research/ai_benchmark.py", "machine": platform.processor()},
    }
    (OUT / "ai").mkdir(parents=True, exist_ok=True)
    (OUT / "ai" / "models_matrix.json").write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        print(
            f"{r['model']}: точность {r['accuracy']}, выдумки {r['rejected_share']}, русский {r['russian_share']}, "
            f"{r['median_seconds']} с, {r['median_tokens_per_second']} ток/с, {r['size_gb']} ГБ, ошибок {r['errors']}"
        )


if __name__ == "__main__":
    main()
