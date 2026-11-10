//#типы Decision Lab повторяют ответы backend/api/routes/decision.py

export type Direction = "max" | "min";
export type Normalization = "vector" | "minmax";

export type CriterionDraft = {
  key: string;
  label: string;
  direction: Direction;
  weight: number;
  unit: string;
  group: string;
  //#выключенный критерий не уходит в расчёт вовсе; вес 0 — уходит как «только для ограничений»
  enabled: boolean;
};

export type MeasurementValue = { value: number | null; std: number | null; note: string };

export type AlternativeDraft = {
  key: string;
  label: string;
  values: Record<string, MeasurementValue>;
  meta: Record<string, string>;
  enabled: boolean;
};

export type ConstraintDraft = { criterion: string; op: "<=" | ">="; bound: number };

export type SensitivityDraft = {
  samples: number;
  seed: number;
  concentration: number;
  grid_step: number;
  stability_threshold: number;
};

export type ImportedMatrix = {
  source: "modelarena" | "matrix" | "csv" | "demo";
  criteria: Array<Omit<CriterionDraft, "enabled">>;
  alternatives: Array<Omit<AlternativeDraft, "enabled">>;
  primary: string | null;
  context: Record<string, unknown>;
  notes: string[];
};

export type AlternativeStatus = "incomplete" | "infeasible" | "unverified" | "dominated" | "pareto" | "feasible" | "pending";

export type ConstraintCheck = {
  criterion: string;
  op: string;
  bound: number;
  description: string;
  observed: number | null;
  satisfied: boolean | null;
};

export type AnalysedAlternative = {
  key: string;
  label: string;
  meta: Record<string, string>;
  values: Record<string, MeasurementValue>;
  missing: string[];
  constraints: ConstraintCheck[];
  violated: string[];
  unverified: string[];
  status: AlternativeStatus;
  status_reason: string;
  dominated_by: string[];
};

export type RankingRow = { key: string; closeness: number; d_plus: number; d_minus: number; rank: number };

export type Interval = {
  criterion: string;
  current: number;
  low: number;
  high: number;
  segments: Array<{ start: number; end: number; winner: string }>;
};

export type Verdict = "stable" | "conditional" | "unstable" | "none";

export type DecisionAnalysis = {
  method: {
    version: string;
    normalization: Normalization;
    seed: number;
    samples: number;
    concentration: number;
    grid_step: number;
    stability_threshold: number;
    input_sha256: string;
  };
  criteria: Array<{
    key: string;
    label: string;
    unit: string;
    direction: Direction;
    group: string;
    weight: number;
    effective_weight: number;
    role: "scored" | "constraint_only" | "unused";
  }>;
  alternatives: AnalysedAlternative[];
  modes: {
    primary: {
      criterion: string;
      ranking: Array<{ key: string; value: number; std: number | null; rank: number }>;
      winner: string | null;
      indistinguishable: string[];
      winner_violates: string[];
    };
    topsis: {
      ranking: RankingRow[];
      winner: string | null;
      winner_violates: string[];
      excluded: Array<{ key: string; reason: string }>;
      degenerate: string[];
      influence?: Record<string, number>;
    };
    robust: {
      ranking: RankingRow[];
      winner: string | null;
      pareto: string[];
      feasible_count: number;
      degenerate?: string[];
      influence?: Record<string, number>;
      verdict: Verdict;
      verdict_text: string;
      winner_local_share?: number;
      winner_noise_share?: number | null;
    };
    fixed?: FixedMode;
  };
  robustness: {
    local: Record<string, number>;
    global: Record<string, number>;
    noise: Record<string, number> | null;
    noise_note: string;
    mean_rank_local: Record<string, number>;
    rank_share: Record<string, number[]>;
    intervals: Interval[];
  };
  agreement: {
    primary_vs_topsis: boolean;
    topsis_vs_robust: boolean;
    primary_vs_robust: boolean;
    robust_vs_fixed?: boolean;
  };
  explanation: string[];
  warnings: string[];
};

export type ModelArenaRun = {
  run_id: string;
  created_at?: string;
  state?: string;
  task_type?: string;
  target_column?: string;
  contenders?: { planned: number; succeeded: number };
};

export type ScaleKind = "linear" | "log";

export type ResolvedScale = {
  criterion: string;
  kind: ScaleKind;
  worst: number;
  best: number;
  source: "natural" | "budget" | "user" | "observed";
  note: string;
};

export type ScaleDraft = { criterion: string; kind: ScaleKind; worst: number; best: number };

export type FixedMode = {
  ranking: RankingRow[];
  winner: string | null;
  scales: ResolvedScale[];
  influence: Record<string, number>;
  utilities: Record<string, Record<string, number>>;
  verdict: Verdict;
  verdict_text: string;
  winner_local_share?: number;
  winner_noise_share?: number | null;
  local: Record<string, number>;
  noise: Record<string, number> | null;
  intervals: Interval[];
  explanation?: string[];
};
