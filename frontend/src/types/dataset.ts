export type ColumnSchema = {
  name: string;
  position: number;
  physical_type: string;
  logical_type: string;
  semantic_type: SemanticType;
  nullable: boolean;
};

export type SemanticType = "id" | "numeric" | "categorical" | "datetime" | "boolean" | "text";

export type DatasetSource = {
  file_name: string;
  format: string;
  bytes: number;
  sha256: string;
};

export type Conversion = {
  source_format: string;
  derived_format: string;
  converter: string;
  converter_version: number;
  created_at: string;
  row_count: number;
  warnings: string[];
};

export type Dataset = {
  dataset_id: string;
  //#имя, под которым датасет доступен в SQL: его подбирает backend
  alias: string;
  workspace_id: string;
  name: string;
  source: DatasetSource;
  schema: { columns: ColumnSchema[] };
  //#null означает «точное число строк ещё не считалось», а не «строк нет»
  row_count: number | null;
  column_count: number;
  status: "ready" | "normalization_failed";
  status_reason: string | null;
  original_format: string;
  working_format: string;
  normalized: boolean;
  conversion: Conversion | null;
  created_at: string;
};

export type Workspace = {
  workspace_id: string;
  name: string;
  created_at: string;
  dataset_count: number;
};

export type Page = {
  columns: string[];
  rows: Record<string, unknown>[];
  //#номер строки в файле, по одному на строку страницы. Не номер на странице:
  //#после сортировки «третья сверху» — каждый раз другая строка
  row_ordinals: number[];
  offset: number;
  limit: number;
  //#число строк после фильтров, всегда точное: срез, из-за которого находка
  //#диагностики расходилась с таблицей, убран
  total_rows: number;
};

export type ColumnStatistics = {
  name: string;
  dtype: string;
  row_count: number;
  null_count: number;
  null_ratio: number;
  unique_count: number;
  unique_ratio: number;
  numeric: Record<string, number | null> | null;
  top_values: { value: unknown; count: number; ratio: number }[] | null;
  histogram: { bins: number[]; counts: number[]; constant: boolean } | null;
  examples: unknown[];
};

export type SortDirection = "asc" | "desc";

export type FilterOperator =
  | "eq"
  | "ne"
  | "contains"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "is_null"
  | "is_not_null";

export type ColumnFilter = {
  column: string;
  operator: FilterOperator;
  value: string;
};

//#шаг конвейера и его последствия. Исходный датасет не меняется никогда: конвейер —
//#это описание, а результат строится повторным применением шагов
export type PipelineStep = {
  step_id: string;
  kind: string;
  params: Record<string, unknown>;
  title: string;
  description: string;
  columns_before: string[];
  columns_after: string[];
  added_columns: string[];
  removed_columns: string[];
  destructive: boolean;
};

export type PipelineView = {
  dataset_id: string;
  steps: PipelineStep[];
  can_undo: boolean;
  can_redo: boolean;
  source_columns: string[];
  result_columns: string[];
  result_types: Record<string, string>;
};

export type StepImpact = {
  rows_before: number;
  rows_after: number;
  rows_removed: number;
  columns_before: number;
  columns_after: number;
  values_lost: number;
};

export type OperationVocabulary = {
  operations: { kind: string; title: string }[];
  cast_targets: string[];
  case_modes: string[];
};

//#сборка датасета: анализ ключей и прогноз размера считаются на сервере по данным
export type KeyPair = { left: string; right: string };

export type JoinAnalysis = {
  keys: {
    pair: KeyPair;
    left_type: string;
    right_type: string;
    types_compatible: boolean;
    left_nulls: number;
    right_nulls: number;
    left_unique_ratio: number;
    right_unique_ratio: number;
  }[];
  cardinality: string;
  left_rows: number;
  right_rows: number;
  matched_left_rows: number;
  matched_right_rows: number;
  unmatched_left_rows: number;
  unmatched_right_rows: number;
  estimated_rows: number;
  row_explosion: boolean;
  warnings: string[];
};

export type JoinSuggestion = {
  left: string;
  right: string;
  left_type: string;
  right_type: string;
  overlap_share: number;
  expected_relation: string;
  confidence: number;
  strength: string;
  reasons: string[];
};

export type JoinRequest = {
  left_dataset_id: string;
  right_dataset_id: string;
  keys: KeyPair[];
  kind: string;
  left_columns?: string[];
  right_columns?: string[];
  right_suffix: string;
};

//#формат выгрузки вместе с его ограничениями: интерфейс не должен угадывать,
//#что XLSX теряет типы и ограничен по числу строк
export type FormatInfo = {
  key: string;
  label: string;
  extensions: string[];
  can_write: boolean;
  preserves_schema: boolean;
  preserves_nulls: boolean;
  row_limit: number | null;
  warnings: string[];
};

//#происхождение: откуда взялся датасет и что стало с каждой колонкой
export type ColumnRef = {
  dataset_id: string;
  dataset_name: string;
  column: string;
  //#имя и отпечаток загруженного файла, если колонка из файла
  source_file: string | null;
  source_sha256: string | null;
};

export type ColumnLineageEvent = {
  //#step — шаг конвейера какого-то датасета, join — соединение
  kind: "step" | "join";
  dataset_id: string;
  dataset_name: string;
  position: number;
  step_id: string;
  operation: string;
  description: string;
  inputs: string[];
  output: string;
};

export type DatasetLineageEvent = {
  position: number;
  step_id: string;
  operation: string;
  description: string;
  params: Record<string, unknown>;
  //#колонки, которые шаг изменил, и те, которые только читал
  changes: string[];
  reads: string[];
};

export type ColumnHistory = {
  name: string;
  //#unknown — происхождение не установлено, и оно не угадывается
  origin: "source" | "step" | "unknown";
  dtype: string;
  origins: ColumnRef[];
  events: ColumnLineageEvent[];
  read_by: number[];
  source_name: string;
};

export type LineageParent = {
  role: "left" | "right";
  dataset_id: string;
  //#имя на момент сборки
  dataset_name: string;
  snapshot: {
    artifact_sha256: string;
    artifact_format: string;
    pipeline_fingerprint: string;
    provenance_status: "known" | "legacy_unknown";
    provenance_origin: "upload" | "join" | null;
    steps: DatasetLineageEvent[];
    history_truncated: boolean;
  } | null;
  //#что с родителем сейчас — отдельно от истории
  current: { available: boolean; changed_since_join: boolean | null };
};

export type LineageWarning =
  | "provenance_unknown"
  | "join_column_map_missing"
  | "join_parent_snapshots_missing"
  | "parent_history_truncated";

export type LineageView = {
  dataset_id: string;
  name: string;
  status: "known" | "legacy_unknown";
  unknown_reason: "missing_record" | "corrupted_record" | "oversized_record" | null;
  origin: "upload" | "join" | null;
  artifact: { file_name: string; format: string; sha256: string; bytes: number };
  join: {
    kind: string;
    keys: { left: string; right: string }[];
    right_suffix: string;
    columns: { name: string; side: "left" | "right"; source: string }[];
  } | null;
  parents: LineageParent[];
  events: DatasetLineageEvent[];
  columns: ColumnHistory[];
  warnings: LineageWarning[];
};
