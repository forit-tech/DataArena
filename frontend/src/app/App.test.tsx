import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

//#тесты используют заглушку fetch, поэтому не требуют запущенного backend и проходят в CI

const HEALTH = {
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
  dataset_count: 0,
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(overrides: Record<string, () => Response | Error> = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);

      for (const [fragment, respond] of Object.entries(overrides)) {
        if (url.includes(fragment)) {
          const outcome = respond();

          if (outcome instanceof Error) {
            throw outcome;
          }

          return outcome;
        }
      }

      if (url.includes("/system/health")) {
        return jsonResponse(HEALTH);
      }

      if (url.includes("/datasets")) {
        return jsonResponse({ datasets: [] });
      }

      if (url.includes("/workspaces")) {
        return jsonResponse(WORKSPACE, 201);
      }

      throw new Error(`неожиданный запрос: ${url}`);
    }),
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("оболочка приложения", () => {
  it("показывает версию контракта, когда backend отвечает", async () => {
    stubApi();

    render(<App />);

    expect(await screen.findByText("dataarena.package/1.0.0")).toBeInTheDocument();
  });

  it("показывает понятное сообщение, когда backend не запущен", async () => {
    stubApi({ "/system/health": () => new TypeError("Failed to fetch") });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Backend недоступен");
    });
    expect(screen.getByText(/порту 8510/)).toBeInTheDocument();
  });

  it("предлагает повторить проверку, а не оставляет пользователя в тупике", async () => {
    stubApi({ "/system/health": () => new TypeError("Failed to fetch") });

    render(<App />);

    expect(await screen.findByRole("button", { name: "Проверить снова" })).toBeInTheDocument();
  });

  it("нереализованные разделы недоступны, а не показывают пустой экран", async () => {
    //#кнопка, открывающая пустоту, выглядит как поломка, а не как «этого ещё нет»
    stubApi();

    render(<App />);

    for (const label of ["Workspace", "Dataset", "SQL", "Health"]) {
      expect(await screen.findByRole("button", { name: label })).toBeEnabled();
    }

    for (const label of ["Builder", "Recipes"]) {
      expect(screen.getByRole("button", { name: label })).toBeDisabled();
    }
  });

  it("пустое рабочее пространство объясняет, что делать дальше", async () => {
    stubApi();

    render(<App />);

    expect(await screen.findByText("Пока ничего не загружено")).toBeInTheDocument();
  });

  it("ошибка списка датасетов показывается, а не проглатывается", async () => {
    stubApi({
      "/datasets": () =>
        jsonResponse(
          {
            error: { code: "workspace_not_found", message: "Workspace не найден.", details: {} },
            detail: "Workspace не найден.",
          },
          404,
        ),
    });
    localStorage.setItem("DataArenaWorkspaceId", WORKSPACE.workspace_id);

    render(<App />);

    expect(await screen.findByText("Workspace не найден.")).toBeInTheDocument();
  });
});
