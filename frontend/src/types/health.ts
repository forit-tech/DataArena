//#типы диагностики повторяют контракт backend. Описания предиката здесь нет намеренно:
//#интерфейс не собирает отбор строк сам, иначе он однажды разойдётся с тем,
//#что посчитала находка

export type Severity = "problem" | "warning" | "notice";
export type Exactness = "exact" | "sampled";

export type SuggestedAction = {
  kind: string;
  columns: string[];
  hint: string;
};

export type Evidence = {
  values: unknown[];
  counts: Record<string, unknown>;
};

export type Finding = {
  finding_id: string;
  code: string;
  severity: Severity;
  scope: "dataset" | "column" | "column_set";
  columns: string[];
  title: string;
  explanation: string;
  exactness: Exactness;
  affected_rows: number | null;
  affected_ratio: number | null;
  sampled_rows: number | null;
  evidence: Evidence;
  suggested_action: SuggestedAction | null;
  //#ложь у находок, затрагивающих весь датасет: кнопки «Показать строки» у них нет,
  //#потому что «показать все строки» — это просто открыть датасет
  has_affected_rows: boolean;
};

export type HealthSummary = {
  problem: number;
  warning: number;
  notice: number;
};

export type HealthReport = {
  dataset_id: string;
  artifact_fingerprint: string;
  checks_version: number;
  computed_at: string;
  total_rows: number;
  summary: HealthSummary;
  findings: Finding[];
};
