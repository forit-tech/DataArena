"""Псевдонимы датасетов."""

from __future__ import annotations

import pytest

from backend.domain.dataset.aliases import (
    FALLBACK_ALIAS,
    MAX_ALIAS_LENGTH,
    alias_from_name,
    next_free_alias,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("продажи.csv", "продажи"),
        ("Продажи.CSV", "продажи"),
        ("sales_2024.parquet", "sales_2024"),
        ("продажи за 2024 год.xlsx", "продажи_за_2024_год"),
        ("отчёт (копия).csv", "отчёт_копия"),
        ("a---b.csv", "a_b"),
        ("2024.csv", "_2024"),
        #запасное имя достаточно проверить один раз: путь к нему один
        ("...csv", FALLBACK_ALIAS),
        ("data.tar.gz", "data_tar"),
    ],
)
def test_alias_is_derived_from_the_name(name: str, expected: str) -> None:
    assert alias_from_name(name) == expected


def test_alias_is_a_bare_sql_identifier() -> None:
    #псевдоним набирают руками: если для обращения нужны кавычки, им неудобно пользоваться
    alias = alias_from_name("продажи за 2024 год (итог).xlsx")

    assert alias.replace("_", "").isalnum()
    assert not alias[0].isdigit()


def test_composed_and_precomposed_letters_give_the_same_alias() -> None:
    #«й» бывает одним символом и парой «и» + знак: без нормализации это разные псевдонимы
    assert alias_from_name("майский.csv") == alias_from_name("ма\u0438\u0306ский.csv")


def test_long_names_are_cut_to_the_limit() -> None:
    alias = alias_from_name("о" * 200 + ".csv")

    assert len(alias) == MAX_ALIAS_LENGTH


def test_a_free_alias_is_returned_unchanged() -> None:
    assert next_free_alias("продажи", set()) == "продажи"


def test_collisions_get_a_number() -> None:
    assert next_free_alias("продажи", {"продажи"}) == "продажи_2"
    assert next_free_alias("продажи", {"продажи", "продажи_2"}) == "продажи_3"


def test_a_numbered_alias_still_respects_the_length_limit() -> None:
    base = "о" * MAX_ALIAS_LENGTH
    alias = next_free_alias(base, {base})

    assert len(alias) <= MAX_ALIAS_LENGTH
    assert alias.endswith("_2")
