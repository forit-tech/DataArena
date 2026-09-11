"""Псевдоним датасета — имя, которым к нему обращаются в SQL.

Пользователь не передаёт в запрос путь к файлу. Он пишет `FROM продажи`, а backend сам
связывает это имя с разрешённым артефактом. Поэтому псевдоним обязан быть предсказуемым
(его набирают руками), уникальным внутри рабочего пространства (иначе `FROM продажи`
неоднозначно) и пригодным для SQL без кавычек — иначе им неудобно пользоваться.

Кириллица сохраняется: DuckDB принимает буквы Unicode в идентификаторах, а продукт
русскоязычный, и превращать «продажи» в «prodazhi» значит заставлять человека угадывать
транслитерацию.
"""

from __future__ import annotations

import re
import unicodedata

MAX_ALIAS_LENGTH = 48
#запасное имя, когда от исходного не осталось ничего пригодного: «12345.csv», «___.csv»
FALLBACK_ALIAS = "датасет"

#всё, что не буква, не цифра и не подчёркивание, становится подчёркиванием: пробелы,
#дефисы, точки и скобки в идентификаторе потребовали бы кавычек при каждом обращении
_UNSAFE = re.compile(r"[^\w]+", flags=re.UNICODE)
_REPEATED_UNDERSCORE = re.compile(r"_{2,}")


def alias_from_name(name: str) -> str:
    """Приводит имя датасета к основе псевдонима. Уникальность обеспечивается отдельно."""
    #NFC: «й» бывает одним символом и парой «и» + знак, и без нормализации два одинаковых
    #на вид имени дали бы разные псевдонимы
    normalized = unicodedata.normalize("NFC", name).strip()
    #расширение в имени таблицы не нужно: «продажи.csv» читается как «продажи»
    stem = normalized.rsplit(".", 1)[0] if "." in normalized else normalized

    candidate = _REPEATED_UNDERSCORE.sub("_", _UNSAFE.sub("_", stem)).strip("_").lower()
    candidate = candidate[:MAX_ALIAS_LENGTH].strip("_")

    if not candidate:
        return FALLBACK_ALIAS

    #идентификатор, начинающийся с цифры, требует кавычек: «2024_продажи» без них не разберётся
    if candidate[0].isdigit():
        candidate = f"_{candidate}"[:MAX_ALIAS_LENGTH]

    return candidate


def next_free_alias(base: str, taken: set[str]) -> str:
    """Подбирает свободный псевдоним, добавляя номер.

    Занятость проверяется вызывающим внутри транзакции: два одновременных импорта одного
    имени иначе получили бы один и тот же псевдоним, и второй упал бы на уникальном индексе.
    """
    if base not in taken:
        return base

    #ограничение на длину соблюдается и с суффиксом, иначе «имя_2» превысило бы предел
    for number in range(2, 1000):
        suffix = f"_{number}"
        candidate = f"{base[: MAX_ALIAS_LENGTH - len(suffix)]}{suffix}"

        if candidate not in taken:
            return candidate

    raise ValueError(f"не удалось подобрать свободный псевдоним для «{base}»")
