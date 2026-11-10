//#ответы маршрутов /api/ai/* и советов локальной модели

export type AiStatus = {
  enabled: boolean;
  provider: string;
  base_url: string;
  model: string | null;
  reachable: boolean;
  models: string[];
  error: string | null;
  privacy: string;
};

export type AiFindingAdvice = {
  summary: string;
  recommended_option: string;
  reasoning: string;
  risks: string[];
  confidence: "low" | "medium" | "high";
  rejected: string[];
  model: string;
  seconds: number;
  tokens_per_second: number | null;
  source: "ai";
};

export type AiAssemblyAdvice = {
  summary: string;
  recommendation: "join" | "aggregate" | "keep_separate" | "review";
  aggregate_table: "left" | "right" | "none";
  group_by: string[];
  measures: string[];
  ml_target: string | null;
  risks: string[];
  rejected: string[];
  rules_verdict: string;
  model: string;
  seconds: number;
  tokens_per_second: number | null;
  source: "ai";
};
