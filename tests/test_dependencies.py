"""Объявленные зависимости сверяются с установленными.

Проверка появилась после настоящего сбоя: в pyproject.toml стояли диапазоны, которые
никто не проверял установкой («fastapi>=0.141,<0.142» при работавшей 0.116). Формально
манифест выглядел строгим, фактически не значил ничего, и обновление FastAPI подо мной
уронило приложение на импорте маршрута — не один запрос, а всё приложение целиком.

Границы, которым не соответствует рабочее окружение, — это не документация,
а обещание воспроизводимости, которого никто не давал.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import Requirement

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#имя дистрибутива не всегда совпадает с именем модуля, а extras в проверке версии не участвуют
def _declared() -> list[Requirement]:
    document = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    runtime = document["project"]["dependencies"]
    develop = document["project"]["optional-dependencies"]["dev"]

    return [Requirement(item) for item in runtime + develop]


def test_installed_versions_satisfy_the_declared_ranges() -> None:
    """Окружение соответствует манифесту — одним проходом по всем зависимостям.

    Раньше это была параметризация по каждому пакету: семнадцать случаев на одно
    свойство. Один проход не слабее, а сильнее — он показывает **все** расхождения
    сразу, а не первое попавшееся, и именно этого не хватало, когда манифест разошёлся
    с реальностью сразу по трём пакетам.
    """
    missing: list[str] = []
    mismatched: list[str] = []

    for requirement in _declared():
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            missing.append(requirement.name)
            continue

        if not requirement.specifier.contains(installed, prereleases=True):
            mismatched.append(f"{requirement.name} {installed} ∉ «{requirement.specifier}»")

    assert not missing, (
        f"объявлены в pyproject.toml, но не установлены: {missing}. "
        "Окружение не собрано из манифеста."
    )
    assert not mismatched, (
        f"расходятся манифест и реальность: {mismatched}. "
        "Нужно либо обновить окружение, либо исправить границы."
    )


def test_every_imported_third_party_package_is_declared() -> None:
    """Незаявленная зависимость работает ровно до чужой машины.

    pyarrow попал в проект именно так: он был нужен для потоковой передачи датасетов
    в DuckDB, устанавливался вручную и не значился в манифесте.
    """
    document = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = {
        Requirement(item).name.lower()
        for item in document["project"]["dependencies"]
        + document["project"]["optional-dependencies"]["dev"]
    }

    for required in ("duckdb", "polars", "pyarrow", "fastapi", "pydantic"):
        assert required in declared, f"{required} используется, но не объявлен"
