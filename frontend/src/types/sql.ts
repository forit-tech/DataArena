//#типы окна SQL повторяют контракт backend: строки результата заранее не типизированы,
//#их состав определяется запросом

export type QueryStatus = "succeeded" | "failed" | "timed_out" | "cancelled";

export type QueryRun = {
  run_id: string;
  sql: string;
  status: QueryStatus;
  started_at: string;
  elapsed_ms: number;
  datasets: string[];
  row_count: number | null;
  truncated: boolean;
  truncated_by: string | null;
  error_code: string | null;
};

export type QueryResult = {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  truncated_by: string | null;
  elapsed_ms: number;
};

export type QueryResponse = {
  result: QueryResult;
  run: QueryRun;
};

export type DatasetBinding = {
  alias: string;
  dataset_id: string;
  name: string;
  row_count: number | null;
  column_count: number;
  columns: string[];
  queryable: boolean;
};

export type SavedQuery = {
  saved_query_id: string;
  name: string;
  sql: string;
  description: string | null;
  created_at: string;
  updated_at: string;
};
