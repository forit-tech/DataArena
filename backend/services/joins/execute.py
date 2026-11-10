"""Выполнение соединения: выбор колонок, разведение имён, ленивый результат.

Соединение строится над **результатами конвейеров** обеих сторон, а не над сырыми
файлами: пользователь, почистивший данные, ожидает соединять почищенное. Результат —
ленивый план: он превращается в строки только там, где их действительно показывают
или сохраняют.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from backend.adapters.storage.workspace_store import WorkspaceStore
from backend.domain.joins.models import JoinError, JoinKind, JoinSpec, KeyPair, types_compatible
from backend.domain.lineage.models import JoinColumn, JoinSide
from backend.services.pipeline.workspace import replayed


def prepared(
    store: WorkspaceStore, workspace_root: Path, workspace_id: str, dataset_id: str
) -> tuple[pl.LazyFrame, pl.Schema]:
    """Датасет в том виде, в каком его видит пользователь: после своих шагов."""
    return replayed(store, workspace_root, workspace_id, dataset_id)


@dataclass(frozen=True, slots=True)
class JoinLayout:
    """Решение о колонках соединения: что берём и как разводим одинаковые имена.

    Одно решение на два применения: по нему строится результат и по нему же пишется карта
    происхождения колонок. Считать эти правила второй раз отдельно для карты нельзя —
    два вычисления разошлись бы, и карта начала бы указывать не на те колонки, причём
    молча.
    """

    kind: JoinKind
    keys: tuple[KeyPair, ...]
    #выбранные колонки каждой стороны, в порядке выбора
    left_columns: tuple[str, ...]
    right_columns: tuple[str, ...]
    #правое имя -> имя в результате, для одноимённых колонок
    renames: dict[str, str]

    @property
    def output_names(self) -> tuple[str, ...]:
        """Имена, которые обязаны оказаться в результате, — и никакие другие."""
        return tuple(
            self.renames.get(name, name) if side is JoinSide.RIGHT else name
            for side, name in self._kept()
        )

    @property
    def right_on(self) -> list[str]:
        """Имена ключа справа после разведения: соединять надо по новому имени."""
        return [self.renames.get(key.right, key.right) for key in self.keys]

    def origins(self, result_columns: Sequence[str]) -> tuple[JoinColumn, ...]:
        """Карта «колонка результата -> сторона и имя на ней».

        Правило «какая колонка остаётся» одно — `_kept`; им же пользуется `output_names`.
        Колонка, которую решение не объясняет, в карту не попадает: происхождение скажет
        «неизвестно». Это лучше, чем приписать её стороне по совпадению имени.
        """
        mapping: dict[str, JoinColumn] = {}

        for side, name in self._kept():
            result = self.renames.get(name, name) if side is JoinSide.RIGHT else name
            mapping[result] = JoinColumn(name=result, side=side, source=name)

        return tuple(mapping[name] for name in result_columns if name in mapping)

    def _kept(self) -> list[tuple[JoinSide, str]]:
        """Колонки сторон, которые останутся в результате, — с именами на своей стороне.

        Внутреннее и левое соединения оставляют левый ключ, правое — правый, полное — оба:
        ключ справа там не дублирует левый, потому что у несовпавших строк он свой.
        """
        key_left = {key.left for key in self.keys}
        key_right = {key.right for key in self.keys}
        left = [
            (JoinSide.LEFT, name)
            for name in self.left_columns
            if not (self.kind is JoinKind.RIGHT and name in key_left)
        ]
        right = [
            (JoinSide.RIGHT, name)
            for name in self.right_columns
            if self.kind in {JoinKind.FULL, JoinKind.RIGHT} or name not in key_right
        ]

        return [*left, *right]


def require_key_columns(spec: JoinSpec, left: pl.Schema, right: pl.Schema) -> None:
    """Колонки ключа есть с обеих сторон — до любого вычисления.

    Без этой проверки предпросмотр с несуществующей колонкой доходил до движка
    и возвращал пятисотку вместо объяснения.
    """
    missing_left = [key.left for key in spec.keys if key.left not in left]
    missing_right = [key.right for key in spec.keys if key.right not in right]

    if missing_left or missing_right:
        raise JoinError(
            "Колонок ключа нет в датасете.",
            details={"left": missing_left, "right": missing_right},
        )


def key_type(left: pl.DataType, right: pl.DataType) -> pl.DataType | None:
    """Общий тип пары ключей, в котором их сравнивают и анализ, и соединение.

    `None` — типы совпадают или несравнимы, и приводить нечего. Прежде анализ сравнивал
    значения как текст, а соединение — в исходных типах: Int против Float давал прогноз
    «пар нет» и пятисотку при выполнении, Float32 против Float64 — прогноз «пары есть»
    и пустой результат. Один общий тип на обе стороны убирает оба расхождения.
    """
    if left == right:
        return None

    if left.is_numeric() and right.is_numeric():
        if left.is_integer() and right.is_integer():
            #Int128 вмещает и UInt64, и Int64: большие беззнаковые не превращаются в null
            return pl.Int128()

        return pl.Float64()

    textual = (pl.Utf8, pl.Categorical, pl.Enum)

    if isinstance(left, textual) and isinstance(right, textual):
        return pl.Utf8()

    return None


def normalized_keys(
    left: pl.LazyFrame, right: pl.LazyFrame, spec: JoinSpec
) -> tuple[pl.LazyFrame, pl.LazyFrame]:
    """Обе стороны с ключами, приведёнными к общему типу каждой пары."""
    left_schema = left.collect_schema()
    right_schema = right.collect_schema()
    left_casts = []
    right_casts = []

    for key in spec.keys:
        common = key_type(left_schema[key.left], right_schema[key.right])

        if common is not None:
            left_casts.append(pl.col(key.left).cast(common))
            right_casts.append(pl.col(key.right).cast(common))

    return (
        left.with_columns(left_casts) if left_casts else left,
        right.with_columns(right_casts) if right_casts else right,
    )


def layout(left_schema: pl.Schema, right_schema: pl.Schema, spec: JoinSpec) -> JoinLayout:
    """Решает, какие колонки войдут в результат и какие имена разойдутся суффиксом."""
    require_key_columns(spec, left_schema, right_schema)
    _require_compatible_keys(spec, left_schema, right_schema)

    left_columns = _selected(left_schema, spec.left_columns, [key.left for key in spec.keys])
    right_columns = _selected(right_schema, spec.right_columns, [key.right for key in spec.keys])
    key_left = {key.left for key in spec.keys}
    key_right = {key.right for key in spec.keys}
    #какие ключи остаются в результате, зависит от вида соединения: внутреннее и левое
    #оставляют левый, правое — правый, полное — оба. Разводить суффиксом надо именно то,
    #что останется: иначе конфликт разрешит библиотека своим суффиксом, которого никто
    #не выбирал, и карта колонок укажет не на ту колонку
    left_out = set(left_columns) - key_left if spec.kind is JoinKind.RIGHT else set(left_columns)
    right_keys_out = spec.kind in {JoinKind.FULL, JoinKind.RIGHT}
    right_out = set(right_columns) if right_keys_out else set(right_columns) - key_right
    renames = {
        name: f"{name}{spec.right_suffix}"
        for name in right_columns
        if name in right_out and name in left_out
    }
    taken = left_out | (right_out - set(renames))
    conflicting = set(renames.values()) & taken

    if conflicting:
        raise JoinError(
            f"Суффикс «{spec.right_suffix}» не разводит имена: {', '.join(sorted(conflicting))} "
            "уже заняты.",
            details={"columns": sorted(conflicting)},
        )

    return JoinLayout(
        kind=spec.kind,
        keys=spec.keys,
        left_columns=left_columns,
        right_columns=right_columns,
        renames=renames,
    )


def build(
    left: pl.LazyFrame,
    right: pl.LazyFrame,
    spec: JoinSpec,
) -> pl.LazyFrame:
    """Собирает результат соединения без его выполнения."""
    plan = layout(left.collect_schema(), right.collect_schema(), spec)

    return result_of(*normalized_keys(left, right, spec), plan)


def result_of(left: pl.LazyFrame, right: pl.LazyFrame, plan: JoinLayout) -> pl.LazyFrame:
    """Строит результат по готовому решению о колонках.

    Ключи сторон уже приведены к общему типу (`normalized_keys`). Имена результата
    сверяются с решением: колонка, которую решение не предусмотрело, означает, что имя
    развела библиотека своим суффиксом, — и тогда карта колонок была бы ложью.
    """
    left_selected = left.select(plan.left_columns)
    right_selected = right.select(plan.right_columns)

    if plan.renames:
        right_selected = right_selected.rename(plan.renames)

    result = _joined(left_selected, right_selected, plan)
    unexpected = sorted(set(result.collect_schema().names()) - set(plan.output_names))

    if unexpected:
        raise JoinError(
            f"Имена колонок результата неоднозначны: {', '.join(unexpected)}. "
            "Выберите другой суффикс или снимите одну из одноимённых колонок.",
            details={"columns": unexpected},
        )

    return result


def _joined(
    left_selected: pl.LazyFrame, right_selected: pl.LazyFrame, plan: JoinLayout
) -> pl.LazyFrame:
    return left_selected.join(
        right_selected,
        left_on=[key.left for key in plan.keys],
        right_on=plan.right_on,
        how=plan.kind.value,
        #порядок строк результата не должен зависеть от плана: иначе одна и та же сборка
        #давала бы разный порядок, и воспроизводимость терялась бы незаметно
        maintain_order="left_right",
    )


def _require_compatible_keys(spec: JoinSpec, left: pl.Schema, right: pl.Schema) -> None:
    """Отказ до соединения, а не необработанная ошибка движка после.

    Измерено: соединение `Int64` с `String` даёт `SchemaError` изнутри Polars — то есть
    пятисотку на действие, о котором анализ честно предупредил. Предупредить и уронить —
    хуже, чем просто отказать.
    """
    mismatched = [
        (key.left, str(left[key.left]), key.right, str(right[key.right]))
        for key in spec.keys
        if key.left in left
        and key.right in right
        and not types_compatible(str(left[key.left]), str(right[key.right]))
    ]

    if mismatched:
        pairs = ", ".join(
            f"«{name}» ({left_type}) и «{other}» ({right_type})"
            for name, left_type, other, right_type in mismatched
        )
        raise JoinError(
            f"Типы ключа не совпадают: {pairs}. Приведите колонку к нужному типу шагом "
            "конвейера — соединение числа с текстом не найдёт ни одной пары.",
            details={"keys": [{"left": item[0], "right": item[2]} for item in mismatched]},
        )


def _selected(schema: pl.Schema, columns: tuple[str, ...], keys: list[str]) -> tuple[str, ...]:
    if not columns:
        return tuple(schema.names())

    missing = [name for name in columns if name not in schema]

    if missing:
        raise JoinError(
            f"Нет колонок: {', '.join(missing)}.", details={"columns": missing}
        )

    #ключ остаётся всегда: без него соединение невозможно, а пользователь, снявший
    #галочку с ключевой колонки, имел в виду «не показывать её», а не «не соединять»
    return tuple(dict.fromkeys([*keys, *columns]))
