"""Пакет датасета для ModelArena."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.api.routes.system import APP_VERSION
from backend.services import lineage as lineage_service
from backend.services.context import get_workspace_root, get_workspace_store
from backend.services.package import builder as package_builder
from backend.services.package.builder import PackageError
from backend.services.pipeline.workspace import replayed
from backend.services.workspace_access import get_dataset_of_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/datasets/{dataset_id}", tags=["package"]
)


@router.get(
    "/package",
    summary="Собрать пакет для ModelArena",
    description=(
        "Отдаёт `.dapkg` — пакет формата `dataarena.package/1`. Внутри данные, схема, "
        "профиль, происхождение и рецепт.\n\n"
        "Собирается результат конвейера, а не исходник. Происхождение и рецепт берутся "
        "из настоящих шагов: по пакету можно ответить, из чего и как получился датасет, "
        "и повторить сборку на новой версии файла.\n\n"
        "Пакет детерминирован: две сборки одного датасета дают одинаковый архив."
    ),
    response_model=None,
)
def get_package(workspace_id: str, dataset_id: str) -> FileResponse:
    store = get_workspace_store()
    workspace_root = get_workspace_root()
    dataset = get_dataset_of_workspace(store, workspace_id, dataset_id)
    steps = store.get_pipeline(dataset_id).steps
    lazy, _ = replayed(store, workspace_root, workspace_id, dataset_id)
    directory = Path(tempfile.mkdtemp(prefix="dataarena_package_"))

    try:
        built = package_builder.build(
            dataset,
            lazy.collect(),
            steps,
            directory,
            producer_version=APP_VERSION,
            lineage=lineage_service.resolve(store, workspace_root, workspace_id, dataset_id),
        )
        #данные, рецепт и происхождение прочитали конвейер каждый сам. Если между этими
        #чтениями его изменили, пакет описывал бы одними шагами данные, собранные другими, —
        #такой пакет не выпускается
        if lineage_service.pipeline_fingerprint(store.get_pipeline(dataset_id).steps) != (
            lineage_service.pipeline_fingerprint(steps)
        ):
            raise PackageError(
                "Конвейер датасета изменился во время сборки пакета. Повторите сборку.",
                details={"dataset_id": dataset_id},
            )
    except BaseException:
        #отказ сборки не оставляет каталог с половиной пакета
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return FileResponse(
        built.path,
        media_type="application/zip",
        filename=built.file_name,
        background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True),
    )
