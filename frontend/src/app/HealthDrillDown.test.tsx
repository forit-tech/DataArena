import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

/* Вертикальный путь целиком, через настоящую оболочку и настоящий fetch-слой:
   диагностика → карточка находки → «Показать строки» → запрос таблицы именно
   с этой находкой → возврат в диагностику без потери контекста.

   Именно этот путь и есть смысл этапа. Проверить его по частям недостаточно:
   разойтись они могут ровно на стыке. */

const SYSTEM_HEALTH = {
  status: "ok",
  version: "0.1.0",
  workspace_root: "D:/work/.dataarena/workspaces",
  max_upload_mb: 512,
  modelarena_configured: false,
  contract: {
    format: "dataarena.package",
    version: "1.0.0",
    fingerprint_algorithm: "dataarena-logical-sha256-v1",
  },
};

const WORKSPACE = {
  workspace_id: "ws_0123456789abcdef",
  name: "Рабочее пространство",
  created_at: "2026-09-07T10:00:00Z",
  dataset_count: 1,
};

const DATASET = {
  dataset_id: "ds_0123456789abcdef",
  workspace_id: WORKSPACE.workspace_id,
  name: "продажи.csv",
  alias: "продажи",
  source: { file_name: "продажи.csv", format: "csv", bytes: 4096, sha256: "a".repeat(64) },
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
        name: "сумма",
        position: 1,
        physical_type: "Float64",
        logical_type: "float",
        semantic_type: "numeric",
        nullable: true,
      },
    ],
  },
  row_count: 200,
  column_count: 2,
  status: "ready",
  status_reason: null,
  original_format: "csv",
  working_format: "csv",
  normalized: false,
  conversion: null,
  created_at: "2026-09-07T10:00:00Z",
};

const FINDING = {
  finding_id: "fnd_00000000000000aa",
  code: "missing_values",
  severity: "warning" as const,
  scope: "column" as const,
  columns: ["сумма"],
  title: "Пропуски в колонке «сумма»",
  explanation:
    "127 строк из 200 не имеют значения. Подсчёты по этой колонке будут считаться по меньшему числу строк, чем кажется.",
  exactness: "exact" as const,
  affected_rows: 127,
  affected_ratio: 0.635,
  sampled_rows: null,
  evidence: { values: [], counts: { nulls: 127 } },
  suggested_action: { kind: "fill_missing", columns: ["сумма"], hint: "Заполнить пропуски." },
  has_affected_rows: true,
};

const REPORT = {
  dataset_id: DATASET.dataset_id,
  artifact_fingerprint: "f".repeat(64),
  checks_version: 1,
  computed_at: "2026-09-07T11:00:00Z",
  total_rows: 200,
  summary: { problem: 0, warning: 1, notice: 0 },
  findings: [FINDING],
};

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function page(totalRows: number) {
  return {
    columns: ["id", "сумма"],
    rows: Array.from({ length: Math.min(totalRows, 3) }, (_, index) => ({
      id: index,
      сумма: null,
    })),
    offset: 0,
    limit: 200,
    total_rows: totalRows,
    total_is_exact: true,
  };
}

let requestedUrls: string[] = [];

beforeEach(() => {
  requestedUrls = [];
  localStorage.setItem("DataArenaWorkspaceId", WORKSPACE.workspace_id);

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requestedUrls.push(url);

      if (url.includes("/system/health")) {
        return jsonResponse(SYSTEM_HEALTH);
      }

      if (url.includes("/health")) {
        return jsonResponse(REPORT);
      }

      if (url.includes("/rows")) {
        //#страница отвечает столько строк, сколько отобрал сервер: с находкой —
        //#ровно столько, сколько она насчитала
        return jsonResponse(page(url.includes("finding=") ? FINDING.affected_rows : 200));
      }

      if (url.includes("/datasets")) {
        return jsonResponse({ datasets: [DATASET] });
      }

      return jsonResponse(WORKSPACE);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

async function openHealth(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  render(<App />);

  await user.click(await screen.findByText("продажи.csv"));
  await user.click(screen.getByRole("button", { name: "Health" }));
  await screen.findByText(FINDING.title);
}

describe("путь от находки к строкам", () => {
  it("«Показать строки» открывает таблицу с этой находкой", async () => {
    const user = userEvent.setup();
    await openHealth(user);

    await user.click(screen.getByRole("button", { name: /Показать строки/ }));

    //#таблица открылась и спросила у сервера именно эту находку
    await waitFor(() =>
      expect(
        requestedUrls.some(
          (url) => url.includes("/rows") && url.includes(`finding=${FINDING.finding_id}`),
        ),
      ).toBe(true),
    );
  });

  it("в таблице видно, что показаны строки находки, и сколько их", async () => {
    const user = userEvent.setup();
    await openHealth(user);

    await user.click(screen.getByRole("button", { name: /Показать строки/ }));

    expect(await screen.findByText(/Показаны строки находки/)).toBeInTheDocument();
    //#число совпадает с тем, что назвала находка: иначе доверия к разделу не останется
    expect(screen.getByText(/127 строк/)).toBeInTheDocument();
  });

  it("из таблицы можно вернуться в диагностику", async () => {
    const user = userEvent.setup();
    await openHealth(user);

    await user.click(screen.getByRole("button", { name: /Показать строки/ }));
    await screen.findByText(/Показаны строки находки/);

    await user.click(screen.getByRole("button", { name: /Вернуться к диагностике/ }));

    //#контекст не потерян: та же находка на месте
    expect(await screen.findByText(FINDING.title)).toBeInTheDocument();
  });

  it("можно снять отбор и увидеть весь датасет", async () => {
    const user = userEvent.setup();
    await openHealth(user);

    await user.click(screen.getByRole("button", { name: /Показать строки/ }));
    await screen.findByText(/Показаны строки находки/);
    requestedUrls.length = 0;

    await user.click(screen.getByRole("button", { name: /Показать весь датасет/ }));

    await waitFor(() =>
      expect(
        requestedUrls.some((url) => url.includes("/rows") && !url.includes("finding=")),
      ).toBe(true),
    );
    expect(screen.queryByText(/Показаны строки находки/)).not.toBeInTheDocument();
  });

  it("сортировка внутри найденных строк сохраняет отбор", async () => {
    //#сортировка не должна выбрасывать пользователя обратно ко всему датасету
    const user = userEvent.setup();
    await openHealth(user);

    await user.click(screen.getByRole("button", { name: /Показать строки/ }));
    await screen.findByText(/Показаны строки находки/);
    requestedUrls.length = 0;

    //#заголовок колонки — кнопка сортировки; берём первую, это «id»
    await user.click(screen.getAllByTitle("Сортировать")[0]!);

    await waitFor(() =>
      expect(
        requestedUrls.some(
          (url) =>
            url.includes("/rows") &&
            url.includes(`finding=${FINDING.finding_id}`) &&
            url.includes("sort=id"),
        ),
      ).toBe(true),
    );
  });

  it("выбор другого датасета снимает находку прежнего", async () => {
    /* Находка принадлежит датасету и артефакту. Оставить её при переходе значило бы
       показать строки одного датасета под находкой другого. */
    const user = userEvent.setup();
    await openHealth(user);

    await user.click(screen.getByRole("button", { name: /Показать строки/ }));
    await screen.findByText(/Показаны строки находки/);

    await user.click(screen.getByRole("button", { name: "Workspace" }));
    await user.click(await screen.findByText("продажи.csv"));

    expect(screen.queryByText(/Показаны строки находки/)).not.toBeInTheDocument();
  });
});
