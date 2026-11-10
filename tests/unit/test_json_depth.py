"""JSON, вложенный глубже предела, отклоняется до того, как его прочитает движок.

Измерено: десять килобайт из пяти тысяч вложенных скобок роняли процесс сервера целиком —
переполнение стека в нативном читателе, без исключения и без ответа.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.adapters.formats.registry import MAX_JSON_DEPTH, ensure_shallow_json
from backend.core.errors import DatasetReadError


def write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "data.json"
    path.write_bytes(content)

    return path


def test_brackets_inside_strings_do_not_count(tmp_path: Path) -> None:
    #в строке — сплошные скобки и экранированная кавычка; настоящая глубина — два
    content = b'[{"a": "[[[[[[\\"{{{{", "b": "]]]]"}]'

    ensure_shallow_json(write(tmp_path, content), limit=2)


def test_the_limit_is_exact(tmp_path: Path) -> None:
    ensure_shallow_json(write(tmp_path, b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH))

    with pytest.raises(DatasetReadError):
        ensure_shallow_json(write(tmp_path, b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1)))


@pytest.mark.parametrize("depth", [5_000, 200_000])
def test_a_json_bomb_is_refused_on_upload_instead_of_killing_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    import io

    from fastapi.testclient import TestClient

    from backend.api.app import create_app
    from backend.core import config
    from backend.services import context

    monkeypatch.setenv("DATAARENA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    config.get_settings.cache_clear()
    context.reset_context()
    client = TestClient(create_app())
    workspace_id = client.post("/api/workspaces", json={"name": "Бомба"}).json()["workspace_id"]

    for name in ("bomb.json", "bomb.jsonl"):
        response = client.post(
            f"/api/workspaces/{workspace_id}/datasets",
            files={"file": (name, io.BytesIO(b"[" * depth + b"]" * depth))},
        )

        assert response.status_code == 400, response.text

    config.get_settings.cache_clear()
    context.reset_context()
