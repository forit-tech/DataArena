import type {
  ColumnFilter,
  ColumnStatistics,
  Dataset,
  Page,
  SortDirection,
  Workspace,
  FormatInfo,
  JoinAnalysis,
  JoinRequest,
  JoinSuggestion,
  LineageView,
  OperationVocabulary,
  PipelineView,
  StepImpact,
} from "../types/dataset";
import type { HealthReport, RepairAdvice, RepairApplied, RepairPreview } from "../types/health";
import type { DatasetBinding, QueryResponse, QueryRun, SavedQuery } from "../types/sql";
import type { HealthStatus } from "../types/system";
import type { DecisionAnalysis, ImportedMatrix, ModelArenaRun } from "../types/decision";
import type { AssemblyDiscovery } from "../types/assembly";
import type { AiAssemblyAdvice, AiFindingAdvice, AiStatus } from "../types/ai";
import { API_PREFIX, deleteVoid, getJson, postForm, postJson, putJson } from "./client";

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

export function fetchDatasets(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<{ datasets: Dataset[] }> {
  return getJson(`/workspaces/${workspaceId}/datasets`, signal);
}

export function uploadDataset(workspaceId: string, file: File): Promise<Dataset> {
  const formData = new FormData();
  formData.append("file", file);
  return postForm<Dataset>(`/workspaces/${workspaceId}/datasets`, formData);
}

export function deleteDataset(workspaceId: string, datasetId: string): Promise<void> {
  return deleteVoid(`/workspaces/${workspaceId}/datasets/${datasetId}`);
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

export type RowSource = "dataset" | "pipeline";

export function fetchRows(
  workspaceId: string,
  datasetId: string,
  query: PageQuery,
  signal?: AbortSignal,
  //#таблица одна и та же: «до» читает исходник, «после» — результат конвейера.
  //#Вторая таблица для результата означала бы вторую реализацию сортировки и фильтров
  source: RowSource = "dataset",
): Promise<Page> {
  const params = pageSearchParams(query);
  const path = source === "pipeline" ? "pipeline/rows" : "rows";
  return getJson<Page>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/${path}?${params.toString()}`,
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

//#исправление находки: совет ничего не меняет, применение добавляет шаги в конвейер,
//#откат — обычная отмена конвейера (undoPipeline)
function findingUrl(workspaceId: string, datasetId: string, findingId: string): string {
  return `/workspaces/${workspaceId}/datasets/${datasetId}/health/findings/${encodeURIComponent(findingId)}`;
}

export function fetchRepair(workspaceId: string, datasetId: string, findingId: string): Promise<RepairAdvice> {
  return getJson<RepairAdvice>(`${findingUrl(workspaceId, datasetId, findingId)}/repair`);
}

export function previewRepair(
  workspaceId: string,
  datasetId: string,
  findingId: string,
  optionId: string,
): Promise<RepairPreview> {
  return postJson<RepairPreview>(`${findingUrl(workspaceId, datasetId, findingId)}/repair/preview`, {
    option_id: optionId,
  });
}

export function applyRepair(
  workspaceId: string,
  datasetId: string,
  findingId: string,
  optionId: string,
  stepsFingerprint: string,
): Promise<RepairApplied> {
  return postJson<RepairApplied>(`${findingUrl(workspaceId, datasetId, findingId)}/repair/apply`, {
    option_id: optionId,
    steps_fingerprint: stepsFingerprint,
  });
}

// ── конвейер преобразований ──────────────────────────────────────────────────

export function fetchOperations(signal?: AbortSignal): Promise<OperationVocabulary> {
  return getJson<OperationVocabulary>("/pipeline/operations", signal);
}

export function fetchPipeline(
  workspaceId: string,
  datasetId: string,
  signal?: AbortSignal,
): Promise<PipelineView> {
  return getJson<PipelineView>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/pipeline`,
    signal,
  );
}

export function savePipeline(
  workspaceId: string,
  datasetId: string,
  steps: { step_id: string; kind: string; params: Record<string, unknown> }[],
): Promise<PipelineView> {
  return putJson<PipelineView>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/pipeline`,
    { steps },
  );
}

export function undoPipeline(workspaceId: string, datasetId: string): Promise<PipelineView> {
  return postJson<PipelineView>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/pipeline/undo`,
    {},
  );
}

export function redoPipeline(workspaceId: string, datasetId: string): Promise<PipelineView> {
  return postJson<PipelineView>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/pipeline/redo`,
    {},
  );
}

export function fetchStepImpact(
  workspaceId: string,
  datasetId: string,
  step: { step_id: string; kind: string; params: Record<string, unknown> },
  before: { step_id: string; kind: string; params: Record<string, unknown> }[],
): Promise<StepImpact> {
  return postJson<StepImpact>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/pipeline/impact`,
    { step, before },
  );
}

// ── сборка датасета ──────────────────────────────────────────────────────────

export function discoverAssembly(workspaceId: string): Promise<AssemblyDiscovery> {
  return getJson<AssemblyDiscovery>(`/workspaces/${workspaceId}/joins/discover`);
}

export function analyseJoin(workspaceId: string, request: JoinRequest): Promise<JoinAnalysis> {
  return postJson<JoinAnalysis>(`/workspaces/${workspaceId}/joins/analyse`, request);
}

export function suggestJoinKeys(
  workspaceId: string,
  leftDatasetId: string,
  rightDatasetId: string,
): Promise<{ suggestions: JoinSuggestion[] }> {
  const params = new URLSearchParams({
    left_dataset_id: leftDatasetId,
    right_dataset_id: rightDatasetId,
  });
  return postJson<{ suggestions: JoinSuggestion[] }>(
    `/workspaces/${workspaceId}/joins/suggest?${params.toString()}`,
    {},
  );
}

export function previewJoin(workspaceId: string, request: JoinRequest): Promise<Page> {
  return postJson<Page>(`/workspaces/${workspaceId}/joins/preview`, request);
}

export function createJoin(
  workspaceId: string,
  request: JoinRequest & { name: string },
): Promise<Dataset> {
  return postJson<Dataset>(`/workspaces/${workspaceId}/joins`, request);
}

// ── выгрузка и пакет ─────────────────────────────────────────────────────────

export function exportUrl(workspaceId: string, datasetId: string, format: string): string {
  return `${API_PREFIX}/workspaces/${workspaceId}/datasets/${datasetId}/export?format=${encodeURIComponent(format)}`;
}

export function packageUrl(workspaceId: string, datasetId: string): string {
  return `${API_PREFIX}/workspaces/${workspaceId}/datasets/${datasetId}/package`;
}

export function fetchWritableFormats(signal?: AbortSignal): Promise<{ formats: FormatInfo[] }> {
  return getJson<{ formats: FormatInfo[] }>("/export/formats", signal);
}

// ── происхождение ────────────────────────────────────────────────────────────

export function fetchLineage(
  workspaceId: string,
  datasetId: string,
  signal?: AbortSignal,
): Promise<LineageView> {
  return getJson<LineageView>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/lineage`,
    signal,
  );
}

//#Decision Lab: расчёт ничего не сохраняет, импорт только разбирает файл
export type EvaluateBody = {
  criteria: Array<{ key: string; label: string; direction: "max" | "min"; weight: number; unit: string; group: string }>;
  alternatives: Array<{
    key: string;
    label: string;
    values: Record<string, { value: number | null; std: number | null; note: string }>;
    meta: Record<string, string>;
  }>;
  constraints: Array<{ criterion: string; op: "<=" | ">="; bound: number }>;
  normalization: "vector" | "minmax";
  primary: string | null;
  sensitivity: { samples: number; seed: number; concentration: number; grid_step: number; stability_threshold: number };
  scales?: Array<{ criterion: string; kind: "linear" | "log"; worst: number; best: number }>;
};

export function evaluateDecision(body: EvaluateBody): Promise<DecisionAnalysis> {
  return postJson<DecisionAnalysis>("/decision/evaluate", body);
}

export function importDecisionMatrix(file: File): Promise<ImportedMatrix> {
  const formData = new FormData();
  formData.append("file", file);
  return postForm<ImportedMatrix>("/decision/import", formData);
}

export function fetchModelArenaRuns(): Promise<{ base_url: string | null; runs: ModelArenaRun[] }> {
  return getJson("/decision/modelarena/runs");
}

export function fetchModelArenaMatrix(runId: string): Promise<ImportedMatrix> {
  return getJson<ImportedMatrix>(`/decision/modelarena/runs/${encodeURIComponent(runId)}`);
}


// ── локальная модель (ИИ-советник) ───────────────────────────────────────────

export function fetchAiStatus(): Promise<AiStatus> {
  return getJson<AiStatus>("/ai/status");
}

export function askAiAboutFinding(
  workspaceId: string,
  datasetId: string,
  findingId: string,
  model: string | null,
): Promise<AiFindingAdvice> {
  return postJson<AiFindingAdvice>(
    `/workspaces/${workspaceId}/datasets/${datasetId}/health/findings/${encodeURIComponent(findingId)}/ai`,
    { model },
  );
}

export function askAiAboutPair(
  workspaceId: string,
  leftDatasetId: string,
  rightDatasetId: string,
  model: string | null,
): Promise<AiAssemblyAdvice> {
  return postJson<AiAssemblyAdvice>(`/workspaces/${workspaceId}/joins/ai`, {
    left_dataset_id: leftDatasetId,
    right_dataset_id: rightDatasetId,
    model,
  });
}
