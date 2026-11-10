import { useCallback, useEffect, useState } from "react";
import { ApiError, describeError } from "../api/client";
import { createWorkspace, fetchWorkspace } from "../api/endpoints";

const WORKSPACE_STORAGE_KEY = "DataArenaWorkspaceId";

//#идентификатор рабочего пространства хранится локально, а сами данные — на сервере:
//#после перезагрузки страницы датасеты, история и запросы приходят с backend,
//#а не восстанавливаются из браузера
export function useWorkspaceId() {
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const open = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const stored = localStorage.getItem(WORKSPACE_STORAGE_KEY);

      if (stored) {
        //#сохранённый идентификатор проверяется: база могла быть очищена или пространство
        //#удалено. Прежде такой идентификатор принимался навсегда, и все разделы отвечали
        //#«не найдено» до ручной очистки хранилища браузера
        try {
          await fetchWorkspace(stored);
          setWorkspaceId(stored);
          return;
        } catch (caught) {
          if (!(caught instanceof ApiError && caught.status === 404)) {
            throw caught;
          }

          localStorage.removeItem(WORKSPACE_STORAGE_KEY);
        }
      }

      const workspace = await createWorkspace("Рабочее пространство");
      localStorage.setItem(WORKSPACE_STORAGE_KEY, workspace.workspace_id);
      setWorkspaceId(workspace.workspace_id);
    } catch (caught) {
      setError(describeError(caught, "Не удалось открыть рабочее пространство."));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void open();
  }, [open]);

  return { workspaceId, error, isLoading, retry: open };
}
