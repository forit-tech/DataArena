"""Decision Lab: многокритериальный выбор модели по измеренным характеристикам."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, UploadFile

from backend.api.schemas.decision import EvaluateRequest
from backend.services import decision as decision_service

router = APIRouter(prefix="/decision", tags=["decision"])


@router.post(
    "/evaluate",
    summary="Многокритериальный выбор: три режима и анализ устойчивости",
    description=(
        "Считает на одной матрице три режима: **A** — по основной метрике, **B** — классический "
        "TOPSIS, **C** — ограничения → Парето-фильтр → TOPSIS → анализ устойчивости "
        "(интервалы весов, доля сценариев весов, устойчивость к шуму измерений).\n\n"
        "Отсутствующее значение (`value: null`) не заменяется нулём: альтернатива без измерения "
        "по выбранному критерию показывается со статусом `incomplete` и в сравнение не входит. "
        "Доля сценариев, в которых альтернатива первая, — мера устойчивости выбора к весам, "
        "а не вероятность её истинного превосходства. При одном seed результат воспроизводится."
    ),
)
def post_evaluate(request: EvaluateRequest) -> dict[str, Any]:
    return decision_service.evaluate(request.model_dump())


@router.post(
    "/import",
    summary="Разобрать файл с характеристиками моделей",
    description=(
        "Принимает лидерборд ModelArena (JSON из `GET /api/arena/runs/{run_id}/leaderboard`), "
        "JSON-матрицу `{criteria, alternatives}` или CSV (столбцы с суффиксом `:max`/`:min`, "
        "столбец `X_std` — неопределённость `X`). Возвращает матрицу для Decision Lab; "
        "ничего не сохраняет."
    ),
)
async def post_import(file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read(decision_service.MAX_IMPORT_BYTES + 1)
    return decision_service.import_file(file.filename or "matrix.json", content)


@router.get("/modelarena/runs", summary="Прогоны запущенной ModelArena")
def get_modelarena_runs() -> dict[str, Any]:
    return decision_service.modelarena_runs()


@router.get("/modelarena/runs/{run_id}", summary="Матрица из лидерборда прогона ModelArena")
def get_modelarena_matrix(run_id: str) -> dict[str, Any]:
    return decision_service.modelarena_matrix(run_id)
