import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, isCancelled } from "../../api/client";
import {
  cancelQuery,
  deleteSavedQuery,
  fetchQueryBindings,
  fetchQueryHistory,
  fetchSavedQueries,
  runQuery,
  saveQuery,
} from "../../api/endpoints";
import type { DatasetBinding, QueryResponse, QueryRun, SavedQuery } from "../../types/sql";

//#предел совпадает с тем, что проверяет backend: пользователь узнаёт о превышении
//#до отправки, а не после
export const MAX_SQL_LENGTH = 20_000;

function newQueryToken(): string {
  //#метка нужна ДО отправки запроса, иначе отменять будет нечего: идентификатор из
  //#ответа приходит уже после того, как запрос закончился
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export type SqlState = {
  bindings: DatasetBinding[];
  history: QueryRun[];
  saved: SavedQuery[];
  response: QueryResponse | null;
  error: string | null;
  running: boolean;
  //#отменять можно, только пока запрос выполняется и метка известна: иначе кнопки нет
  cancellable: boolean;
  loadingLists: boolean;
};

export function useSqlWorkspace(workspaceId: string | null) {
  const [state, setState] = useState<SqlState>({
    bindings: [],
    history: [],
    saved: [],
    response: null,
    error: null,
    running: false,
    cancellable: false,
    loadingLists: false,
  });

  //#метка живёт в ref, а не в состоянии: отмена должна дотянуться до неё, не дожидаясь
  //#перерисовки, иначе между запуском и обновлением состояния отменять нечем
  const tokenRef = useRef<string | null>(null);
  //#счётчик запусков: ответ устаревшего запроса не должен перезаписать более новый,
  //#если пользователь успел запустить следующий
  const runIdRef = useRef(0);

  const refreshLists = useCallback(
    async (signal?: AbortSignal) => {
      if (!workspaceId) {
        return;
      }

      setState((current) => ({ ...current, loadingLists: true }));

      try {
        const [bindings, history, saved] = await Promise.all([
          fetchQueryBindings(workspaceId, signal),
          fetchQueryHistory(workspaceId, signal),
          fetchSavedQueries(workspaceId, signal),
        ]);

        setState((current) => ({
          ...current,
          bindings: bindings.datasets,
          history: history.runs,
          saved: saved.queries,
          loadingLists: false,
        }));
      } catch (error) {
        if (isCancelled(error)) {
          return;
        }

        setState((current) => ({
          ...current,
          loadingLists: false,
          error: describeError(error, "Не удалось загрузить состояние окна запросов."),
        }));
      }
    },
    [workspaceId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refreshLists(controller.signal);
    return () => controller.abort();
  }, [refreshLists]);

  const execute = useCallback(
    async (sql: string) => {
      if (!workspaceId || !sql.trim()) {
        return;
      }

      const token = newQueryToken();
      const attempt = runIdRef.current + 1;
      runIdRef.current = attempt;
      tokenRef.current = token;

      setState((current) => ({
        ...current,
        running: true,
        cancellable: true,
        error: null,
      }));

      try {
        const response = await runQuery(workspaceId, { sql, query_token: token });

        if (runIdRef.current !== attempt) {
          //#пока шёл ответ, пользователь запустил другой запрос: этот результат устарел
          return;
        }

        setState((current) => ({ ...current, response, running: false, cancellable: false }));
      } catch (error) {
        if (runIdRef.current !== attempt) {
          return;
        }

        setState((current) => ({
          ...current,
          running: false,
          cancellable: false,
          //#результат прошлого запроса убирается: показывать его под новым текстом
          //#запроса значило бы выдавать старые строки за новые
          response: null,
          error: describeError(error, "Запрос не выполнен."),
        }));
      } finally {
        if (runIdRef.current === attempt) {
          tokenRef.current = null;
        }

        //#история обновляется при любом исходе: неудачный запрос тоже в неё попадает
        void refreshLists();
      }
    },
    [workspaceId, refreshLists],
  );

  const cancel = useCallback(async () => {
    const token = tokenRef.current;

    if (!workspaceId || !token) {
      return;
    }

    try {
      await cancelQuery(workspaceId, token);
    } catch {
      //#запрос успел завершиться сам между нажатием и доставкой отмены: отменять нечего,
      //#и сообщать об этом как об ошибке незачем — исход придёт обычным ответом
    }
  }, [workspaceId]);

  const save = useCallback(
    async (name: string, sql: string) => {
      if (!workspaceId) {
        return false;
      }

      try {
        await saveQuery(workspaceId, { name, sql });
        await refreshLists();
        return true;
      } catch (error) {
        setState((current) => ({
          ...current,
          error: describeError(error, "Не удалось сохранить запрос."),
        }));
        return false;
      }
    },
    [workspaceId, refreshLists],
  );

  const remove = useCallback(
    async (savedQueryId: string) => {
      if (!workspaceId) {
        return;
      }

      try {
        await deleteSavedQuery(workspaceId, savedQueryId);
      } catch (error) {
        setState((current) => ({
          ...current,
          error: describeError(error, "Не удалось удалить запрос."),
        }));
      }

      await refreshLists();
    },
    [workspaceId, refreshLists],
  );

  const dismissError = useCallback(() => {
    setState((current) => ({ ...current, error: null }));
  }, []);

  return { state, execute, cancel, save, remove, refreshLists, dismissError };
}
