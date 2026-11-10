import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeError } from "../../api/client";
import { evaluateDecision } from "../../api/endpoints";
import type { EvaluateBody } from "../../api/endpoints";
import type {
  AlternativeDraft,
  ConstraintDraft,
  CriterionDraft,
  DecisionAnalysis,
  ImportedMatrix,
  Normalization,
  ScaleDraft,
  SensitivityDraft,
} from "../../types/decision";

//#пересчёт идёт сам после каждой правки, но не на каждый шаг ползунка: 250 мс достаточно,
//#чтобы перетаскивание не порождало десятки запросов, и мало, чтобы казаться мгновенным
const DEBOUNCE_MS = 250;
const STORAGE_KEY = "dataarena.decision-lab.v1";

export const DEFAULT_SENSITIVITY: SensitivityDraft = {
  samples: 2000,
  seed: 20261109,
  concentration: 50,
  grid_step: 0.01,
  stability_threshold: 0.6,
};

export type Profile = { id: string; label: string; hint: string; weights: Record<string, number> };

//#профиль раздаёт веса по смысловым группам критериев. Метрика качества получает вес только
//#основная: две метрики качества с полным весом учли бы качество дважды
export const PROFILES: Profile[] = [
  {
    id: "balanced",
    label: "Баланс",
    hint: "Качество важнее всего, но скорость и размер тоже учитываются",
    weights: { quality: 4, time: 2, size: 2, memory: 1, cost: 1, stability: 1, other: 1 },
  },
  {
    id: "quality",
    label: "Качество прежде всего",
    hint: "Ресурсы почти не важны",
    weights: { quality: 8, time: 0.5, size: 0.5, memory: 0.5, cost: 0.5, stability: 1, other: 0.5 },
  },
  {
    id: "edge",
    label: "Слабое железо",
    hint: "Модель должна быть маленькой и быстрой",
    weights: { quality: 3, time: 3, size: 3, memory: 2, cost: 1, stability: 0.5, other: 1 },
  },
  {
    id: "latency",
    label: "Скорость ответа",
    hint: "Главное — время инференса",
    weights: { quality: 3, time: 6, size: 1, memory: 1, cost: 1, stability: 0.5, other: 0.5 },
  },
];

//#демонстрационный набор: числа вымышлены, и интерфейс это говорит прямо
export const DEMO_MATRIX: ImportedMatrix = {
  source: "demo",
  primary: "roc_auc",
  context: {},
  notes: ["Демонстрационные данные: числа вымышлены и не являются результатом эксперимента."],
  criteria: [
    { key: "roc_auc", label: "ROC-AUC", direction: "max", weight: 1, unit: "", group: "quality" },
    { key: "inference_ms", label: "Инференс", direction: "min", weight: 1, unit: "мс", group: "time" },
    { key: "model_mb", label: "Размер модели", direction: "min", weight: 1, unit: "МБ", group: "size" },
  ],
  alternatives: [
    { key: "a", label: "Градиентный бустинг", meta: {}, values: v(0.91, 0.008, 45, 120) },
    { key: "b", label: "Логистическая регрессия", meta: {}, values: v(0.89, 0.01, 12, 35) },
    { key: "c", label: "Большой ансамбль", meta: {}, values: v(0.93, 0.007, 110, 280) },
    { key: "d", label: "Случайный лес", meta: {}, values: v(0.9, 0.012, 60, 210) },
    { key: "e", label: "Дерево решений", meta: {}, values: v(0.84, 0.02, 3, 2) },
  ],
};

function v(auc: number, std: number, ms: number, mb: number) {
  return {
    roc_auc: { value: auc, std, note: "" },
    inference_ms: { value: ms, std: null, note: "" },
    model_mb: { value: mb, std: null, note: "" },
  };
}

export type LabState = {
  source: ImportedMatrix["source"] | null;
  notes: string[];
  context: Record<string, unknown>;
  criteria: CriterionDraft[];
  alternatives: AlternativeDraft[];
  constraints: ConstraintDraft[];
  primary: string | null;
  normalization: Normalization;
  sensitivity: SensitivityDraft;
  //#шкалы режима D, заданные пользователем; остальные критерии получают шкалу по умолчанию на backend
  scales: ScaleDraft[];
};

const EMPTY: LabState = {
  source: null,
  notes: [],
  context: {},
  criteria: [],
  alternatives: [],
  constraints: [],
  primary: null,
  normalization: "vector",
  sensitivity: DEFAULT_SENSITIVITY,
  scales: [],
};

function readStored(): LabState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as LabState;
    return parsed && Array.isArray(parsed.criteria) && Array.isArray(parsed.alternatives) ? { ...EMPTY, ...parsed } : EMPTY;
  } catch {
    return EMPTY;
  }
}

export function fromImported(matrix: ImportedMatrix): LabState {
  return {
    ...EMPTY,
    source: matrix.source,
    notes: matrix.notes,
    context: matrix.context,
    primary: matrix.primary,
    criteria: matrix.criteria.map((item) => ({ ...item, unit: item.unit ?? "", group: item.group ?? "", enabled: true })),
    alternatives: matrix.alternatives.map((item) => ({ ...item, meta: item.meta ?? {}, enabled: true })),
  };
}

export function applyProfile(criteria: CriterionDraft[], profile: Profile, primary: string | null): CriterionDraft[] {
  const primaryKey = primary ?? criteria.find((item) => item.group === "quality")?.key ?? null;
  return criteria.map((item) => {
    if (item.group === "quality" && item.key !== primaryKey) return { ...item, weight: 0 };
    const weight = profile.weights[item.group || "other"] ?? profile.weights.other ?? 1;
    return { ...item, weight };
  });
}

export function buildBody(state: LabState): EvaluateBody | null {
  const criteria = state.criteria.filter((item) => item.enabled);
  const keys = new Set(criteria.map((item) => item.key));
  const alternatives = state.alternatives.filter((item) => item.enabled);
  if (criteria.length === 0 || alternatives.length < 2 || !criteria.some((item) => item.weight > 0)) return null;
  return {
    criteria: criteria.map(({ key, label, direction, weight, unit, group }) => ({ key, label, direction, weight, unit, group })),
    alternatives: alternatives.map((item) => ({
      key: item.key,
      label: item.label,
      meta: item.meta,
      values: Object.fromEntries(Object.entries(item.values).filter(([key]) => keys.has(key))),
    })),
    constraints: state.constraints.filter((item) => keys.has(item.criterion) && Number.isFinite(item.bound)),
    normalization: state.normalization,
    primary: state.primary && keys.has(state.primary) ? state.primary : null,
    sensitivity: state.sensitivity,
    scales: (state.scales ?? []).filter(
      (item) => keys.has(item.criterion) && Number.isFinite(item.worst) && Number.isFinite(item.best),
    ),
  };
}

export function whyNotReady(state: LabState): string {
  if (state.criteria.length === 0) return "Загрузите характеристики моделей, чтобы начать сравнение.";
  if (!state.criteria.some((item) => item.enabled)) return "Включите хотя бы один критерий.";
  if (!state.criteria.some((item) => item.enabled && item.weight > 0))
    return "У всех включённых критериев вес 0 — оптимизировать нечего. Дайте вес хотя бы одному.";
  return "Для выбора нужно хотя бы две включённые модели.";
}

export function useDecisionLab() {
  const [state, setState] = useState<LabState>(readStored);
  const [analysis, setAnalysis] = useState<DecisionAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const requestId = useRef(0);
  const body = useMemo(() => buildBody(state), [state]);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      //#хранилище недоступно (приватное окно): черновик просто не переживёт перезагрузку
    }
  }, [state]);

  useEffect(() => {
    if (!body) {
      requestId.current += 1;
      setAnalysis(null);
      setError(null);
      setLoading(false);
      return;
    }
    const id = ++requestId.current;
    setLoading(true);
    const timer = window.setTimeout(() => {
      evaluateDecision(body)
        .then((result) => {
          //#ответ на устаревшую версию матрицы не должен перезаписать свежий
          if (id !== requestId.current) return;
          setAnalysis(result);
          setError(null);
        })
        .catch((reason: unknown) => {
          if (id !== requestId.current) return;
          setError(describeError(reason, "Не удалось выполнить расчёт."));
        })
        .finally(() => {
          if (id === requestId.current) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [body]);

  const update = useCallback((patch: Partial<LabState> | ((prev: LabState) => Partial<LabState>)) => {
    setState((prev) => ({ ...prev, ...(typeof patch === "function" ? patch(prev) : patch) }));
  }, []);

  const load = useCallback((matrix: ImportedMatrix) => {
    const next = fromImported(matrix);
    //#после загрузки сразу действует профиль «Баланс»: иначе ресурсные критерии пришли бы с весом 0
    setState({ ...next, criteria: applyProfile(next.criteria, PROFILES[0]!, next.primary) });
    setAnalysis(null);
  }, []);

  const reset = useCallback(() => {
    setState(EMPTY);
    setAnalysis(null);
    setError(null);
  }, []);

  return { state, update, load, reset, analysis, error, loading, body };
}
