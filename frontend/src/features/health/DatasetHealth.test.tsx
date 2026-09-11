import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import * as endpoints from "../../api/endpoints";
import type { Dataset } from "../../types/dataset";
import type { Finding, HealthReport } from "../../types/health";
import { DatasetHealth } from "./DatasetHealth";

const DATASET: Dataset = {
  dataset_id: "ds_0123456789abcdef",
  workspace_id: "ws_0123456789abcdef",
  name: "продажи.csv",
  alias: "продажи",
  source: { file_name: "продажи.csv", format: "csv", bytes: 2048, sha256: "a".repeat(64) },
  schema: {
    columns: [
      {
        name: "сумма",
        position: 0,
        physical_type: "Float64",
        logical_type: "float",
        semantic_type: "numeric",
        nullable: true,
      },
    ],
  },
  row_count: 200,
  column_count: 1,
  status: "ready",
  status_reason: null,
  original_format: "csv",
  working_format: "csv",
  normalized: false,
  conversion: null,
  created_at: "2026-01-01T00:00:00+00:00",
};

function makeFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    finding_id: "fnd_0000000000000001",
    code: "missing_values",
    severity: "warning",
    scope: "column",
    columns: ["сумма"],
    title: "Пропуски в колонке «сумма»",
    explanation:
      "127 строк из 200 не имеют значения. Подсчёты по этой колонке будут считаться по меньшему числу строк, чем кажется.",
    exactness: "exact",
    affected_rows: 127,
    affected_ratio: 0.635,
    sampled_rows: null,
    evidence: { values: [], counts: { nulls: 127 } },
    suggested_action: {
      kind: "fill_missing",
      columns: ["сумма"],
      hint: "Заполнить значением по умолчанию или отбросить эти строки.",
    },
    has_affected_rows: true,
    ...overrides,
  };
}

function makeReport(findings: Finding[]): HealthReport {
  return {
    dataset_id: DATASET.dataset_id,
    artifact_fingerprint: "f".repeat(64),
    checks_version: 1,
    computed_at: "2026-01-01T10:00:00+00:00",
    total_rows: 200,
    summary: {
      problem: findings.filter((item) => item.severity === "problem").length,
      warning: findings.filter((item) => item.severity === "warning").length,
      notice: findings.filter((item) => item.severity === "notice").length,
    },
    findings,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("диагностика датасета", () => {
  it("показывает находку с масштабом и объяснением", async () => {
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(makeReport([makeFinding()]));
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    expect(await screen.findByText("Пропуски в колонке «сумма»")).toBeInTheDocument();
    //#масштаб показан и числом, и долей: «127 строк» без доли не говорит, много это или мало
    expect(screen.getByText(/127 строк · 63,5%/)).toBeInTheDocument();
    expect(screen.getByText(/будут считаться по меньшему числу строк/)).toBeInTheDocument();
    expect(screen.getByText("сумма")).toBeInTheDocument();
  });

  it("не показывает сводной оценки качества", async () => {
    //#одно число скрывает, какой именно дефект важен, и подталкивает улучшать число
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(makeReport([makeFinding()]));
    const { container } = render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    await screen.findByText("Пропуски в колонке «сумма»");

    expect(container.textContent).not.toMatch(/\/\s*100|балл|оценка качества|score/i);
  });

  it("«Показать строки» передаёт наверх именно ту находку", async () => {
    const finding = makeFinding();
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(makeReport([finding]));
    const onShowRows = vi.fn();
    const user = userEvent.setup();
    render(<DatasetHealth dataset={DATASET} onShowRows={onShowRows} />);

    await user.click(await screen.findByRole("button", { name: /Показать строки/ }));

    expect(onShowRows).toHaveBeenCalledWith(finding);
  });

  it("у находки на весь датасет кнопки строк нет", async () => {
    //#«показать все строки» — это просто открыть датасет
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(
      makeReport([
        makeFinding({
          code: "all_null_column",
          severity: "problem",
          title: "Колонка «пусто» пуста целиком",
          has_affected_rows: false,
        }),
      ]),
    );
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    await screen.findByText("Колонка «пусто» пуста целиком");

    expect(screen.queryByRole("button", { name: /Показать строки/ })).not.toBeInTheDocument();
  });

  it("выборочная оценка подписана словами, а не значком", async () => {
    //#выборочное число, принятое за точное, приводит к неверному решению по данным
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(
      makeReport([makeFinding({ exactness: "sampled", sampled_rows: 10000 })]),
    );
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    expect(await screen.findByText(/оценка по выборке/)).toBeInTheDocument();
  });

  it("точный подсчёт тоже подписан", async () => {
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(makeReport([makeFinding()]));
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    expect(await screen.findByText(/точный подсчёт/)).toBeInTheDocument();
  });

  it("чистый датасет объясняет, что проверки прошли", async () => {
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(makeReport([]));
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    expect(await screen.findByText("Проверки ничего не нашли")).toBeInTheDocument();
    expect(screen.getByText(/не обнаружено/)).toBeInTheDocument();
  });

  it("во время расчёта не показывает прежних находок", async () => {
    /* Старый отчёт под видом нового — это показ находок, относящихся к другим данным.
       И никакой полосы с процентами: за ней не стояло бы ничего измеримого. */
    let release: ((value: HealthReport) => void) | undefined;
    vi.spyOn(endpoints, "fetchDatasetHealth").mockReturnValue(
      new Promise<HealthReport>((resolve) => {
        release = resolve;
      }),
    );
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    expect(await screen.findByText("Считаем диагностику")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();

    release?.(makeReport([makeFinding()]));

    expect(await screen.findByText("Пропуски в колонке «сумма»")).toBeInTheDocument();
  });

  it("ошибка объясняется и предлагает повторить", async () => {
    const fetch = vi.spyOn(endpoints, "fetchDatasetHealth");
    fetch.mockRejectedValueOnce(
      new ApiError(409, {
        code: "dataset_unreadable",
        message: "Датасет не читается движком.",
        details: {},
      }),
    );
    fetch.mockResolvedValueOnce(makeReport([makeFinding()]));
    const user = userEvent.setup();
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    expect(await screen.findByText("Датасет не читается движком.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Повторить/ }));

    expect(await screen.findByText("Пропуски в колонке «сумма»")).toBeInTheDocument();
  });

  it("уровни находок показаны счётчиками, а не одним числом", async () => {
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(
      makeReport([
        makeFinding({ finding_id: "fnd_1", severity: "problem", code: "all_null_column" }),
        makeFinding({ finding_id: "fnd_2", severity: "warning" }),
        makeFinding({ finding_id: "fnd_3", severity: "notice", code: "outlier_values" }),
      ]),
    );
    render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    await waitFor(() => expect(screen.getByText("Дефект")).toBeInTheDocument());
    expect(screen.getByText("Предупреждение")).toBeInTheDocument();
    //#«сигнал», а не «замечание»: слово не должно подталкивать это «чинить»
    expect(screen.getByText("Сигнал")).toBeInTheDocument();
  });

  it("значение из данных не превращается в разметку", async () => {
    //#примеры значений приходят из файла пользователя и остаются текстом
    vi.spyOn(endpoints, "fetchDatasetHealth").mockResolvedValue(
      makeReport([
        makeFinding({
          evidence: { values: ["<script>alert(1)</script>"], counts: {} },
        }),
      ]),
    );
    const { container } = render(<DatasetHealth dataset={DATASET} onShowRows={() => {}} />);

    await screen.findByText(/alert\(1\)/);

    expect(container.querySelector("script")).toBeNull();
  });
});
