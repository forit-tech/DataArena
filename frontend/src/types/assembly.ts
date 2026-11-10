//#ответ GET /joins/discover: связи между датасетами, назначение и кандидаты в цель

export type RelationVerdict = "safe" | "weak" | "dangerous" | "none";
export type PairVerdict = "join" | "aggregate" | "review" | "none";

export type RelationCandidate = {
  keys: Array<{ left: string; right: string }>;
  cardinality: string;
  estimated_rows: number;
  left_rows: number;
  right_rows: number;
  matched_left_rows: number;
  matched_right_rows: number;
  match_share: number;
  verdict: RelationVerdict;
  explanation: string;
  reasons: string[];
  confidence: number | null;
};

export type AssemblyPair = {
  left_dataset_id: string;
  right_dataset_id: string;
  left_name: string;
  right_name: string;
  verdict: PairVerdict;
  advice: string;
  candidates: RelationCandidate[];
};

export type TargetCandidate = {
  column: string;
  task: "regression" | "classification";
  classes: number | null;
  minority_share: number | null;
  null_ratio: number;
  name_hint: boolean;
  leakage_suspects: Array<{ column: string; reason: string }>;
  warnings: string[];
};

export type AssemblyDataset = {
  dataset_id: string;
  name: string;
  rows: number;
  columns: Array<{ name: string; dtype: string; null_ratio: number; unique: number; unique_ratio: number; role: string }>;
  purposes: Array<{ purpose: string; title: string; reason: string }>;
  targets: TargetCandidate[];
};

export type AssemblyDiscovery = {
  datasets: AssemblyDataset[];
  pairs: AssemblyPair[];
  note: string;
};
