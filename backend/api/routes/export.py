"""Выгрузка результата в файл."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.api.schemas.export import FormatsResponse
from backend.services import export as export_service
from backend.services.context import get_workspace_root, get_workspace_store
from backend.services.pipeline.workspace import replayed
from backend.services.workspace_access import get_dataset_of_workspace

router = APIRouter(tags=["export"])


@router.get(
    "/export/formats",
    response_model=FormatsResponse,
    summary="Форматы выгрузки",
    description=(
        "Форматы, в которые можно записать, вместе с их ограничениями: предел строк, "
        "сохраняются ли типы и пропуски. Ограничения приходят с сервера — интерфейс "
        "не должен угадывать, что XLSX теряет типы."
    ),
)
def get_formats() -> dict:
    return {"formats": export_service.formats()}


@router.get(
    "/workspaces/{workspace_id}/datasets/{dataset_id}/export",
    summary="Выгрузить результат",
    description=(
        "Отдаёт файл с результатом конвейера — тем, что пользователь видит на экране, "
        "а не исходником.\n\n"
        "CSV, Parquet и JSONL пишутся потоково: миллион строк не собирается в памяти. "
        "Для форматов, требующих материализации, действует предел строк, и при его "
        "превышении выгрузка отклоняется, а не обрезается молча.\n\n"
        "В CSV и TSV значения, которые табличный редактор выполнил бы как формулу "
        "(начинаются с «=», «@», или с «+»/«-», но не являются числом), помечаются "
        "апострофом. Числа вроде «-5» и телефоны вроде «+7 999» не меняются. В XLSX "
        "строки записываются строками и формулами не становятся, поэтому там значения "
        "остаются как есть."
    ),
    response_model=None,
)
def get_export(
    workspace_id: str,
    dataset_id: str,
    format: Annotated[str, Query(description="csv, tsv, parquet, json, jsonl, xlsx…")] = "csv",
) -> FileResponse:
    store = get_workspace_store()
    workspace_root = get_workspace_root()
    dataset = get_dataset_of_workspace(store, workspace_id, dataset_id)
    lazy, schema = replayed(store, workspace_root, workspace_id, dataset_id)

    directory = Path(tempfile.mkdtemp(prefix="dataarena_export_"))

    try:
        result = export_service.export(lazy, schema, format, directory, dataset.name)
    except BaseException:
        #отказ выгрузки не должен оставлять каталог с половиной файла: фоновая очистка
        #назначается только успешному ответу, и прежде такие каталоги копились
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return FileResponse(
        result.path,
        media_type=result.media_type,
        filename=result.file_name,
        #временный каталог удаляется после отправки: иначе выгрузки копились бы
        #во временной папке до перезагрузки машины
        background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True),
    )
