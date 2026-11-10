"""Происхождение датасета: откуда он взялся и что стало с каждой колонкой.

Раздел отвечает на два вопроса, которые задают через месяц после сборки:

* **откуда взялся этот датасет** — из какого файла, и если он собран соединением,
  то из каких двух датасетов, в каком их состоянии и по какому ключу;
* **откуда взялась эта колонка** — из какой колонки какого файла, как она называлась
  раньше и какие шаги реально меняли её значения.

Хранится только то, что известно лишь в момент создания датасета: как он создан.
Для собранного датасета это ещё и **снимок родителей** на момент сборки — их отпечатки,
конвейер и уже вычисленная история колонок. Родителя потом могут изменить или удалить,
и история ребёнка обязана остаться прежней: он собран из того, что было, а не из того,
что стало.

Происхождение колонок самого датасета **не хранится** — оно выводится из шагов его
конвейера, которые и так сохранены. Две записи об одном и том же однажды разошлись бы.

Все документы здесь читаются из хранилища и из пакетов, то есть это недоверенные данные.
Разбор строгий: то, что не укладывается в модель, — `ProvenanceError`, а не
правдоподобная подстановка.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#предел длины одной строки в документе происхождения: имя колонки, описание шага.
#Настоящие значения на порядки короче; всё длиннее — повреждение, а не данные
MAX_TEXT = 10_000
#предел числа элементов одного списка: колонок, шагов, событий
MAX_ITEMS = 100_000



class ProvenanceError(ValueError):
    """Документ происхождения не соответствует модели."""


# ── строгий разбор ────────────────────────────────────────────────────────────


def _object(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProvenanceError(f"{what}: ожидался объект.")

    return value


def _text(payload: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or len(value) > MAX_TEXT or (not value and not allow_empty):
        raise ProvenanceError(f"Поле «{key}» должно быть строкой.")

    return value


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    if payload.get(key) is None:
        return None

    return _text(payload, key, allow_empty=True)


def _integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)

    #bool — подкласс int, и `true` в поле номера шага было бы повреждением
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProvenanceError(f"Поле «{key}» должно быть неотрицательным целым.")

    return value


def _items[T](payload: Mapping[str, Any], key: str, parse: Callable[[Any], T]) -> tuple[T, ...]:
    value = payload.get(key)

    if value is None:
        return ()

    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ProvenanceError(f"Поле «{key}» должно быть списком.")

    return tuple(parse(item) for item in value)


def _names(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    def one(item: Any) -> str:
        if not isinstance(item, str) or len(item) > MAX_TEXT:
            raise ProvenanceError(f"Поле «{key}» должно содержать строки.")

        return item

    return _items(payload, key, one)


def _params(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("params")

    if value is None:
        return {}

    if not isinstance(value, dict):
        raise ProvenanceError("Параметры шага должны быть объектом.")

    return dict(value)


# ── перечисления ──────────────────────────────────────────────────────────────


class Origin(StrEnum):
    """Как датасет появился в рабочем пространстве."""

    UPLOAD = "upload"
    JOIN = "join"


class ColumnOrigin(StrEnum):
    """Как колонка появилась."""

    #колонка исходного файла — возможно, переименованная и изменённая шагами
    SOURCE = "source"
    #колонка создана шагом конвейера из других колонок
    STEP = "step"
    #происхождение не установить: датасет собран соединением без сохранённой карты
    #колонок или создан до появления записей о происхождении. Угадывать здесь нельзя —
    #угаданное происхождение неотличимо от настоящего
    UNKNOWN = "unknown"


class JoinSide(StrEnum):
    LEFT = "left"
    RIGHT = "right"


class ProvenanceStatus(StrEnum):
    """Известно ли, как датасет создан."""

    KNOWN = "known"
    #записи нет, она повреждена или происхождение нельзя восстановить достоверно.
    #Это не ошибка и не повод для пятисотки: раздел говорит «неизвестно», а не выдумывает
    LEGACY_UNKNOWN = "legacy_unknown"


class UnknownReason(StrEnum):
    """Почему происхождение неизвестно — чтобы интерфейс объяснил, а не развёл руками."""

    #датасет создан до появления записей о происхождении
    MISSING_RECORD = "missing_record"
    #запись есть, но прочитать её нельзя
    CORRUPTED_RECORD = "corrupted_record"
    #запись больше разумного предела: читать её целиком ради справки нельзя
    OVERSIZED_RECORD = "oversized_record"


def _enum[E: StrEnum](enum: type[E], payload: Mapping[str, Any], key: str) -> E:
    try:
        return enum(_text(payload, key))
    except ValueError as error:
        raise ProvenanceError(f"Недопустимое значение поля «{key}».") from error


# ── ссылки и события ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """Колонка конкретного датасета — точка, откуда пришли значения.

    Для колонки загруженного файла рядом лежат имя файла и его отпечаток: имя датасета
    описывает карточку в рабочем пространстве, а отпечаток — сами байты, из которых
    значения прочитаны.
    """

    dataset_id: str
    dataset_name: str
    column: str
    source_file: str | None = None
    source_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "column": self.column,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ColumnRef:
        payload = _object(value, "Ссылка на колонку")

        return cls(
            dataset_id=_text(payload, "dataset_id"),
            dataset_name=_text(payload, "dataset_name", allow_empty=True),
            column=_text(payload, "column", allow_empty=True),
            source_file=_optional_text(payload, "source_file"),
            source_sha256=_optional_text(payload, "source_sha256"),
        )


class EventKind(StrEnum):
    #шаг конвейера какого-то датасета
    STEP = "step"
    #соединение, из которого колонка попала в собранный датасет
    JOIN = "join"


@dataclass(frozen=True, slots=True)
class ColumnLineageEvent:
    """Событие, после которого значения колонки стали другими или она сменила имя.

    Событие помнит, в каком датасете оно произошло: история колонки собранного датасета
    начинается в конвейере родителя, проходит через соединение и продолжается в конвейере
    самого датасета — и это одна цепочка.
    """

    kind: EventKind
    dataset_id: str
    dataset_name: str
    #номер шага в конвейере своего датасета, с единицы; у соединения — ноль
    position: int
    step_id: str
    operation: str
    description: str
    #колонки, из которых получен результат, — под именами на момент события
    inputs: tuple[str, ...]
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "position": self.position,
            "step_id": self.step_id,
            "operation": self.operation,
            "description": self.description,
            "inputs": list(self.inputs),
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ColumnLineageEvent:
        payload = _object(value, "Событие колонки")

        return cls(
            kind=_enum(EventKind, payload, "kind"),
            dataset_id=_text(payload, "dataset_id"),
            dataset_name=_text(payload, "dataset_name", allow_empty=True),
            position=_integer(payload, "position"),
            step_id=_text(payload, "step_id", allow_empty=True),
            operation=_text(payload, "operation"),
            description=_text(payload, "description", allow_empty=True),
            inputs=_names(payload, "inputs"),
            output=_text(payload, "output", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class DatasetLineageEvent:
    """Шаг конвейера на уровне датасета: что он сделал со строками и с какими колонками.

    `changes` — колонки, значения или имя которых шаг изменил. `reads` — колонки, на которые
    шаг только опирался. Сортировка по «сумме» здесь честно видна как шаг, читавший «сумму»,
    и при этом в истории самой колонки её нет.
    """

    position: int
    step_id: str
    operation: str
    description: str
    params: dict[str, Any]
    changes: tuple[str, ...]
    reads: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "step_id": self.step_id,
            "operation": self.operation,
            "description": self.description,
            "params": self.params,
            "changes": list(self.changes),
            "reads": list(self.reads),
        }

    @classmethod
    def from_dict(cls, value: Any) -> DatasetLineageEvent:
        payload = _object(value, "Шаг датасета")

        return cls(
            position=_integer(payload, "position"),
            step_id=_text(payload, "step_id", allow_empty=True),
            operation=_text(payload, "operation"),
            description=_text(payload, "description", allow_empty=True),
            params=_params(payload),
            changes=_names(payload, "changes"),
            reads=_names(payload, "reads"),
        )


@dataclass(frozen=True, slots=True)
class ColumnLineage:
    """Что известно об одной колонке: откуда пришла, что её меняло, кто её читал."""

    name: str
    dtype: str
    origin: ColumnOrigin
    #колонки-источники. Кортеж, а не одна ссылка: вычисляемая колонка получается
    #из нескольких, и модель не должна ломаться, когда такая операция появится.
    #Пусто — когда происхождение неизвестно
    origins: tuple[ColumnRef, ...] = ()
    #события в хронологическом порядке — от конвейера родителя до конвейера датасета
    events: tuple[ColumnLineageEvent, ...] = ()
    #номера шагов конвейера этого датасета, которые колонку только читали
    read_by: tuple[int, ...] = ()

    @property
    def former_name(self) -> str:
        """Имя, под которым колонка пришла, — если источник один."""
        return self.origins[0].column if len(self.origins) == 1 else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": self.dtype,
            "origin": self.origin.value,
            "origins": [item.to_dict() for item in self.origins],
            "events": [item.to_dict() for item in self.events],
            "read_by": list(self.read_by),
            "source_name": self.former_name,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ColumnLineage:
        payload = _object(value, "История колонки")

        def position(item: Any) -> int:
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise ProvenanceError("Номер шага должен быть неотрицательным целым.")

            return item

        return cls(
            name=_text(payload, "name", allow_empty=True),
            dtype=_text(payload, "dtype", allow_empty=True),
            origin=_enum(ColumnOrigin, payload, "origin"),
            origins=_items(payload, "origins", ColumnRef.from_dict),
            events=_items(payload, "events", ColumnLineageEvent.from_dict),
            read_by=_items(payload, "read_by", position),
        )


# ── то, что хранится ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UploadSource:
    """Файл, из которого датасет загружен, — в момент загрузки."""

    file_name: str
    format: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "format": self.format,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, value: Any) -> UploadSource:
        payload = _object(value, "Источник загрузки")

        return cls(
            file_name=_text(payload, "file_name", allow_empty=True),
            format=_text(payload, "format"),
            sha256=_text(payload, "sha256"),
            bytes=_integer(payload, "bytes"),
        )


@dataclass(frozen=True, slots=True)
class JoinColumn:
    """Одна колонка результата соединения и сторона, откуда она пришла.

    Карта сохраняется при сборке, потому что по результату её не восстановить: имя
    «сумма_2» может быть и правой «суммой», разведённой суффиксом, и родной левой
    колонкой, которую так назвали изначально. Угадывание здесь уже приводило к тому,
    что левую колонку объявляли правой.
    """

    #имя в результате соединения
    name: str
    #какая сторона дала значения
    side: JoinSide
    #имя колонки на этой стороне, до разведения суффиксом
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "side": self.side.value, "source": self.source}

    @classmethod
    def from_dict(cls, value: Any) -> JoinColumn:
        payload = _object(value, "Колонка соединения")

        return cls(
            name=_text(payload, "name", allow_empty=True),
            side=_enum(JoinSide, payload, "side"),
            source=_text(payload, "source", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class ParentSnapshot:
    """Родитель собранного датасета в том состоянии, в каком его соединили.

    Ссылки на родителя недостаточно: его конвейер потом поменяют, а сам датасет могут
    удалить, и история ребёнка, читающая текущего родителя, начала бы рассказывать
    про данные, из которых ребёнок никогда не собирался. Поэтому здесь хранится всё,
    что нужно, чтобы рассказать историю без родителя: отпечатки, шаги и уже вычисленная
    история каждой колонки, вошедшей в соединение.
    """

    role: JoinSide
    dataset_id: str
    dataset_name: str
    #отпечаток байтов, из которых родитель читался, и их формат
    artifact_sha256: str
    artifact_format: str
    #отпечаток конвейера родителя на момент сборки
    pipeline_fingerprint: str
    #было ли известно происхождение самого родителя
    provenance_status: ProvenanceStatus
    #как был создан сам родитель: загрузкой, соединением или неизвестно
    provenance_origin: Origin | None
    #шаги конвейера родителя на момент сборки
    steps: tuple[DatasetLineageEvent, ...] = ()
    #история колонок родителя, вошедших в соединение, — под их именами у родителя
    columns: tuple[ColumnLineage, ...] = ()
    #история колонок не уместилась в предел размера записи и сохранена без событий
    history_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "artifact_sha256": self.artifact_sha256,
            "artifact_format": self.artifact_format,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "provenance_status": self.provenance_status.value,
            "provenance_origin": self.provenance_origin.value if self.provenance_origin else None,
            "steps": [item.to_dict() for item in self.steps],
            "columns": [item.to_dict() for item in self.columns],
            "history_truncated": self.history_truncated,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ParentSnapshot:
        payload = _object(value, "Снимок родителя")
        truncated = payload.get("history_truncated", False)

        if not isinstance(truncated, bool):
            raise ProvenanceError("Поле «history_truncated» должно быть логическим.")

        return cls(
            role=_enum(JoinSide, payload, "role"),
            dataset_id=_text(payload, "dataset_id"),
            dataset_name=_text(payload, "dataset_name", allow_empty=True),
            artifact_sha256=_text(payload, "artifact_sha256"),
            artifact_format=_text(payload, "artifact_format"),
            pipeline_fingerprint=_text(payload, "pipeline_fingerprint"),
            provenance_status=_enum(ProvenanceStatus, payload, "provenance_status"),
            provenance_origin=(
                _enum(Origin, payload, "provenance_origin")
                if payload.get("provenance_origin") is not None
                else None
            ),
            steps=_items(payload, "steps", DatasetLineageEvent.from_dict),
            columns=_items(payload, "columns", ColumnLineage.from_dict),
            history_truncated=truncated,
        )


@dataclass(frozen=True, slots=True)
class JoinParents:
    """Две стороны соединения, ключ и то, что из них получилось."""

    left_dataset_id: str
    right_dataset_id: str
    left_name: str
    right_name: str
    kind: str
    keys: tuple[tuple[str, str], ...]
    right_suffix: str
    #какая колонка результата пришла с какой стороны. Пусто у датасетов, собранных
    #до появления карты: тогда происхождение колонок — «неизвестно», а не выдумка
    columns: tuple[JoinColumn, ...] = ()
    #состояние обоих родителей на момент сборки. Пусто у датасетов, собранных до
    #появления снимков: тогда карта известна, а прошлое родителей — нет
    parents: tuple[ParentSnapshot, ...] = ()

    def snapshot(self, side: JoinSide) -> ParentSnapshot | None:
        for item in self.parents:
            if item.role is side:
                return item

        return None

    def parent_id(self, side: JoinSide) -> str:
        return self.left_dataset_id if side is JoinSide.LEFT else self.right_dataset_id

    def parent_name(self, side: JoinSide) -> str:
        return self.left_name if side is JoinSide.LEFT else self.right_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_dataset_id": self.left_dataset_id,
            "right_dataset_id": self.right_dataset_id,
            "left_name": self.left_name,
            "right_name": self.right_name,
            "kind": self.kind,
            "keys": [{"left": left, "right": right} for left, right in self.keys],
            "right_suffix": self.right_suffix,
            "columns": [item.to_dict() for item in self.columns],
            "parents": [item.to_dict() for item in self.parents],
        }

    @classmethod
    def from_dict(cls, value: Any) -> JoinParents:
        #вид соединения, ключ и суффикс обязательны: прежде вместо отсутствующих
        #подставлялись «inner» и «_2», то есть повреждённая запись рассказывала
        #о соединении, которого никто не выполнял
        payload = _object(value, "Соединение")

        def key(item: Any) -> tuple[str, str]:
            pair = _object(item, "Пара ключа")

            return _text(pair, "left", allow_empty=True), _text(pair, "right", allow_empty=True)

        keys = _items(payload, "keys", key)

        if not keys:
            raise ProvenanceError("У соединения нет ключа.")

        parents = _items(payload, "parents", ParentSnapshot.from_dict)

        if len({item.role for item in parents}) != len(parents):
            raise ProvenanceError("Снимок одной стороны записан дважды.")

        return cls(
            left_dataset_id=_text(payload, "left_dataset_id"),
            right_dataset_id=_text(payload, "right_dataset_id"),
            left_name=_text(payload, "left_name", allow_empty=True),
            right_name=_text(payload, "right_name", allow_empty=True),
            kind=_text(payload, "kind"),
            keys=keys,
            right_suffix=_text(payload, "right_suffix", allow_empty=True),
            columns=_items(payload, "columns", JoinColumn.from_dict),
            parents=parents,
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """Как датасет создан. Пишется один раз — в момент создания.

    Значения по умолчанию нет намеренно. Прежде `Provenance()` означало «загружен файлом»,
    и этим же значением закрывались отсутствующая и повреждённая записи — так собранный
    датасет без записи выдавал себя за файл, взявшийся ниоткуда. Теперь каждый случай
    называется явно: `upload(...)`, `joined(...)` или `unknown(...)`.
    """

    status: ProvenanceStatus
    origin: Origin | None = None
    join: JoinParents | None = None
    #файл загрузки. Может отсутствовать у записей, сделанных до его появления:
    #тогда источником служит неизменяемая запись самого датасета
    source: UploadSource | None = None
    unknown_reason: UnknownReason | None = None

    def __post_init__(self) -> None:
        if self.status is ProvenanceStatus.KNOWN:
            if self.origin is None or self.unknown_reason is not None:
                raise ProvenanceError("Известное происхождение требует способа создания.")
            if (self.origin is Origin.JOIN) != (self.join is not None):
                raise ProvenanceError("Соединение и его родители задаются только вместе.")
            if self.source is not None and self.origin is not Origin.UPLOAD:
                raise ProvenanceError("Файл загрузки бывает только у загрузки.")
        elif (
            self.origin is not None
            or self.join is not None
            or self.source is not None
            or self.unknown_reason is None
        ):
            raise ProvenanceError("Неизвестное происхождение не может содержать подробностей.")

    @classmethod
    def upload(cls, source: UploadSource) -> Provenance:
        return cls(status=ProvenanceStatus.KNOWN, origin=Origin.UPLOAD, source=source)

    @classmethod
    def joined(cls, parents: JoinParents) -> Provenance:
        return cls(status=ProvenanceStatus.KNOWN, origin=Origin.JOIN, join=parents)

    @classmethod
    def unknown(cls, reason: UnknownReason) -> Provenance:
        return cls(status=ProvenanceStatus.LEGACY_UNKNOWN, unknown_reason=reason)

    @property
    def is_upload(self) -> bool:
        return self.status is ProvenanceStatus.KNOWN and self.origin is Origin.UPLOAD

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "origin": self.origin.value if self.origin is not None else None,
            "source": self.source.to_dict() if self.source is not None else None,
            "join": self.join.to_dict() if self.join is not None else None,
            "unknown_reason": self.unknown_reason.value if self.unknown_reason else None,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Provenance:
        """Строгое чтение: всё, что не укладывается в модель, — `ProvenanceError`.

        Решать, что делать с повреждённой записью, — не дело модели: хранилище превращает
        ошибку в «неизвестно». Здесь важно одно — не собрать из мусора правдоподобный ответ.
        """
        payload = _object(value, "Документ происхождения")
        #записи, сделанные до появления статуса, писались только для соединений
        #и всегда явно: у них есть способ создания, и они считаются известными
        status = (
            _enum(ProvenanceStatus, payload, "status")
            if payload.get("status") is not None
            else ProvenanceStatus.KNOWN
        )

        if status is ProvenanceStatus.LEGACY_UNKNOWN:
            return cls.unknown(_enum(UnknownReason, payload, "unknown_reason"))

        origin = _enum(Origin, payload, "origin")
        join = payload.get("join")
        source = payload.get("source")

        return cls(
            status=status,
            origin=origin,
            join=JoinParents.from_dict(join) if join is not None else None,
            source=UploadSource.from_dict(source) if source is not None else None,
        )


# ── то, что вычисляется ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ParentView:
    """Родитель в ответе: снимок на момент сборки и отдельно — что с ним сейчас.

    Текущее состояние не смешивается с историей. Оно нужно ровно для двух честных
    пометок — «родителя больше нет» и «родителя изменили после сборки» — и ни одно поле
    снимка им не подменяется.
    """

    role: JoinSide
    dataset_id: str
    #имя на момент сборки
    dataset_name: str
    snapshot: ParentSnapshot | None
    #существует ли родитель сейчас
    available: bool
    #изменён ли его конвейер после сборки; `None` — сравнить не с чем
    changed_since_join: bool | None

    def to_dict(self) -> dict[str, Any]:
        snapshot = self.snapshot

        return {
            "role": self.role.value,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "snapshot": (
                {
                    "artifact_sha256": snapshot.artifact_sha256,
                    "artifact_format": snapshot.artifact_format,
                    "pipeline_fingerprint": snapshot.pipeline_fingerprint,
                    "provenance_status": snapshot.provenance_status.value,
                    "provenance_origin": (
                        snapshot.provenance_origin.value if snapshot.provenance_origin else None
                    ),
                    "steps": [item.to_dict() for item in snapshot.steps],
                    "history_truncated": snapshot.history_truncated,
                }
                if snapshot is not None
                else None
            ),
            "current": {
                "available": self.available,
                "changed_since_join": self.changed_since_join,
            },
        }


@dataclass(frozen=True, slots=True)
class DatasetLineage:
    """Происхождение датасета целиком — единый ответ для интерфейса и для пакета."""

    dataset_id: str
    name: str
    provenance: Provenance
    #собственные байты датасета: загруженный файл или сохранённый результат сборки
    artifact: UploadSource
    parents: tuple[ParentView, ...]
    #шаги собственного конвейера, по порядку
    events: tuple[DatasetLineageEvent, ...]
    columns: tuple[ColumnLineage, ...]
    #что раздел знает о неполноте ответа: снимков нет, история усечена и тому подобное
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        join = self.provenance.join

        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "status": self.provenance.status.value,
            "unknown_reason": (
                self.provenance.unknown_reason.value if self.provenance.unknown_reason else None
            ),
            "origin": self.provenance.origin.value if self.provenance.origin else None,
            "artifact": self.artifact.to_dict(),
            "join": (
                {
                    "kind": join.kind,
                    "keys": [{"left": left, "right": right} for left, right in join.keys],
                    "right_suffix": join.right_suffix,
                    "columns": [item.to_dict() for item in join.columns],
                }
                if join is not None
                else None
            ),
            "parents": [item.to_dict() for item in self.parents],
            "events": [item.to_dict() for item in self.events],
            "columns": [item.to_dict() for item in self.columns],
            "warnings": list(self.warnings),
        }
