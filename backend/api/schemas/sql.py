"""Схемы API окна SQL.

Схема результата намеренно не описывает строки типизированно: их состав определяется
запросом и заранее неизвестен. Колонки перечисляются отдельно, потому что их порядок —
часть ответа: словарь строки его не сохраняет.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

#верхняя граница длины совпадает с той, что проверяет движок: клиент узнаёт о превышении
#до отправки восьмидесяти килобайт текста, а не после
MAX_SQL_LENGTH = 20_000


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=MAX_SQL_LENGTH, description="Текст запроса.")
    query_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
        description=(
            "Метка выполнения, чтобы запрос можно было отменить. Задаётся клиентом до "
            "отправки: идентификатор из ответа пришёл бы уже после завершения. "
            "Не задана — отменить запрос будет нечем."
        ),
    )
    row_limit: int | None = Field(
        default=None, ge=1, le=10_000, description="Сколько строк вернуть, не больше предела."
    )
    timeout_seconds: int | None = Field(
        default=None, ge=1, le=300, description="Сколько ждать до прерывания."
    )


class QueryRunResponse(BaseModel):
    run_id: str
    sql: str
    status: str = Field(description="succeeded, failed, timed_out или cancelled.")
    started_at: str
    elapsed_ms: int
    datasets: list[str] = Field(description="Псевдонимы датасетов, к которым обращался запрос.")
    row_count: int | None
    truncated: bool
    truncated_by: str | None
    error_code: str | None = Field(description="Код ошибки. Текст сообщения в историю не пишется.")


class QueryResultResponse(BaseModel):
    columns: list[str] = Field(description="Порядок колонок — часть ответа.")
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = Field(description="Строк было больше предела, показан не весь результат.")
    truncated_by: str | None = Field(description="rows или bytes — чем именно ограничен ответ.")
    elapsed_ms: int


class QueryResponse(BaseModel):
    result: QueryResultResponse
    run: QueryRunResponse


class QueryHistoryResponse(BaseModel):
    runs: list[QueryRunResponse]


class DatasetBindingResponse(BaseModel):
    #то, что можно написать в FROM: интерфейс показывает этот список рядом с редактором,
    #иначе имя датасета в SQL приходится угадывать
    alias: str
    dataset_id: str
    name: str
    row_count: int | None
    column_count: int
    columns: list[str]
    queryable: bool = Field(description="Ложь, если датасет не готов к чтению.")


class DatasetBindingsResponse(BaseModel):
    datasets: list[DatasetBindingResponse]


class SavedQueryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sql: str = Field(min_length=1, max_length=MAX_SQL_LENGTH)
    description: str | None = Field(default=None, max_length=1_000)


class SavedQueryResponse(BaseModel):
    saved_query_id: str
    name: str
    sql: str
    description: str | None
    created_at: str
    updated_at: str


class SavedQueryListResponse(BaseModel):
    queries: list[SavedQueryResponse]
