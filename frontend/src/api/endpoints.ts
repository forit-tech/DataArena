import type {
  ColumnFilter,
  ColumnStatistics,
  Dataset,
  Page,
  SortDirection,
  Workspace,
} from "../types/dataset";
import type { HealthReport } from "../types/health";
import type { DatasetBinding, QueryResponse, QueryRun, SavedQuery } from "../types/sql";
import type { HealthStatus } from "../types/system";
import { deleteVoid, getJson, postForm, postJson } from "./client";

//#каждый endpoint описан ровно один раз и возвращает типизированный результат
//#компоненты не знают путей API и не собирают запросы вручную

export function fetchHealth(): Promise<HealthStatus> {
  return getJson<HealthStatus>("/system/health");
}

export function createWorkspace(name: string): Promise<Workspace> {
  return postJson<Workspace>("/workspaces", { name });
}

export function fetchWorkspace(workspaceId: string): Promise<Workspace> {
  return getJson<Workspace>(`/workspaces/${workspaceId}`);
}

export function fetchDatasets(workspaceId: string): Promise<{ datasets: Dataset[] }> {
  return getJson(`/workspaces/${workspaceId}/datasets`);
}

export function uploadDataset(workspaceId: string, file: File): Promise<Dataset> {
  const formData = new FormData();
  formData.append("file", file);
  return postForm<Dataset>(`/workspaces/${workspaceId}/datasets`, formData);
}

export type PageQuery = {
  offset: number;
  limit: number;
  sort: { column: string; direction: SortDirection }[];
  filters: ColumnFilter[];
  search: string;
  //#строки находки диагностики. Отбор выполняет сервер: интерфейс передаёт
  //#идентификатор находки, а не собирает предикат сам
  finding?: string | null;
};

function pageSearchParams(query: PageQuery): URLSearchParams {
  //#параметры собираются здесь, а не в компоненте: формат «column:direction»
  //#и «column:operator:value» — часть контракта API, и знать о нём должен один модуль
  const params = new URLSearchParams();
  params.set("offset", String(query.offset));
  params.set("limit", String(query.limit));

  query.sort.forEach((item) => params.append("sort", `${item.column}:${item.direction}`));
  query.filters.forEach((item) =>
    params.append(
      "filter",
      item.operator === "is_null" || item.operator === "is_not_null"
        ? `${item.column}:${item.operator}`
        : `${item.column}:${item.operator}:${item.value}`,
    ),
  );

  if (query.search.trim()) {
    params.set("search", query.search.trim());
  }

  if (query.finding) {
    params.set("finding", query.finding);
  }

  return params;
}

export function fetchRows(
  workspaceId: string,
  datasetId: string,
  query: PageQuery,
  signal?: AbortSignal,
): Promise<Page> {
  const params = pageSearchParams(query);
  return getJson<Page>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/rows?${params.toString()}`,
    signal,
  );
}

export function fetchColumnStatistics(
  workspaceId: string,
  datasetId: string,
  column: string,
  signal?: AbortSignal,
): Promise<ColumnStatistics> {
  return getJson<ColumnStatistics>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/columns/${encodeURIComponent(column)}`,
    signal,
  );
}

// ── окно SQL ─────────────────────────────────────────────────────────────────

export function fetchQueryBindings(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<{ datasets: DatasetBinding[] }> {
  return getJson(`/workspaces/${workspaceId}/sql/datasets`, signal);
}

export type RunQueryBody = {
  sql: string;
  //#метка задаётся клиентом ДО отправки: идентификатор из ответа пришёл бы уже после
  //#завершения запроса, и отменять было бы нечего
  query_token?: string;
  row_limit?: number;
  timeout_seconds?: number;
};

export function runQuery(workspaceId: string, body: RunQueryBody): Promise<QueryResponse> {
  return postJson<QueryResponse>(`/workspaces/${workspaceId}/sql/queries`, body);
}

export function cancelQuery(workspaceId: string, queryToken: string): Promise<void> {
  return deleteVoid(`/workspaces/${workspaceId}/sql/queries/${queryToken}`);
}

export function fetchQueryHistory(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<{ runs: QueryRun[] }> {
  return getJson(`/workspaces/${workspaceId}/sql/history?limit=50`, signal);
}

export function fetchSavedQueries(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<{ queries: SavedQuery[] }> {
  return getJson(`/workspaces/${workspaceId}/sql/saved`, signal);
}

export function saveQuery(
  workspaceId: string,
  body: { name: string; sql: string; description?: string },
): Promise<SavedQuery> {
  return postJson<SavedQuery>(`/workspaces/${workspaceId}/sql/saved`, body);
}

export function deleteSavedQuery(workspaceId: string, savedQueryId: string): Promise<void> {
  return deleteVoid(`/workspaces/${workspaceId}/sql/saved/${savedQueryId}`);
}


// ── диагностика ──────────────────────────────────────────────────────────────

export function fetchDatasetHealth(
  workspaceId: string,
  datasetId: string,
  signal?: AbortSignal,
): Promise<HealthReport> {
  return getJson<HealthReport>(`/workspaces/${workspaceId}/datasets/${datasetId}/health`, signal);
}
