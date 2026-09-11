import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Dataset } from "../../types/dataset";
import { DatasetTable } from "./DatasetTable";

const DATASET: Dataset = {
  dataset_id: "ds_0123456789abcdef",
  workspace_id: "ws_0123456789abcdef",
  alias: "cities",
  name: "cities.csv",
  source: { file_name: "cities.csv", format: "csv", bytes: 2048, sha256: "a".repeat(64) },
  schema: {
    columns: [
      {
        name: "id",
        position: 0,
        physical_type: "Int64",
        logical_type: "integer",
        semantic_type: "id",
        nullable: false,
      },
      {
        name: "город",
        position: 1,
        physical_type: "String",
        logical_type: "string",
        semantic_type: "categorical",
        nullable: true,
      },
      {
        name: "сумма",
        position: 2,
        physical_type: "Float64",
        logical_type: "float",
        semantic_type: "numeric",
        nullable: true,
      },
    ],
  },
  row_count: 3,
  column_count: 3,
  status: "ready",
  status_reason: null,
  original_format: "csv",
  working_format: "csv",
  normalized: false,
  conversion: null,
  created_at: "2026-09-07T10:00:00Z",
};

const PAGE = {
  columns: ["id", "город", "сумма"],
  rows: [
    { id: 1, город: "Москва", сумма: 100.5 },
    { id: 2, город: null, сумма: null },
    { id: 3, город: "Казань", сумма: -20 },
  ],
  offset: 0,
  limit: 200,
  total_rows: 3,
  total_is_exact: true,
};

function stubRows(handler: (url: string) => Response | Error | Promise<Response>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      //#заглушка обязана уважать отмену: иначе тест на устаревшие ответы ничего не проверяет
      if (init?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }

      const outcome = await handler(url);

      if (outcome instanceof Error) {
        throw outcome;
      }

      return outcome;
    }),
  );
}

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("таблица датасета", () => {
  it("показывает строки и число найденного", async () => {
    stubRows(() => json(PAGE));

    render(<DatasetTable dataset={DATASET} />);

    expect(await screen.findByText("Москва")).toBeInTheDocument();
    expect(screen.getByText("1–3 из 3")).toBeInTheDocument();
  });

  it("пропуск показывается явно и отличается от пустой строки", async () => {
    //#в данных это разные вещи, и пустая ячейка скрыла бы разницу
    stubRows(() => json(PAGE));

    render(<DatasetTable dataset={DATASET} />);

    await screen.findByText("Москва");
    expect(screen.getAllByText("null").length).toBeGreaterThan(0);
  });

  it("сортировка уходит на сервер, а не выполняется в браузере", async () => {
    //#в браузере лежит только одна страница: сортировать её локально означало бы
    //#отсортировать двести строк из миллиона и выдать это за результат
    const urls: string[] = [];
    stubRows((url) => {
      urls.push(url);
      return json(PAGE);
    });

    render(<DatasetTable dataset={DATASET} />);
    await screen.findByText("Москва");

    await userEvent.click(screen.getByRole("button", { name: /сумма/ }));

    await waitFor(() => {
      expect(urls.some((url) => url.includes("sort=%D1%81%D1%83%D0%BC%D0%BC%D0%B0%3Aasc"))).toBe(
        true,
      );
    });
  });

  it("поиск уходит на сервер целиком, а не фильтрует загруженную страницу", async () => {
    const urls: string[] = [];
    stubRows((url) => {
      urls.push(url);
      return json({ ...PAGE, rows: [], total_rows: 0 });
    });

    render(<DatasetTable dataset={DATASET} />);
    await screen.findByRole("searchbox");

    await userEvent.type(screen.getByRole("searchbox"), "Казань{Enter}");

    await waitFor(() => {
      expect(urls.some((url) => url.includes("search="))).toBe(true);
    });
  });

  it("устаревший ответ не перезаписывает более новый", async () => {
    //#медленный ответ на прежнюю сортировку не должен затирать уже показанный результат
    let call = 0;
    stubRows(async () => {
      call += 1;

      if (call === 1) {
        await new Promise((resolve) => setTimeout(resolve, 60));
        return json({ ...PAGE, rows: [{ id: 999, город: "УСТАРЕЛО", сумма: 0 }] });
      }

      return json(PAGE);
    });

    render(<DatasetTable dataset={DATASET} />);
    await userEvent.click(await screen.findByRole("button", { name: /сумма/ }));

    await waitFor(() => expect(screen.getByText("Москва")).toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 120));

    expect(screen.queryByText("УСТАРЕЛО")).not.toBeInTheDocument();
  });

  it("ошибка страницы показывается с возможностью повторить", async () => {
    stubRows(() =>
      json(
        {
          error: { code: "dataset_not_found", message: "Датасет не найден.", details: {} },
          detail: "Датасет не найден.",
        },
        404,
      ),
    );

    render(<DatasetTable dataset={DATASET} />);

    expect(await screen.findByText("Датасет не найден.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });

  it("пустой результат поиска отличается от пустого датасета", async () => {
    stubRows(() => json({ ...PAGE, rows: [], total_rows: 0 }));

    render(<DatasetTable dataset={DATASET} />);
    await screen.findByRole("searchbox");
    await userEvent.type(screen.getByRole("searchbox"), "нетничего{Enter}");

    expect(await screen.findByText(/Ни одна строка не подошла/)).toBeInTheDocument();
  });

  it("неточный подсчёт показывается как «более N», а не как точное число", async () => {
    //#выдавать срезанное число за точное значило бы соврать о размере данных
    stubRows(() => json({ ...PAGE, total_rows: 5_000_000, total_is_exact: false }));

    render(<DatasetTable dataset={DATASET} />);

    expect(await screen.findByText(/более/)).toBeInTheDocument();
  });

  it("кнопка «Вперёд» недоступна, когда следующей страницы нет", async () => {
    stubRows(() => json(PAGE));

    render(<DatasetTable dataset={DATASET} />);
    await screen.findByText("Москва");

    expect(screen.getByRole("button", { name: "Вперёд" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Назад" })).toBeDisabled();
  });

  it("в браузер не загружается весь датасет: запрос всегда с limit", async () => {
    const urls: string[] = [];
    stubRows((url) => {
      urls.push(url);
      return json(PAGE);
    });

    render(<DatasetTable dataset={DATASET} />);
    await screen.findByText("Москва");

    expect(urls[0]).toMatch(/limit=\d+/);
    expect(urls[0]).toMatch(/offset=0/);
  });
});
