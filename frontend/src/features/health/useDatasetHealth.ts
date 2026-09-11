import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, isCancelled } from "../../api/client";
import { fetchDatasetHealth } from "../../api/endpoints";
import type { HealthReport } from "../../types/health";

type State = {
  report: HealthReport | null;
  isLoading: boolean;
  error: string | null;
};

export function useDatasetHealth(workspaceId: string | null, datasetId: string | null) {
  const [state, setState] = useState<State>({ report: null, isLoading: true, error: null });

  //#номер запроса: при быстрой смене датасета ответ по прежнему не должен
  //#перезаписать уже показанный новый — тот же класс гонки, что и в таблице
  const requestId = useRef(0);
  const inFlight = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    if (!workspaceId || !datasetId) {
      setState({ report: null, isLoading: false, error: null });
      return;
    }

    const current = ++requestId.current;
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    //#прежний отчёт убирается сразу: показывать его, пока считается новый, значило бы
    //#выдавать устаревшие находки за актуальные
    setState({ report: null, isLoading: true, error: null });

    void (async () => {
      try {
        const report = await fetchDatasetHealth(workspaceId, datasetId, controller.signal);

        if (current === requestId.current) {
          setState({ report, isLoading: false, error: null });
        }
      } catch (error) {
        if (isCancelled(error) || current !== requestId.current) {
          return;
        }

        setState({
          report: null,
          isLoading: false,
          error: describeError(error, "Не удалось получить диагностику датасета."),
        });
      }
    })();
  }, [workspaceId, datasetId]);

  useEffect(() => {
    load();
    return () => inFlight.current?.abort();
  }, [load]);

  return { ...state, reload: load };
}
