import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as endpoints from "../../api/endpoints";
import type { DecisionAnalysis } from "../../types/decision";
import demoAnalysis from "./__fixtures__/demo-analysis.json";
import { DecisionLab } from "./DecisionLab";
import { DEMO_MATRIX, PROFILES, applyProfile, buildBody, fromImported } from "./useDecisionLab";

/* Экран выбора проверяется по тому, что уходит в расчёт и что показывается в ответ:
   веса и ограничения обязаны доходить до backend без искажений, а отсутствие
   измерения не должно превращаться в ноль на стороне интерфейса. Фикстура —
   настоящий ответ backend на демо-набор. */

const ANALYSIS = demoAnalysis as unknown as DecisionAnalysis;

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useDecisionLab helpers", () => {
  it("профиль не даёт двум метрикам качества полный вес", () => {
    const state = fromImported({
      ...DEMO_MATRIX,
      criteria: [...DEMO_MATRIX.criteria, { key: "f1", label: "F1", direction: "max", weight: 1, unit: "", group: "quality" }],
    });
    const weighted = applyProfile(state.criteria, PROFILES[0]!, "roc_auc");
    expect(weighted.find((c) => c.key === "roc_auc")!.weight).toBe(4);
    expect(weighted.find((c) => c.key === "f1")!.weight).toBe(0);
  });

  it("выключенные критерии и модели не уходят в расчёт, null остаётся null", () => {
    const state = fromImported(DEMO_MATRIX);
    state.criteria[2]!.enabled = false;
    state.alternatives[0]!.enabled = false;
    state.alternatives[1]!.values.inference_ms = { value: null, std: null, note: "" };
    const body = buildBody(state)!;
    expect(body.criteria.map((c) => c.key)).toEqual(["roc_auc", "inference_ms"]);
    expect(body.alternatives).toHaveLength(4);
    expect(Object.keys(body.alternatives[0]!.values)).toEqual(["roc_auc", "inference_ms"]);
    expect(body.alternatives[0]!.values.inference_ms!.value).toBeNull();
  });

  it("без положительного веса расчёт не запускается", () => {
    const state = fromImported(DEMO_MATRIX);
    state.criteria = state.criteria.map((c) => ({ ...c, weight: 0 }));
    expect(buildBody(state)).toBeNull();
  });
});

describe("DecisionLab", () => {
  it("демо-набор считается, показывает рекомендацию и пересчитывает при смене веса", async () => {
    const evaluate = vi.spyOn(endpoints, "evaluateDecision").mockResolvedValue(ANALYSIS);
    render(<DecisionLab />);

    fireEvent.click(screen.getByRole("button", { name: "Демо-набор" }));
    expect(screen.getByText(/числа вымышлены/)).toBeInTheDocument();

    await waitFor(() => expect(evaluate).toHaveBeenCalledTimes(1));
    const body = evaluate.mock.calls[0]![0];
    expect(body.criteria.map((c) => [c.key, c.weight])).toEqual([
      ["roc_auc", 4],
      ["inference_ms", 2],
      ["model_mb", 2],
    ]);
    expect(await screen.findByTestId("robust-winner")).toHaveTextContent("Дерево решений");
    //#подробности спрятаны во вкладки: предупреждение о влиянии — во вкладке «Почему так»
    fireEvent.click(screen.getByRole("tab", { name: /Почему так/ }));
    expect(screen.getByText(/фактическое влияние на выбор/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Вес «ROC-AUC» числом"), { target: { value: "9" } });
    await waitFor(() => expect(evaluate).toHaveBeenCalledTimes(2));
    expect(evaluate.mock.calls[1]![0].criteria[0]!.weight).toBe(9);
  });

  it("переключатель показывает рекомендацию режима D", async () => {
    vi.spyOn(endpoints, "evaluateDecision").mockResolvedValue(ANALYSIS);
    render(<DecisionLab />);
    fireEvent.click(screen.getByRole("button", { name: "Демо-набор" }));
    const winner = await screen.findByTestId("robust-winner");
    expect(winner).toHaveTextContent("Дерево решений");
    fireEvent.click(screen.getByRole("radio", { name: "Режим D" }));
    const fixedWinner = ANALYSIS.alternatives.find((alt) => alt.key === ANALYSIS.modes.fixed!.winner)!.label;
    expect(await screen.findByTestId("robust-winner")).toHaveTextContent(fixedWinner);
    expect(screen.getByText("Фиксированные шкалы")).toBeInTheDocument();
  });

  it("ограничение уходит в расчёт", async () => {
    const evaluate = vi.spyOn(endpoints, "evaluateDecision").mockResolvedValue(ANALYSIS);
    render(<DecisionLab />);
    fireEvent.click(screen.getByRole("button", { name: "Демо-набор" }));
    await waitFor(() => expect(evaluate).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: /Добавить ограничение/ }));
    await waitFor(() => expect(evaluate).toHaveBeenCalledTimes(2));
    const constraint = evaluate.mock.calls[1]![0].constraints[0]!;
    expect(constraint.criterion).toBe("inference_ms");
    expect(constraint.op).toBe("<=");
  });

  it("ошибка backend показывается, а не проглатывается", async () => {
    vi.spyOn(endpoints, "evaluateDecision").mockRejectedValue(new Error("сбой"));
    render(<DecisionLab />);
    fireEvent.click(screen.getByRole("button", { name: "Демо-набор" }));
    expect(await screen.findByText("Расчёт не выполнен")).toBeInTheDocument();
  });

  it("пустой экран объясняет, что загрузить", () => {
    render(<DecisionLab />);
    expect(screen.getByText(/Сравните модели по нескольким критериям/)).toBeInTheDocument();
  });
});
