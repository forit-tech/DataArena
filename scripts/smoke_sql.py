"""Сквозная проверка окна SQL на настоящем сервере.

Идёт тем же путём, что и человек: загрузить датасет, спросить у него что-нибудь, посмотреть
историю, сохранить запрос, перезапустить backend и убедиться, что всё на месте, а исходный
файл не изменился ни разу.

Запуск: python scripts/smoke_sql.py
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def request(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    call = urllib.request.Request(url, data=body, method=method, headers=headers or {})

    try:
        with urllib.request.urlopen(call, timeout=120) as response:
            payload = response.read()
            return response.status, (json_loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read()
        return error.code, (json_loads(payload) if payload else None)


def json_loads(payload: bytes):
    import json

    return json.loads(payload.decode("utf-8"))


def post_json(url: str, data: dict):
    import json

    return request(
        "POST", url, json.dumps(data).encode("utf-8"), {"Content-Type": "application/json"}
    )


def upload(url: str, name: str, content: bytes):
    boundary = uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return request(
        "POST", url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )


class Server:
    def __init__(self, workspace_root: Path, port: int) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}/api"
        environment = {**os.environ, "DATAARENA_WORKSPACE_ROOT": str(workspace_root)}
        self.process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "uvicorn", "backend.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT,
            env=environment,
        )

    def wait(self) -> None:
        deadline = time.time() + 45

        while time.time() < deadline:
            try:
                status, _ = request("GET", f"{self.base}/system/health")

                if status == 200:
                    return
            except OSError:
                time.sleep(0.3)

        raise RuntimeError("backend не поднялся")

    def stop(self) -> None:
        self.process.send_signal(signal.SIGTERM)

        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


def check(label: str, condition: bool, detail: str = "") -> None:
    """Печатает исход проверки. Пояснение показывается только при сбое.

    Показывать его при успехе — значит выводить строки вида «A != B» под отметкой OK:
    читающий вывод решит, что проверка провалилась, хотя всё в порядке.
    """
    if condition:
        print(f"  [OK  ] {label}")
        return

    print(f"  [СБОЙ] {label}{f' — {detail}' if detail else ''}")
    raise SystemExit(1)


def prepare_datasets(base: str, workspace_id: str) -> None:
    orders = pl.DataFrame(
        {
            "id": list(range(1, 1001)),
            "город": ["Москва", "Казань", "Омск", "Тверь"] * 250,
            "сумма": [float(index * 10) for index in range(1, 1001)],
        }
    )
    clients = pl.DataFrame(
        {"id": list(range(1, 1001)), "имя": [f"клиент {n}" for n in range(1, 1001)]}
    )

    status, first = upload(
        f"{base}/workspaces/{workspace_id}/datasets",
        "продажи.csv",
        orders.write_csv().encode("utf-8"),
    )
    check("продажи.csv загружен", status == 201, str(first))

    buffer = io.BytesIO()
    clients.write_parquet(buffer)
    status, second = upload(
        f"{base}/workspaces/{workspace_id}/datasets", "клиенты.parquet", buffer.getvalue()
    )
    check("клиенты.parquet загружен", status == 201, str(second))

    status, bindings = request("GET", f"{base}/workspaces/{workspace_id}/sql/datasets")
    aliases = {item["alias"] for item in bindings["datasets"]}
    check("псевдонимы видны в окне запросов", aliases == {"продажи", "клиенты"}, str(aliases))


def source_fingerprints(workspace_root: Path, workspace_id: str) -> dict[str, str]:
    #отпечаток исходных файлов: они не должны измениться ни от одного запроса
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((workspace_root / workspace_id / "sources").rglob("*"))
        if path.is_file()
    }


def run_analytics(base: str, workspace_id: str) -> None:
    endpoint = f"{base}/workspaces/{workspace_id}/sql/queries"

    status, body = post_json(endpoint, {"sql": "SELECT count(*) AS n FROM продажи"})
    check("простой счётчик", status == 200 and body["result"]["rows"][0]["n"] == 1000, str(body))

    status, body = post_json(
        endpoint,
        {
            "sql": "SELECT город, count(*) AS n, round(avg(сумма), 2) AS средняя "
            "FROM продажи GROUP BY город ORDER BY n DESC, город"
        },
    )
    check("группировка", status == 200 and len(body["result"]["rows"]) == 4, str(body))

    status, body = post_json(
        endpoint,
        {
            "sql": "SELECT k.имя, p.сумма FROM продажи p JOIN клиенты k ON p.id = k.id "
            "WHERE p.сумма > 9000 ORDER BY p.сумма"
        },
    )
    check("join двух датасетов", status == 200 and body["result"]["row_count"] > 0, str(body)[:200])
    check(
        "оба датасета отмечены в истории",
        sorted(body["run"]["datasets"]) == ["клиенты", "продажи"],
        str(body["run"]["datasets"]),
    )

    status, body = post_json(endpoint, {"sql": "SELECT * FROM продажи", "row_limit": 10})
    check(
        "усечение названо прямо",
        status == 200 and body["result"]["truncated"] and body["result"]["truncated_by"] == "rows",
        str(body["result"])[:200],
    )


HOSTILE = [
    ("SELECT * FROM read_parquet('C:/Windows/win.ini')", {400, 403, 422}),
    ("SELECT * FROM duckdb_settings()", {403}),
    ("SELECT current_setting('temp_directory')", {403}),
    ("ATTACH 'other.db' AS other", {403}),
    ("COPY продажи TO 'C:/leak.csv'", {403, 422}),
    ("DROP TABLE продажи", {403}),
    ("SELECT * FROM продажи; DROP TABLE продажи", {403}),
    ("INSTALL httpfs", {403}),
    ("LOAD httpfs", {403}),
    ("CREATE SECRET s (TYPE S3, KEY_ID 'k', SECRET 'v')", {403}),
    ("EXPORT DATABASE 'C:/dump'", {403}),
    ("SET enable_external_access=true", {403}),
    ("SELECT * FROM glob('C:/*')", {403}),
    ("SELECT * FROM read_csv('https://attacker.example/x.csv')", {403}),
    #шаблон, который ронял процесс целиком: сервер обязан пережить его и ответить отказом
    ("SELECT count(*) FROM продажи WHERE город LIKE '" + "%_" * 400 + "%'", {403}),
    #нулевой байт: разборщик и движок понимают его по-разному
    ("SELECT 1 FROM продажи" + chr(0) + "; DROP TABLE продажи", {403}),  # noqa: S608
]


#движок печатает пути через прямую косую черту: проверка на «C:\\» их не находила
#и была зелёной всегда. Признаки перечислены так, как они выглядят в сообщениях
PATH_MARKERS = ("C:/", "C:\\", "AppData", "/etc/", "/home/", "Traceback", ".duckdb")


def _mentions_a_path(text: str) -> bool:
    return any(marker in text for marker in PATH_MARKERS)


def run_refusals(base: str, workspace_id: str) -> None:
    for sql_text, expected in HOSTILE:
        status, body = post_json(
            f"{base}/workspaces/{workspace_id}/sql/queries", {"sql": sql_text}
        )
        check(f"отказ: {sql_text[:45]}", status in expected, f"{status} {str(body)[:120]}")
        check(
            "  без путей в сообщении",
            not _mentions_a_path(str(body)),
            str(body)[:160],
        )


#строки разной длины: постранично такой файл читается, движком запросов — нет
RAGGED_CSV = b"a,b\n1,2\n3\n4,5,6\n"


def survived_hostile_corpus(base: str, workspace_id: str) -> None:
    """Сервер продолжает работать после всего враждебного корпуса.

    Половина смысла проверки в этом: шаблон LIKE из корпуса раньше не отвергался,
    а ронял процесс целиком, и следующий запрос было уже некому обслуживать.
    """
    status, body = post_json(
        f"{base}/workspaces/{workspace_id}/sql/queries",
        {"sql": "SELECT count(*) AS n FROM продажи"},
    )
    check(
        "после всех отказов сервер отвечает",
        status == 200 and body["result"]["rows"][0]["n"] == 1000,
        str(body)[:160],
    )

    #CSV с разным числом полей: постранично читается, движком запросов — нет
    status, body = upload(
        f"{base}/workspaces/{workspace_id}/datasets", "рваный.csv", RAGGED_CSV
    )
    check("рваный CSV загружается", status == 201, str(body)[:160])

    status, body = post_json(
        f"{base}/workspaces/{workspace_id}/sql/queries", {"sql": "SELECT * FROM рваный"}
    )
    check(
        "нечитаемый движком датасет — доменная ошибка, а не 500",
        status == 409 and body["error"]["code"] == "dataset_unreadable",
        f"{status} {str(body)[:160]}",
    )


def check_history_and_saving(base: str, workspace_id: str) -> None:
    status, history = request("GET", f"{base}/workspaces/{workspace_id}/sql/history")
    statuses = [run["status"] for run in history["runs"]]
    check(
        "история содержит и удачные, и неудачные",
        "succeeded" in statuses and "failed" in statuses,
        str(statuses[:5]),
    )
    check("результат в историю не пишется", all("rows" not in run for run in history["runs"]))

    status, saved = post_json(
        f"{base}/workspaces/{workspace_id}/sql/saved",
        {
            "name": "Выручка по городам",
            "sql": "SELECT город, sum(сумма) FROM продажи GROUP BY город",
        },
    )
    check("запрос сохранён", status == 201, str(saved))

    status, conflict = post_json(
        f"{base}/workspaces/{workspace_id}/sql/saved",
        {"name": "Выручка по городам", "sql": "SELECT 1 FROM продажи"},
    )
    check("занятое имя — конфликт, а не ошибка сервера", status == 409, str(conflict))


def check_after_restart(base: str, workspace_id: str) -> None:
    status, history = request("GET", f"{base}/workspaces/{workspace_id}/sql/history")
    check("история пережила перезапуск", status == 200 and len(history["runs"]) >= 10, str(status))

    status, saved_list = request("GET", f"{base}/workspaces/{workspace_id}/sql/saved")
    check(
        "сохранённый запрос пережил перезапуск",
        status == 200 and [item["name"] for item in saved_list["queries"]] == ["Выручка по городам"],
        str(saved_list),
    )

    status, body = post_json(
        f"{base}/workspaces/{workspace_id}/sql/queries",
        {"sql": saved_list["queries"][0]["sql"]},
    )
    check("сохранённый запрос выполняется после перезапуска", status == 200, str(body)[:200])


def main() -> int:
    workspace_root = Path(tempfile.mkdtemp(prefix="dataarena_smoke_"))
    port = free_port()
    server = Server(workspace_root, port)

    try:
        server.wait()
        print("1. Рабочее пространство и датасеты")
        _, workspace = post_json(f"{server.base}/workspaces", {"name": "Проверка SQL"})
        workspace_id = workspace["workspace_id"]
        prepare_datasets(server.base, workspace_id)

        before = source_fingerprints(workspace_root, workspace_id)
        check("исходные файлы найдены", len(before) == 2, str(list(before)))

        print("2. Запросы")
        run_analytics(server.base, workspace_id)

        print("3. Отказы")
        run_refusals(server.base, workspace_id)

        print("4. Сервер пережил враждебный корпус")
        survived_hostile_corpus(server.base, workspace_id)

        print("5. История и сохранённые запросы")
        check_history_and_saving(server.base, workspace_id)

        print("6. Перезапуск backend")
        server.stop()
        server = Server(workspace_root, port)
        server.wait()
        check_after_restart(server.base, workspace_id)

        print("7. Неизменность исходных файлов")
        after = source_fingerprints(workspace_root, workspace_id)
        #сверяются файлы, снятые в начале: новые датасеты за это время появиться могли,
        #а вот содержимое уже загруженных обязано остаться прежним до последнего байта
        changed = {
            name: (digest, after.get(name))
            for name, digest in before.items()
            if after.get(name) != digest
        }
        check("исходные файлы не изменились ни одним запросом", not changed, str(changed))

        print("\nСквозная проверка пройдена.")
        return 0
    finally:
        server.stop()
        shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
