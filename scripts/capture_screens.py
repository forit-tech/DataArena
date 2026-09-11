"""Снимки интерфейса для отчёта об этапе.

Гоняет настоящий Chrome по настоящему приложению: загружает датасет через API, открывает
диагностику, проваливается в строки находки и сохраняет снимки. Ничего не рисует и ничего
не подставляет — на снимках то же, что видит человек.

Запуск (backend и frontend должны быть уже подняты):
    python scripts/capture_screens.py [каталог]
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import polars as pl
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_sql import post_json, upload

APP_URL = "http://127.0.0.1:5174"
API = "http://127.0.0.1:8510/api"
CHROME = Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe")
WIDTH, HEIGHT = 1560, 1000
ROWS = 4000


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def demo_dataset() -> bytes:
    """Датасет с настоящими отклонениями: так выглядит выгрузка из учётной системы."""
    frame = pl.DataFrame(
        {
            "номер_заказа": [f"ORD-{index:06d}" for index in range(ROWS - 2)]
            + ["ORD-000007", "ORD-000007"],
            "город": [
                ["Москва", "москва", "Казань ", None, "Омск", "Санкт-Петербург"][index % 6]
                for index in range(ROWS)
            ],
            "сумма": [
                None
                if index % 25 == 0
                else (4_500_000.0 if index in (11, 907) else float(300 + (index * 37) % 5000))
                for index in range(ROWS)
            ],
            "количество": [str(1 + index % 9) if index % 200 else "н/д" for index in range(ROWS)],
            "валюта": ["RUB"] * ROWS,
            "комментарий": [None] * ROWS,
            "маржа": [
                float("nan") if index % 300 == 0 else round(((index * 13) % 400) / 10, 2)
                for index in range(ROWS)
            ],
            "менеджер": [
                ["Иванова", "иванова", "Петров", "Сидорова "][index % 4] for index in range(ROWS)
            ],
            "статус": ["выполнен"] * (ROWS - 6) + ["отменён"] * 6,
        }
    )
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    return buffer.getvalue()


class Chrome:
    """Настоящий Chrome без окна, управляемый по протоколу отладки."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.profile = Path(tempfile.mkdtemp(prefix="dataarena_chrome_"))
        self.process = subprocess.Popen(  # noqa: S603
            [
                str(CHROME),
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile}",
                f"--window-size={WIDTH},{HEIGHT}",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--no-first-run",
                "--no-default-browser-check",
                APP_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def websocket_url(self) -> str:
        deadline = time.time() + 30

        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=2
                ) as response:
                    for target in json.loads(response.read()):
                        if target.get("type") == "page":
                            return str(target["webSocketDebuggerUrl"])
            except OSError:
                time.sleep(0.3)

        raise RuntimeError("Chrome не поднялся")

    def stop(self) -> None:
        self.process.terminate()

        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

        shutil.rmtree(self.profile, ignore_errors=True)


class Session:
    def __init__(self, connection: websockets.ClientConnection) -> None:
        self.connection = connection
        self.counter = 0

    async def send(self, method: str, **params: object) -> dict:
        self.counter += 1
        await self.connection.send(
            json.dumps({"id": self.counter, "method": method, "params": params})
        )

        while True:
            message = json.loads(await self.connection.recv())

            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")

                return message.get("result", {})

    async def evaluate(self, expression: str) -> object:
        result = await self.send(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        return result.get("result", {}).get("value")

    async def click_text(self, text: str) -> None:
        """Нажимает элемент по видимому тексту — так же, как это делает человек."""
        clicked = await self.evaluate(
            "(() => {"
            f"  const needle = {json.dumps(text)};"
            "  const nodes = [...document.querySelectorAll('button, [role=button], .dataset-card')];"
            "  const found = nodes.find((node) => (node.textContent || '').includes(needle));"
            "  if (!found) return false;"
            "  found.click();"
            "  return true;"
            "})()"
        )

        if not clicked:
            raise RuntimeError(f"не найден элемент с текстом «{text}»")

    async def settle(self, seconds: float = 1.5) -> None:
        await asyncio.sleep(seconds)

    async def capture(self, path: Path) -> None:
        result = await self.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        path.write_bytes(base64.b64decode(result["data"]))
        print(f"  сохранён {path.name}")


async def capture_all(session: Session, directory: Path) -> None:
    await session.send("Page.enable")
    await session.evaluate(f"location.href = {json.dumps(APP_URL)}")
    await session.settle(3)

    await session.click_text("заказы_2026.parquet")
    await session.settle()
    await session.capture(directory / "01-таблица.png")

    await session.click_text("Health")
    await session.settle(3)
    await session.capture(directory / "02-диагностика.png")

    #прокрутка до сигналов: они внизу списка, и их тон должен быть виден
    await session.evaluate("document.querySelector('.app-content').scrollTop = 1400")
    await session.settle(1)
    await session.capture(directory / "03-диагностика-сигналы.png")

    await session.evaluate("document.querySelector('.app-content').scrollTop = 0")
    await session.settle(1)
    await session.click_text("Показать строки")
    await session.settle(2.5)
    await session.capture(directory / "04-строки-находки.png")

    await session.click_text("Вернуться к диагностике")
    await session.settle(2)
    await session.capture(directory / "05-возврат-в-диагностику.png")


async def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("screens")
    directory.mkdir(parents=True, exist_ok=True)

    print("1. Готовим рабочее пространство")
    _, workspace = post_json(f"{API}/workspaces", {"name": "Снимки"})
    workspace_id = workspace["workspace_id"]
    status, dataset = upload(
        f"{API}/workspaces/{workspace_id}/datasets", "заказы_2026.parquet", demo_dataset()
    )
    print(f"  датасет загружен: {status} {dataset.get('dataset_id')}")

    chrome = Chrome(free_port())

    try:
        url = chrome.websocket_url()

        async with websockets.connect(url, max_size=64 * 1024 * 1024) as connection:
            session = Session(connection)
            #рабочее пространство подставляется до загрузки приложения: иначе оно создаст своё
            await session.send(
                "Page.addScriptToEvaluateOnNewDocument",
                source=f"localStorage.setItem('DataArenaWorkspaceId', {json.dumps(workspace_id)});",
            )
            print("2. Снимаем экраны")
            await capture_all(session, directory)

        print(f"\nСнимки в каталоге {directory.resolve()}")
        return 0
    finally:
        chrome.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
