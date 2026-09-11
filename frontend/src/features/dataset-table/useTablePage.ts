import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, isCancelled } from "../../api/client";
import { fetchRows } from "../../api/endpoints";
import type { ColumnFilter, Page, SortDirection } from "../../types/dataset";

export type TableQuery = {
  offset: number;
  limit: number;
  sort: { column: string; direction: SortDirection }[];
  filters: ColumnFilter[];
  search: string;
  //#строки находки диагностики; null означает «весь датасет»
  finding?: string | null;
};

export const PAGE_SIZE = 200;

export const EMPTY_QUERY: TableQuery = {
  offset: 0,
  limit: PAGE_SIZE,
  sort: [],
  filters: [],
  search: "",
  finding: null,
};

type State = {
  page: Page | null;
  isLoading: boolean;
  error: string | null;
};

export function useTablePage(workspaceId: string, datasetId: string, query: TableQuery) {
  const [state, setState] = useState<State>({ page: null, isLoading: true, error: null });

  //#каждый запрос получает номер, и применяется результат только последнего
  //#без этого медленный ответ на старую сортировку перезаписывает уже показанный новый
  const requestId = useRef(0);
  const inFlight = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    const current = ++requestId.current;

    //#предыдущий запрос отменяется: он больше никому не нужен, а сервер перестаёт
    //#тратить на него время
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    setState((previous) => ({ ...previous, isLoading: true, error: null }));

    void (async () => {
      try {
        const page = await fetchRows(workspaceId, datasetId, query, controller.signal);

        if (current === requestId.current) {
          setState({ page, isLoading: false, error: null });
        }
      } catch (error) {
        //#отмена — не ошибка: пользователь просто передумал, и показывать ему нечего
        if (isCancelled(error) || current !== requestId.current) {
          return;
        }

        setState({
          page: null,
          isLoading: false,
          error: describeError(error, "Не удалось загрузить страницу."),
        });
      }
    })();
  }, [workspaceId, datasetId, query]);

  useEffect(() => {
    load();
    return () => inFlight.current?.abort();
  }, [load]);

  return { ...state, reload: load };
}
