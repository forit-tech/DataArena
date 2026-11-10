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

//#исправление находки шагом конвейера: совет, предпросмотр и результат применения
export type RepairSafety = "safe" | "lossy" | "decision";

export type RepairStep = { kind: string; params: Record<string, unknown>; description: string };

export type RepairOption = {
  option_id: string;
  title: string;
  safety: RepairSafety;
  note: string;
  steps: RepairStep[];
};

export type RepairAdvice = {
  finding_id: string;
  code: string;
  columns: string[];
  explanation: { cause: string; consequence: string; approach: string };
  options: RepairOption[];
  manual_reason: string;
  steps_fingerprint: string;
  source: "rules" | "ai";
};

export type RepairPreview = {
  finding_id: string;
  option: RepairOption;
  steps_fingerprint: string;
  rows_before: number;
  rows_after: number;
  rows_removed: number;
  columns_before: number;
  columns_after: number;
  columns_removed: string[];
  values_lost: number;
  changed_rows: number;
  samples: Array<{ column: string; before: string | null; after: string | null; rows: number }>;
};

export type RepairApplied = {
  applied: Array<{ step_id: string; kind: string; params: Record<string, unknown> }>;
  option: RepairOption;
};
