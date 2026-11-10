import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as endpoints from "../../api/endpoints";
import type { ColumnHistory, ColumnLineageEvent, Dataset, LineageView } from "../../types/dataset";
import { DatasetLineage } from "./DatasetLineage";

/* Раздел отвечает на вопрос «откуда это взялось». Ответ, который выглядит уверенно,
   но неверен, хуже отсутствия ответа: по нему принимают решения о данных. Поэтому
   проверяется не отрисовка таблицы, а то, что «неизвестно» остаётся «неизвестно»,
   что колонка называет ту сторону соединения, которая её дала, и что её история
   читается сверху вниз в том порядке, в каком всё происходило. */

const DATASET: Dataset = {
  dataset_id: "ds_0123456789abcdef",
  workspace_id: "ws_0123456789abcdef",
  name: "customer_orders.parquet",
  alias: "customer_orders",
  source: {
    file_name: "customer_orders.parquet",
    format: "parquet",
    bytes: 2048,
    sha256: "a".repeat(64),
  },
  schema: { columns: [] },
  row_count: 10,
  column_count: 2,
  status: "ready",
  status_reason: null,
  original_format: "parquet",
  working_format: "parquet",
  normalized: false,
  conversion: null,
  created_at: "2026-01-01T00:00:00+00:00",
};

function step(
  dataset: string,
  position: number,
  operation: string,
  description: string,
): ColumnLineageEvent {
  return {
    kind: "step",
    dataset_id: `ds_${dataset}`,
    dataset_name: dataset,
    position,
    step_id: `s${position}`,
    operation,
    description,
    inputs: ["email"],
    output: "email",
  };
}

const EMAIL: ColumnHistory = {
  name: "email",
  dtype: "String",
  origin: "source",
  origins: [
    {
      dataset_id: "ds_customers",
      dataset_name: "customers",
      column: "email",
      source_file: "customers.csv",
      source_sha256: "c".repeat(64),
    },
  ],
  events: [
    step("customers", 1, "trim_whitespace", "Убрать пробелы по краям в: email"),
    step("customers", 2, "fill_missing", "Заполнить пропуски в «email»"),
    {
      ...step("customer_orders.parquet", 0, "join", "LEFT JOIN: «customers».email → email"),
      kind: "join",
    },
  ],
  read_by: [1],
  source_name: "email",
};

function view(overrides: Partial<LineageView> = {}): LineageView {
  return {
    dataset_id: DATASET.dataset_id,
    name: DATASET.name,
    status: "known",
    unknown_reason: null,
    origin: "join",
    artifact: { file_name: DATASET.name, format: "parquet", sha256: "b".repeat(64), bytes: 10 },
    join: {
      kind: "left",
      keys: [{ left: "id", right: "customer_id" }],
      right_suffix: "_right",
      columns: [],
    },
    parents: [
      {
        role: "left",
        dataset_id: "ds_customers",
        dataset_name: "customers",
        snapshot: null,
        current: { available: true, changed_since_join: false },
      },
      {
        role: "right",
        dataset_id: "ds_orders",
        dataset_name: "orders",
        snapshot: null,
        current: { available: false, changed_since_join: null },
      },
    ],
    events: [
      {
        position: 1,
        step_id: "s1",
        operation: "sort_rows",
        description: "Отсортировать по «amount»",
        params: { column: "amount" },
        changes: [],
        reads: ["amount"],
      },
    ],
    columns: [EMAIL],
    warnings: [],
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Происхождение", () => {
  it("показывает соединение, роли родителей и составной ключ", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockResolvedValue(
      view({
        join: {
          kind: "left",
          keys: [
            { left: "id", right: "customer_id" },
            { left: "region", right: "region" },
          ],
          right_suffix: "_right",
          columns: [],
        },
      }),
    );

    render(<DatasetLineage dataset={DATASET} />);

    await waitFor(() => expect(screen.getByText("LEFT JOIN")).toBeInTheDocument());

    expect(screen.getByText("LEFT")).toBeInTheDocument();
    expect(screen.getByText("RIGHT")).toBeInTheDocument();
    expect(
      screen.getByText(/customers\.id = orders\.customer_id AND customers\.region = orders\.region/),
    ).toBeInTheDocument();
    //пропавший родитель назван, а не исчез из списка
    expect(screen.getByText("датасета больше нет")).toBeInTheDocument();
  });

  it("раскрывает историю колонки сверху вниз, от файла к результату", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockResolvedValue(view());

    render(<DatasetLineage dataset={DATASET} />);

    fireEvent.click(await screen.findByRole("button", { name: "email" }));

    const chain = screen.getByLabelText("История колонки email");
    const links = within(chain)
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");

    expect(links[0]).toContain("customers.csv · email");
    expect(links[1]).toContain("Убрать пробелы");
    expect(links[2]).toContain("Заполнить пропуски");
    expect(links[3]).toContain("LEFT JOIN");
    expect(links[4]).toContain("customer_orders.parquet.email");
    //сортировка читала колонку, но не меняла её — и названа отдельно
    expect(within(chain).getByText(/Только читали/)).toBeInTheDocument();
  });

  it("не превращает неизвестное происхождение колонки в источник", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockResolvedValue(
      view({
        columns: [{ ...EMAIL, name: "сумма", origin: "unknown", origins: [], events: [] }],
      }),
    );

    render(<DatasetLineage dataset={DATASET} />);

    await waitFor(() => expect(screen.getByText("неизвестно")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "сумма" }));

    expect(screen.getByText(/Откуда пришла колонка, неизвестно/)).toBeInTheDocument();
    expect(screen.queryByText(/customers\.csv/)).not.toBeInTheDocument();
  });

  it("честно говорит, что происхождение датасета недоступно", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockResolvedValue(
      view({
        status: "legacy_unknown",
        unknown_reason: "corrupted_record",
        origin: null,
        join: null,
        parents: [],
        warnings: ["provenance_unknown"],
      }),
    );

    render(<DatasetLineage dataset={DATASET} />);

    await waitFor(() =>
      expect(screen.getByText("Происхождение недоступно")).toBeInTheDocument(),
    );

    expect(screen.getByText(/повреждена/)).toBeInTheDocument();
    //«Загружен файлом» здесь было бы выдумкой: запись повреждена, а не сказано «загрузка»
    expect(screen.queryByText(/Загружен файлом/)).not.toBeInTheDocument();
  });

  it("предупреждает, что родителя изменили после сборки", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockResolvedValue(
      view({
        parents: [
          {
            role: "left",
            dataset_id: "ds_customers",
            dataset_name: "customers",
            snapshot: null,
            current: { available: true, changed_since_join: true },
          },
        ],
        warnings: ["join_parent_snapshots_missing"],
      }),
    );

    render(<DatasetLineage dataset={DATASET} />);

    await waitFor(() => expect(screen.getByText(/изменён после сборки/)).toBeInTheDocument());

    expect(screen.getByText(/снимков родителей/)).toBeInTheDocument();
  });

  it("пустой конвейер — ответ, а не ошибка", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockResolvedValue(
      view({ origin: "upload", join: null, parents: [], events: [], columns: [] }),
    );

    render(<DatasetLineage dataset={DATASET} />);

    await waitFor(() => expect(screen.getByText(/Шагов нет/)).toBeInTheDocument());

    expect(screen.getByText(/Загружен файлом/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("ошибка загрузки показывается как ошибка", async () => {
    vi.spyOn(endpoints, "fetchLineage").mockRejectedValue(new Error("сеть"));

    render(<DatasetLineage dataset={DATASET} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
