"""Согласование числительных в текстах находок.

«1 значений записаны по-разному» и «667 строк не имеют значения» — по-русски это
одинаково неверно, только первое заметно сразу, а второе становится неверным на числах
вроде 21 или 1002. Тексты находок видит человек и по ним принимает решение; сломанное
согласование в них выглядит так же, как сломанный расчёт.
"""

from __future__ import annotations

#границы правил русского счёта: 11–14 ведут себя не так, как 1–4
_TEEN_START = 11
_TEEN_END = 14
_FEW_START = 2
_FEW_END = 4


def plural(count: int, one: str, few: str, many: str) -> str:
    """Возвращает форму слова для числа: 1 строка, 2 строки, 5 строк."""
    remainder_hundred = abs(count) % 100

    if _TEEN_START <= remainder_hundred <= _TEEN_END:
        return many

    remainder_ten = abs(count) % 10

    if remainder_ten == 1:
        return one

    if _FEW_START <= remainder_ten <= _FEW_END:
        return few

    return many


def rows(count: int) -> str:
    """«5 строк», «2 строки», «1 строка» — вместе с числом."""
    return f"{count} {plural(count, 'строка', 'строки', 'строк')}"


def values(count: int) -> str:
    return f"{count} {plural(count, 'значение', 'значения', 'значений')}"
