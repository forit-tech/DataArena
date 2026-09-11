"""Идентификаторы workspace и датасетов.

Все пути на диске собираются **только** из этих идентификаторов, никогда из пользовательской
строки. Приём перенесён из AutoDataAnalysis, где он закрывал path traversal в хранилище
экспериментов: путь, собранный из проверенного по регулярному выражению значения,
не может выйти за пределы каталога, каким бы ни было имя файла у пользователя.
"""

from __future__ import annotations

import re
import secrets

from backend.core.errors import ValidationError

#строчные латинские буквы и цифры: алфавит выбран так, чтобы идентификатор одинаково вёл себя
#в путях файловой системы, в URL и в именах таблиц SQL
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LENGTH = 16

WORKSPACE_ID_PATTERN = re.compile(r"^ws_[0-9a-z]{16}$")
DATASET_ID_PATTERN = re.compile(r"^ds_[0-9a-z]{16}$")
QUERY_RUN_ID_PATTERN = re.compile(r"^qr_[0-9a-z]{16}$")
SAVED_QUERY_ID_PATTERN = re.compile(r"^sq_[0-9a-z]{16}$")


def _random_suffix() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_SUFFIX_LENGTH))


def new_workspace_id() -> str:
    return f"ws_{_random_suffix()}"


def new_dataset_id() -> str:
    return f"ds_{_random_suffix()}"


def new_query_run_id() -> str:
    return f"qr_{_random_suffix()}"


def new_saved_query_id() -> str:
    return f"sq_{_random_suffix()}"


def ensure_workspace_id(value: str) -> str:
    #эта функция проверяет идентификатор ДО того, как он попадёт в путь или в запрос
    if not WORKSPACE_ID_PATTERN.match(value):
        raise ValidationError(
            "Некорректный идентификатор workspace.",
            details={"workspace_id": value[:64]},
        )

    return value


def ensure_dataset_id(value: str) -> str:
    if not DATASET_ID_PATTERN.match(value):
        raise ValidationError(
            "Некорректный идентификатор датасета.",
            details={"dataset_id": value[:64]},
        )

    return value


def ensure_saved_query_id(value: str) -> str:
    if not SAVED_QUERY_ID_PATTERN.match(value):
        raise ValidationError(
            "Некорректный идентификатор сохранённого запроса.",
            details={"saved_query_id": value[:64]},
        )

    return value
