"""Снимки всех разделов для документации (docs/images).

Как и capture_screens.py, гоняет настоящий Chrome по настоящему приложению: датасет
загружается через API, состояние Decision Lab — реальная матрица breast_cancer
из docs/research/data. Ничего не рисуется и не подставляется.

Запуск (backend и frontend уже подняты):
    .venv/Scripts/python scripts/capture_docs.py [каталог]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capture_screens import API, APP_URL, Chrome, Session, demo_dataset, free_port
from smoke_sql import post_json, request, upload

from backend.services.decision import import_file

ROOT = Path(__file__).resolve().parents[1]
DECISION_KEY = "dataarena.decision-lab.v1"


def decision_state() -> dict:
    matrix = import_file("breast_cancer.json", (ROOT / "docs/research/data/breast_cancer.json").read_bytes())
    weights = {"roc_auc": 4, "inference_ms": 2, "model_mb": 2}
    return {
        "source": "matrix",
        "notes": ["Реальные измерения: breast_cancer, 5-fold CV, scikit-learn 1.7.2."],
        "context": matrix["context"],
        "criteria": [
            {**item, "weight": weights.get(item["key"], 0), "enabled": item["key"] in weights}
            for item in matrix["criteria"]
        ],
        "alternatives": [{**item, "enabled": True} for item in matrix["alternatives"]],
        "constraints": [
            {"criterion": "inference_ms", "op": "<=", "bound": 10},
            {"criterion": "model_mb", "op": "<=", "bound": 1},
        ],
        "primary": "roc_auc",
        "normalization": "vector",
        "sensitivity": {
            "samples": 2000,
            "seed": 20261109,
            "concentration": 50,
            "grid_step": 0.01,
            "stability_threshold": 0.6,
        },
        "scales": [],
    }


async def scroll(session: Session, top: int) -> None:
    await session.evaluate(f"document.querySelector('.app-content').scrollTop = {top}")
    await session.settle(0.8)


async def scroll_to(session: Session, selector: str) -> None:
    await session.evaluate(
        "(() => { const c = document.querySelector('.app-content');"
        f" const el = document.querySelector({json.dumps(selector)});"
        " if (el) c.scrollTop = el.getBoundingClientRect().top + c.scrollTop - c.getBoundingClientRect().top - 16; })()"
    )
    await session.settle(0.8)


SET_SELECT = (
    "(i, v) => { const el = document.querySelectorAll('select')[i];"
    " Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(el, v);"
    " el.dispatchEvent(new Event('change', { bubbles: true })); }"
)


async def capture_all(session: Session, directory: Path, joined_name: str) -> None:
    await session.send("Page.enable")
    await session.evaluate(f"location.href = {json.dumps(APP_URL)}")
    await session.settle(3)
    await session.capture(directory / "01-workspace.png")

    await session.click_text("заказы_2026.parquet")
    await session.settle(2)
    await session.capture(directory / "02-dataset.png")

    await session.click_text("SQL-запрос")
    await session.settle(1.5)
    #запрос к собранному датасету: снимок показывает результат, а не пустой редактор
    query = (
        "SELECT отдел, count(*) AS заказов, round(sum(сумма)) AS выручка\n"
        "FROM заказы_с_отделами\nGROUP BY отдел\nORDER BY выручка DESC"
    )
    await session.evaluate(
        "(() => { const el = document.querySelector('.sql-drawer textarea');"
        " Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(el, "
        f"{json.dumps(query, ensure_ascii=False)});"
        " el.dispatchEvent(new Event('input', { bubbles: true })); })()"
    )
    await session.settle(0.5)
    await session.click_text("Выполнить")
    await session.settle(4)
    await scroll_to(session, ".sql-drawer")
    await session.capture(directory / "03-sql.png")
    await scroll(session, 0)

    await session.click_text("Конвейер")
    await session.settle(2)
    await session.capture(directory / "04-pipeline.png")

    await session.click_text("Качество")
    await session.settle(3)
    await session.capture(directory / "05-health.png")

    await session.click_text("Сборка")
    await session.settle(2)
    #ключ «менеджер = менеджер»: селекты страницы — левый, правый датасет, затем пара ключа
    await session.evaluate(f"({SET_SELECT})(2, 'менеджер')")
    await session.evaluate(f"({SET_SELECT})(3, 'менеджер')")
    await session.settle(1)
    await session.click_text("Проверить")
    await session.settle(2)
    await session.capture(directory / "06-builder.png")

    #происхождение интереснее у собранного датасета: у него два родителя
    await session.click_text("Файлы")
    await session.settle(1.5)
    await session.click_text(joined_name)
    await session.settle(1.5)
    await session.click_text("Происхождение")
    await session.settle(2)
    await session.capture(directory / "07-lineage.png")

    await session.click_text("Выбор модели")
    await session.settle(4)
    await session.capture(directory / "08-decision-c.png")

    await session.click_text("Режим D")
    await session.settle(2)
    await session.capture(directory / "09-decision-d.png")

    await session.click_text("Компромисс")
    await session.settle(1)
    await scroll_to(session, ".modes-table")
    await session.capture(directory / "10-decision-tradeoff.png")

    await session.click_text("Устойчивость")
    await session.settle(1)
    await session.capture(directory / "10b-decision-stability.png")

    await session.click_text("Почему так")
    await session.settle(1)
    await session.capture(directory / "11-decision-influence.png")

    await session.evaluate("document.querySelectorAll('details.decision-panel').forEach((d) => d.open = true)")
    await scroll(session, 0)
    await session.evaluate("document.querySelector('.decision__controls').scrollIntoView()")
    await session.evaluate(
        "(() => { const el = [...document.querySelectorAll('summary')].find(s => s.textContent.includes('Шкалы'));"
        " const c = document.querySelector('.app-content');"
        " c.scrollTop = el.getBoundingClientRect().top + c.scrollTop - c.getBoundingClientRect().top - 16; })()"
    )
    await session.settle(1)
    await session.capture(directory / "12-decision-scales.png")

    #светлая тема: переключатель в островке навигации
    await scroll(session, 0)
    await session.evaluate("document.querySelector('.island__theme').click()")
    await session.settle(1)
    await session.capture(directory / "13-light-decision.png")
    await session.click_text("Данные")
    await session.settle(2)
    await session.capture(directory / "14-light-dataset.png")
    await session.evaluate("document.querySelector('.island__theme').click()")


async def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images"
    directory.mkdir(parents=True, exist_ok=True)

    _, workspace = post_json(f"{API}/workspaces", {"name": "Документация"})
    workspace_id = workspace["workspace_id"]
    _, orders = upload(f"{API}/workspaces/{workspace_id}/datasets", "заказы_2026.parquet", demo_dataset())
    #второй источник нужен разделу Builder: без него соединять нечего
    rows = ["менеджер,отдел,город_офиса", "Иванова,Розница,Москва", "Петров,Опт,Казань", "Сидорова,Розница,Омск"]
    managers = "".join(f"{row}{chr(10)}" for row in rows)
    _, managers_ds = upload(f"{API}/workspaces/{workspace_id}/datasets", "менеджеры.csv", managers.encode("utf-8"))

    #шаги конвейера: снимок раздела показывает настоящую очистку, а не пустой список
    steps = [
        {"step_id": "trim", "kind": "trim_whitespace", "params": {"columns": ["менеджер", "город"]}},
        {"step_id": "case", "kind": "change_case", "params": {"columns": ["менеджер", "город"], "case": "title"}},
        {"step_id": "drop", "kind": "drop_columns", "params": {"columns": ["комментарий"]}},
    ]
    orders_id = orders["dataset_id"]
    status, _ = request(
        "PUT",
        f"{API}/workspaces/{workspace_id}/datasets/{orders_id}/pipeline",
        json.dumps({"steps": steps}, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    assert status == 200, f"конвейер не сохранён: {status}"
    joined_name = "заказы с отделами"
    status, joined = post_json(
        f"{API}/workspaces/{workspace_id}/joins",
        {
            "left_dataset_id": orders_id,
            "right_dataset_id": managers_ds["dataset_id"],
            "keys": [{"left": "менеджер", "right": "менеджер"}],
            "kind": "left",
            "right_suffix": "_справочник",
            "name": joined_name,
        },
    )
    assert status in (200, 201), f"сборка не выполнена: {status} {joined}"
    state = json.dumps(decision_state(), ensure_ascii=False)

    chrome = Chrome(free_port())
    try:
        async with websockets.connect(chrome.websocket_url(), max_size=64 * 1024 * 1024) as connection:
            session = Session(connection)
            await session.send(
                "Page.addScriptToEvaluateOnNewDocument",
                source=(
                    f"localStorage.setItem('DataArenaWorkspaceId', {json.dumps(workspace_id)});"
                    f"localStorage.setItem({json.dumps(DECISION_KEY)}, {json.dumps(state)});"
                ),
            )
            await capture_all(session, directory, joined_name)
        print(f"Снимки в каталоге {directory}")
        return 0
    finally:
        chrome.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
