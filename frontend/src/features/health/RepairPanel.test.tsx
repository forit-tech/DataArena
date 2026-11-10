import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as endpoints from "../../api/endpoints";
import type { RepairAdvice, RepairPreview } from "../../types/health";
import { RepairPanel } from "./RepairPanel";

/* Исправление идёт строго по порядку: совет → предпросмотр → применение с отпечатком
   того состояния, на котором строился предпросмотр. */

const ADVICE: RepairAdvice = {
  finding_id: "f1",
  code: "whitespace_anomaly",
  columns: ["менеджер"],
  explanation: { cause: "Пробелы по краям.", consequence: "Значения дробятся.", approach: "Убрать пробелы." },
  options: [
    {
      option_id: "trim",
      title: "Убрать пробелы по краям",
      safety: "safe",
      note: "",
      steps: [{ kind: "trim_whitespace", params: { columns: ["менеджер"] }, description: "Убрать пробелы по краям в: менеджер" }],
    },
  ],
  manual_reason: "",
  steps_fingerprint: "abc",
  source: "rules",
};

const PREVIEW: RepairPreview = {
  finding_id: "f1",
  option: ADVICE.options[0]!,
  steps_fingerprint: "abc",
  rows_before: 10,
  rows_after: 10,
  rows_removed: 0,
  columns_before: 3,
  columns_after: 3,
  columns_removed: [],
  values_lost: 0,
  changed_rows: 2,
  samples: [{ column: "менеджер", before: "Петров ", after: "Петров", rows: 2 }],
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RepairPanel", () => {
  it("показывает объяснение, предпросмотр и применяет с отпечатком предпросмотра", async () => {
    vi.spyOn(endpoints, "fetchRepair").mockResolvedValue(ADVICE);
    vi.spyOn(endpoints, "previewRepair").mockResolvedValue(PREVIEW);
    const apply = vi.spyOn(endpoints, "applyRepair").mockResolvedValue({ applied: [], option: ADVICE.options[0]! });
    const onApplied = vi.fn();

    render(<RepairPanel workspaceId="ws" datasetId="ds" findingId="f1" onApplied={onApplied} onSkip={() => {}} />);

    expect(await screen.findByText("Пробелы по краям.")).toBeInTheDocument();
    expect(screen.getByText("Ничего не теряется")).toBeInTheDocument();
    expect(await screen.findByTestId("repair-preview")).toHaveTextContent("2 строки изменятся");
    expect(screen.getByText("«Петров »")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Исправить/ }));

    await waitFor(() => expect(apply).toHaveBeenCalledWith("ws", "ds", "f1", "trim", "abc"));
    expect(onApplied).toHaveBeenCalled();
  });

  it("без безопасного варианта объясняет, что решать вручную", async () => {
    vi.spyOn(endpoints, "fetchRepair").mockResolvedValue({
      ...ADVICE,
      options: [],
      manual_reason: "Безопасного автоматического исправления нет.",
    });

    render(<RepairPanel workspaceId="ws" datasetId="ds" findingId="f1" onApplied={() => {}} onSkip={() => {}} />);

    expect(await screen.findByText(/Безопасного автоматического исправления нет/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Исправить/ })).not.toBeInTheDocument();
  });
});
