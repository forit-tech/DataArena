"""Правило зависимостей между слоями проверяется тестом, а не договорённостью.

`api -> services -> domain`, `services -> adapters`.
`domain` не знает ни про HTTP, ни про файлы, ни про хранилище.

Без такого теста слои расползаются незаметно: один импорт «по-быстрому» не виден в ревью,
а через месяц доменную логику уже нельзя протестировать без поднятия FastAPI.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"

#слой -> набор слоёв backend, которые ему разрешено импортировать
ALLOWED_DEPENDENCIES: dict[str, set[str]] = {
    "domain": {"domain", "core"},
    "adapters": {"adapters", "domain", "core"},
    "services": {"services", "adapters", "domain", "core"},
    "api": {"api", "services", "domain", "core"},
    "core": {"core"},
}
#эти библиотеки доменный слой импортировать не должен: они привязывают его к транспорту,
#хранилищу или конкретному движку исполнения
FORBIDDEN_IN_DOMAIN = ("fastapi", "starlette", "uvicorn", "duckdb", "sqlite3")


def _module_layer(path: Path) -> str:
    return path.relative_to(BACKEND_ROOT).parts[0]


def _imported_modules(path: Path) -> set[str]:
    #эта функция собирает имена импортируемых модулей из AST, а не регулярками:
    #строка "import fastapi" в комментарии или docstring не должна считаться импортом
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)

    return modules


def _python_files() -> list[Path]:
    return [path for path in sorted(BACKEND_ROOT.rglob("*.py")) if "__pycache__" not in path.parts]


def test_layers_respect_the_dependency_direction() -> None:
    violations: list[str] = []

    for path in _python_files():
        layer = _module_layer(path)

        if layer not in ALLOWED_DEPENDENCIES:
            #файл лежит прямо в backend/, например main.py: у него нет собственного слоя
            continue

        for module in _imported_modules(path):
            if not module.startswith("backend."):
                continue

            imported_layer = module.split(".")[1]

            if imported_layer not in ALLOWED_DEPENDENCIES[layer]:
                relative = path.relative_to(BACKEND_ROOT)
                violations.append(f"{relative}: {layer} импортирует {imported_layer} ({module})")

    assert not violations, "Нарушено направление зависимостей:\n" + "\n".join(violations)


def test_domain_does_not_know_about_transport_or_storage() -> None:
    violations: list[str] = []

    for path in _python_files():
        if _module_layer(path) != "domain":
            continue

        for module in _imported_modules(path):
            root = module.split(".")[0]

            if root in FORBIDDEN_IN_DOMAIN:
                violations.append(f"{path.relative_to(BACKEND_ROOT)}: импортирует {module}")

    assert not violations, "Доменный слой привязан к транспорту или хранилищу:\n" + "\n".join(violations)


def test_every_backend_package_is_importable() -> None:
    #каталог без __init__.py собирается в неявный namespace-пакет и ведёт себя иначе при упаковке
    missing = [
        directory.relative_to(BACKEND_ROOT)
        for directory in sorted(BACKEND_ROOT.rglob("*"))
        if directory.is_dir() and "__pycache__" not in directory.parts
        and not (directory / "__init__.py").exists()
    ]

    assert not missing, f"Каталоги без __init__.py: {missing}"
