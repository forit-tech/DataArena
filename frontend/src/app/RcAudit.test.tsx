import { fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getBlob, getJson } from "../api/client";
import * as endpoints from "../api/endpoints";
import { DatasetBuilder } from "../features/builder/DatasetBuilder";
import { DatasetTable } from "../features/dataset-table/DatasetTable";
import { ExportPanel } from "../features/pipeline/ExportPanel";
import { WorkspacePanel } from "../features/workspace/WorkspacePanel";
import { useWorkspaceId } from "../hooks/useWorkspaceId";
import type { Dataset } from "../types/dataset";

/* Регрессии RC-аудита интерфейса: состояния отказа, в которых человек прежде видел
   вечную загрузку, разметку чужой страницы, «успешно скачанный» файл с текстом ошибки
   или совет, не имеющий отношения к причине. */

const DATASET: Dataset = {
  dataset_id: "ds_0123456789abcdef",
  workspace_id: "ws_0123456789abcdef",
  name: "клиенты.csv",
  alias: "клиенты",
  source: { file_name: "клиенты.csv", format: "csv", bytes: 10, sha256: "a".repeat(64) },
  schema: { columns: [] },
  row_count: 3,
  column_count: 2,
  status: "ready",
  status_reason: null,
  original_format: "csv",
  working_format: "csv",
  normalized: false,
  conversion: null,
  created_at: "2026-01-01T00:00:00+00:00",
};

function respondWith(body: string, status = 200, headers: Record<string, string> = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body, { status, headers })),
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("транспорт", () => {
  it("HTML-страница ошибки не попадает в сообщение", async () => {
    respondWith("<!DOCTYPE html><html><body><h1>502 Bad Gateway</h1></body></html>", 502);

    const error = (await getJson("/x").catch((caught: unknown) => caught)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).not.toContain("<");
    expect(error.message).toContain("502");
  });

  it("успешный ответ не в JSON объясняется, а не «Unexpected token»", async () => {
    respondWith("<!doctype html><div id=root></div>", 200);

    const error = (await getJson("/x").catch((caught: unknown) => caught)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("unexpected_response");
  });

  it("файл приходит с именем от сервера", async () => {
    respondWith("a,b\n", 200, { "content-disposition": "attachment; filename*=utf-8''%D0%BA.csv" });

    const { fileName } = await getBlob("/api/x");

    expect(fileName).toBe("к.csv");
  });
});

describe("рабочее пространство", () => {
  it("несуществующий сохранённый идентификатор заменяется новым пространством", async () => {
    localStorage.setItem("DataArenaWorkspaceId", "ws_gone00000000000");
    vi.spyOn(endpoints, "fetchWorkspace").mockRejectedValue(
      new ApiError(404, { code: "workspace_not_found", message: "нет", details: {} }),
    );
    const created = vi
      .spyOn(endpoints, "createWorkspace")
      .mockResolvedValue({ workspace_id: "ws_new000000000000", name: "x", created_at: "" } as never);

    const { result } = renderHook(() => useWorkspaceId());

    await waitFor(() => expect(result.current.workspaceId).toBe("ws_new000000000000"));
    expect(created).toHaveBeenCalledOnce();
    expect(localStorage.getItem("DataArenaWorkspaceId")).toBe("ws_new000000000000");
  });

  it("сетевой сбой не стирает сохранённый идентификатор", async () => {
    localStorage.setItem("DataArenaWorkspaceId", "ws_kept00000000000");
    vi.spyOn(endpoints, "fetchWorkspace").mockRejectedValue(new TypeError("сеть"));

    const { result } = renderHook(() => useWorkspaceId());

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(localStorage.getItem("DataArenaWorkspaceId")).toBe("ws_kept00000000000");
  });

  it("неоткрывшееся пространство — ошибка с повтором, а не вечное «Открывается…»", () => {
    const retry = vi.fn();

    render(
      <WorkspacePanel
        workspaceId={null}
        workspaceError="Backend не отвечает."
        onRetryWorkspace={retry}
        onSelectDataset={() => undefined}
        selectedDatasetId={null}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Backend не отвечает.");
    expect(screen.queryByText(/Открывается/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Попробовать снова" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});

describe("выгрузка", () => {
  it("ошибка сервера показывается, а не сохраняется как файл", async () => {
    vi.spyOn(endpoints, "fetchWritableFormats").mockResolvedValue({
      formats: [
        {
          key: "csv",
          label: "CSV",
          extensions: [".csv"],
          can_write: true,
          preserves_schema: false,
          preserves_nulls: true,
          row_limit: null,
          warnings: [],
        },
      ],
    });
    respondWith(
      JSON.stringify({ error: { code: "package_failed", message: "В датасете нет строк.", details: {} } }),
      422,
    );
    const createObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });

    render(<ExportPanel dataset={DATASET} />);
    fireEvent.click(await screen.findByRole("button", { name: /Пакет для ModelArena/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("В датасете нет строк.");
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("несостоявшийся список форматов назван, а не проглочен", async () => {
    vi.spyOn(endpoints, "fetchWritableFormats").mockRejectedValue(new TypeError("сеть"));

    render(<ExportPanel dataset={DATASET} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Выгрузить/ })).toBeDisabled();
  });
});

describe("сборка", () => {
  it("несостоявшийся список датасетов — ошибка, а не совет загрузить второй файл", async () => {
    vi.spyOn(endpoints, "fetchDatasets").mockRejectedValue(
      new ApiError(500, { code: "internal_error", message: "Внутренняя ошибка.", details: {} }),
    );

    render(<DatasetBuilder workspaceId="ws_0123456789abcdef" onDatasetCreated={() => undefined} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Внутренняя ошибка.");
    expect(screen.queryByText(/Нужно минимум два датасета/)).not.toBeInTheDocument();
  });
});

describe("таблица результата конвейера", () => {
  it("колонки — из результата, а не из схемы исходника", async () => {
    //#прежде шапка строилась по исходнику: удалённая шагом колонка показывалась сплошными
    //#пропусками, а переименованная — не показывалась вовсе
    const dataset: Dataset = {
      ...DATASET,
      schema: {
        columns: [
          { name: "id", position: 0, physical_type: "Int64", logical_type: "integer", semantic_type: "numeric", nullable: false },
          { name: "удалённая", position: 1, physical_type: "String", logical_type: "string", semantic_type: "text", nullable: true },
        ],
      },
    };
    vi.spyOn(endpoints, "fetchRows").mockResolvedValue({
      columns: ["id", "новая"],
      rows: [{ id: 1, "новая": 2.5 }],
      row_ordinals: [0],
      offset: 0,
      limit: 100,
      total_rows: 1,
    } as never);

    render(<DatasetTable dataset={dataset} source="pipeline" resultTypes={{ id: "Int64", "новая": "Float64" }} />);

    expect(await screen.findByText("новая")).toBeInTheDocument();
    expect(screen.queryByText("удалённая")).not.toBeInTheDocument();
    //#показатели считаются по исходнику: для колонки результата кнопки нет
    expect(screen.queryByRole("button", { name: /Показатели колонки/ })).not.toBeInTheDocument();
  });
});
