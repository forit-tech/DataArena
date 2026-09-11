import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import * as endpoints from "../../api/endpoints";
import type { DatasetBinding, QueryResponse, QueryRun, SavedQuery } from "../../types/sql";
import { SqlWorkspace } from "./SqlWorkspace";
import { useSqlWorkspace } from "./useSqlWorkspace";

const WORKSPACE = "ws_0123456789abcdef";

const BINDINGS: DatasetBinding[] = [
  {
    alias: "продажи",
    dataset_id: "ds_0123456789abcdef",
    name: "продажи.csv",
    row_count: 20,
    column_count: 3,
    columns: ["id", "город", "сумма"],
    queryable: true,
  },
  {
    alias: "битый",
    dataset_id: "ds_0123456789abcdee",
    name: "битый.json",
    row_count: null,
    column_count: 0,
    columns: [],
    queryable: false,
  },
];

function makeRun(overrides: Partial<QueryRun> = {}): QueryRun {
  return {
    run_id: "qr_0123456789abcdef",
    sql: "SELECT count(*) FROM продажи",
    status: "succeeded",
    started_at: "2026-01-01T00:00:00+00:00",
    elapsed_ms: 12,
    datasets: ["продажи"],
    row_count: 1,
    truncated: false,
    truncated_by: null,
    error_code: null,
    ...overrides,
  };
}

function makeResponse(overrides: Partial<QueryResponse["result"]> = {}): QueryResponse {
  return {
    result: {
      columns: ["город", "n"],
      rows: [
        { город: "Москва", n: 5 },
        { город: null, n: 2 },
      ],
      row_count: 2,
      truncated: false,
      truncated_by: null,
      elapsed_ms: 12,
      ...overrides,
    },
    run: makeRun(),
  };
}

function stubLists(saved: SavedQuery[] = [], history: QueryRun[] = []) {
  vi.spyOn(endpoints, "fetchQueryBindings").mockResolvedValue({ datasets: BINDINGS });
  vi.spyOn(endpoints, "fetchQueryHistory").mockResolvedValue({ runs: history });
  vi.spyOn(endpoints, "fetchSavedQueries").mockResolvedValue({ queries: saved });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("окно SQL", () => {
  it("показывает псевдонимы, которые можно написать в FROM", async () => {
    //#имя датасета в SQL не должно быть загадкой: иначе пользователь угадывает,
    //#как называется его файл после загрузки
    stubLists();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    expect(await screen.findByText("продажи")).toBeInTheDocument();
    expect(screen.getByText("битый")).toBeInTheDocument();
  });

  it("не даёт вставить в запрос датасет, который нельзя читать", async () => {
    stubLists();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    const broken = await screen.findByText("битый");

    expect(broken.closest("button")).toBeDisabled();
  });

  it("выполняет запрос и показывает результат", async () => {
    stubLists();
    const run = vi.spyOn(endpoints, "runQuery").mockResolvedValue(makeResponse());
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.type(screen.getByLabelText("Текст SQL-запроса"), "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));

    expect(await screen.findByText("Москва")).toBeInTheDocument();
    expect(run).toHaveBeenCalledOnce();
  });

  it("метка отмены создаётся до отправки запроса", async () => {
    //#идентификатор из ответа пришёл бы уже после завершения, и отменять было бы нечего
    stubLists();
    const run = vi.spyOn(endpoints, "runQuery").mockResolvedValue(makeResponse());
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.type(screen.getByLabelText("Текст SQL-запроса"), "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));

    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0]?.[1].query_token).toMatch(/^[0-9a-f]{32}$/);
  });

  it("кнопка отмены появляется только пока запрос выполняется", async () => {
    //#кнопка, которая ничего не отменяет, хуже её отсутствия
    stubLists();
    let release: ((value: QueryResponse) => void) | undefined;
    vi.spyOn(endpoints, "runQuery").mockReturnValue(
      new Promise<QueryResponse>((resolve) => {
        release = resolve;
      }),
    );
    const cancel = vi.spyOn(endpoints, "cancelQuery").mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    expect(screen.queryByRole("button", { name: /Отменить/ })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Текст SQL-запроса"), "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));

    const cancelButton = await screen.findByRole("button", { name: /Отменить/ });
    await user.click(cancelButton);

    expect(cancel).toHaveBeenCalledWith(WORKSPACE, expect.stringMatching(/^[0-9a-f]{32}$/));

    release?.(makeResponse());

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Отменить/ })).not.toBeInTheDocument(),
    );
  });

  it("усечение результата названо прямо", async () => {
    //#срез, выданный за полный результат, — неверный ответ, а не экономия
    stubLists();
    vi.spyOn(endpoints, "runQuery").mockResolvedValue({
      ...makeResponse({ truncated: true, truncated_by: "rows", row_count: 1000 }),
    });
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.type(screen.getByLabelText("Текст SQL-запроса"), "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));

    expect(await screen.findByText(/их было больше/)).toBeInTheDocument();
  });

  it("ошибка показывается, а прежний результат убирается", async () => {
    //#старые строки под новым текстом запроса выглядели бы как его результат
    stubLists();
    const run = vi.spyOn(endpoints, "runQuery");
    run.mockResolvedValueOnce(makeResponse());
    run.mockRejectedValueOnce(
      new ApiError(403, { code: "sql_rejected", message: "Функции недоступны в окне запросов.", details: {}, }),
    );
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    const editor = screen.getByLabelText("Текст SQL-запроса");
    await user.type(editor, "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));
    expect(await screen.findByText("Москва")).toBeInTheDocument();

    await user.clear(editor);
    await user.type(editor, "SELECT version() FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));

    expect(await screen.findByText(/Функции недоступны/)).toBeInTheDocument();
    expect(screen.queryByText("Москва")).not.toBeInTheDocument();
  });

  it("пустое значение отличимо от пустой строки", async () => {
    stubLists();
    vi.spyOn(endpoints, "runQuery").mockResolvedValue(makeResponse());
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.type(screen.getByLabelText("Текст SQL-запроса"), "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Выполнить/ }));

    expect(await screen.findByText("—")).toBeInTheDocument();
  });

  it("история показывает и неудачные запросы", async () => {
    //#к истории чаще всего возвращаются именно после неудачи
    stubLists(
      [],
      [
        makeRun({ run_id: "qr_1", status: "failed", error_code: "sql_rejected", row_count: null }),
        makeRun({ run_id: "qr_2" }),
      ],
    );
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    expect(await screen.findByText("ошибка")).toBeInTheDocument();
    expect(screen.getByText(/выполнен/)).toBeInTheDocument();
  });

  it("запрос из истории возвращается в редактор", async () => {
    stubLists([], [makeRun({ sql: "SELECT город FROM продажи" })]);
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.click(await screen.findByText("SELECT город FROM продажи"));

    expect(screen.getByLabelText("Текст SQL-запроса")).toHaveValue("SELECT город FROM продажи");
  });

  it("сохранённый запрос открывается по имени", async () => {
    stubLists([
      {
        saved_query_id: "sq_0123456789abcdef",
        name: "Выручка",
        sql: "SELECT сумма FROM продажи",
        description: null,
        created_at: "2026-01-01T00:00:00+00:00",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
    ]);
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.click(await screen.findByText("Выручка"));

    expect(screen.getByLabelText("Текст SQL-запроса")).toHaveValue("SELECT сумма FROM продажи");
  });

  it("занятое имя показывается как понятная ошибка, а форма остаётся открытой", async () => {
    //#пользователь должен иметь возможность исправить имя, а не набирать запрос заново
    stubLists();
    vi.spyOn(endpoints, "saveQuery").mockRejectedValue(
      new ApiError(409, {
        code: "saved_query_name_taken",
        message: "Запрос с именем «Выручка» уже сохранён.",
        details: {},
      }),
    );
    const user = userEvent.setup();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    await user.type(screen.getByLabelText("Текст SQL-запроса"), "SELECT 1 FROM продажи");
    await user.click(screen.getByRole("button", { name: /Сохранить/ }));
    await user.type(screen.getByLabelText("Имя сохранённого запроса"), "Выручка");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText(/уже сохранён/)).toBeInTheDocument();
    expect(screen.getByLabelText("Имя сохранённого запроса")).toHaveValue("Выручка");
  });

  it("без рабочего пространства раздел объясняет, что делать", async () => {
    render(<SqlWorkspace workspaceId={null} />);

    expect(screen.getByText("Рабочее пространство не выбрано")).toBeInTheDocument();
  });

  it("пустой запрос выполнить нельзя", async () => {
    stubLists();
    render(<SqlWorkspace workspaceId={WORKSPACE} />);

    expect(await screen.findByRole("button", { name: /Выполнить/ })).toBeDisabled();
  });
});

describe("состояние окна запросов", () => {
  it("ответ устаревшего запроса не перезаписывает более новый", async () => {
    /* Кнопка «Выполнить» блокируется на время выполнения, поэтому через интерфейс
       два запроса подряд не запустить — но защита живёт в хуке, и проверять её нужно
       там же. Иначе она остаётся непроверенной до первого места, где вызов пойдёт
       не из этой кнопки: горячей клавиши, автоповтора, второго редактора. */
    stubLists();
    let releaseFirst: ((value: QueryResponse) => void) | undefined;
    const run = vi.spyOn(endpoints, "runQuery");
    run.mockReturnValueOnce(
      new Promise<QueryResponse>((resolve) => {
        releaseFirst = resolve;
      }),
    );
    run.mockResolvedValueOnce({
      ...makeResponse(),
      result: { ...makeResponse().result, rows: [{ город: "Казань", n: 9 }] },
    });

    const { result } = renderHook(() => useSqlWorkspace(WORKSPACE));

    let first: Promise<void> | undefined;

    await act(async () => {
      first = result.current.execute("SELECT 1 FROM продажи");
      await result.current.execute("SELECT 2 FROM продажи");
    });

    await waitFor(() => expect(result.current.state.response?.result.rows[0]?.город).toBe("Казань"));

    await act(async () => {
      releaseFirst?.(makeResponse());
      await first;
    });

    //#ответ первого запроса пришёл последним, но он устарел и обязан быть отброшен
    await waitFor(() => expect(result.current.state.response?.result.rows[0]?.город).toBe("Казань"));
  });

  it("после успешного запроса списки перечитываются", async () => {
    //#история обновляется при любом исходе: неудачный запрос тоже в неё попадает
    stubLists();
    vi.spyOn(endpoints, "runQuery").mockResolvedValue(makeResponse());
    const history = vi.mocked(endpoints.fetchQueryHistory);

    const { result } = renderHook(() => useSqlWorkspace(WORKSPACE));
    await waitFor(() => expect(history).toHaveBeenCalledTimes(1));

    await act(() => result.current.execute("SELECT 1 FROM продажи"));

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
  });

  it("после неудачного запроса списки тоже перечитываются", async () => {
    stubLists();
    vi.spyOn(endpoints, "runQuery").mockRejectedValue(
      new ApiError(403, { code: "sql_rejected", message: "Отказано.", details: {}, }),
    );
    const history = vi.mocked(endpoints.fetchQueryHistory);

    const { result } = renderHook(() => useSqlWorkspace(WORKSPACE));
    await waitFor(() => expect(history).toHaveBeenCalledTimes(1));

    await act(() => result.current.execute("SELECT version() FROM продажи"));

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
  });
});
