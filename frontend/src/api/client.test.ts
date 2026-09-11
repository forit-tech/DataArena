import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError, describeError, getJson } from "./client";

function respondWith(body: string, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body, { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("транспортный слой", () => {
  it("возвращает разобранный JSON на успешном ответе", async () => {
    respondWith(JSON.stringify({ status: "ok" }));

    await expect(getJson<{ status: string }>("/system/health")).resolves.toEqual({ status: "ok" });
  });

  it("разбирает доменную ошибку и сохраняет код и детали", async () => {
    respondWith(
      JSON.stringify({
        error: { code: "dataset_not_found", message: "Датасет не найден.", details: { id: "ds_1" } },
        detail: "Датасет не найден.",
      }),
      404,
    );

    await expect(getJson("/datasets/ds_1")).rejects.toMatchObject({
      status: 404,
      code: "dataset_not_found",
      message: "Датасет не найден.",
      details: { id: "ds_1" },
    });
  });

  it("не падает, когда сервер вернул не JSON", async () => {
    //#без этого браузер показывает техническое "Unexpected token", которое не объясняет причину
    respondWith("502 Bad Gateway", 502);

    const error = await getJson("/system/health").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe("502 Bad Gateway");
  });

  it("остановленный backend за прокси Vite распознаётся как сетевой сбой", async () => {
    //#прокси отдаёт пустой 500, когда target недоступен. Наш backend всегда возвращает непустое
    //#тело, поэтому пустое тело при 5xx означает «до backend не дошли» — и пользователю надо
    //#сказать, что нужно его запустить, а не «пустой ответ (500)»
    respondWith("", 500);

    const error = await getJson("/system/health").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(NetworkError);
    expect((error as NetworkError).message).toContain("8510");
  });

  it("пустой ответ 4xx остаётся ошибкой приложения", async () => {
    //#4xx приходит от работающего backend: это ошибка запроса, а не отсутствие сервиса
    respondWith("", 404);

    await expect(getJson("/system/health")).rejects.toMatchObject({ code: "empty_response" });
  });

  it("отличает сетевой сбой от ошибки приложения", async () => {
    //#это разные проблемы с разными действиями пользователя: запустить backend или исправить запрос
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    await expect(getJson("/system/health")).rejects.toBeInstanceOf(NetworkError);
  });

  it("describeError сводит любую ошибку к одной строке", () => {
    expect(describeError(new NetworkError("нет связи"), "запасной текст")).toBe("нет связи");
    expect(describeError("строка вместо ошибки", "запасной текст")).toBe("запасной текст");
  });
});
