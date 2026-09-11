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
  //#null или total_is_exact=false означает «строк больше»: показывать это надо как «более N»
  total_rows: number | null;
  total_is_exact: boolean;
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
