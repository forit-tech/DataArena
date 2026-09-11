import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, FileUp, Loader2 } from "lucide-react";
import { describeError } from "../../api/client";
import { fetchDatasets, uploadDataset } from "../../api/endpoints";
import type { Dataset } from "../../types/dataset";
import { formatBytes, formatCount, formatRowCount } from "../../utils/format";

export type WorkspacePanelProps = {
  //#рабочее пространство открывается один раз на всё приложение: раздел SQL работает
  //#с тем же самым, и держать идентификатор внутри одной панели значило бы открыть второе
  workspaceId: string | null;
  onSelectDataset: (dataset: Dataset) => void;
  selectedDatasetId: string | null;
};

export function WorkspacePanel({ workspaceId, onSelectDataset, selectedDatasetId }: WorkspacePanelProps) {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadDatasets = useCallback(async (id: string) => {
    try {
      const response = await fetchDatasets(id);
      setDatasets(response.datasets);
      setError(null);
    } catch (caught) {
      setError(describeError(caught, "Не удалось получить список датасетов."));
    }
  }, []);

  useEffect(() => {
    //#данные живут на сервере: после перезагрузки страницы датасеты приходят с backend,
    //#а не восстанавливаются из браузера
    if (!workspaceId) {
      return;
    }

    void loadDatasets(workspaceId).finally(() => setIsLoading(false));
  }, [workspaceId, loadDatasets]);

  const handleFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0 || !workspaceId) {
        return;
      }

      setIsUploading(true);
      setError(null);

      try {
        //#файлы загружаются по одному последовательно: параллельная отправка нескольких
        //#больших файлов забивает канал и делает время ожидания непредсказуемым
        for (const file of Array.from(files)) {
          await uploadDataset(workspaceId, file);
        }

        await loadDatasets(workspaceId);
      } catch (caught) {
        setError(describeError(caught, "Не удалось загрузить файл."));
      } finally {
        setIsUploading(false);

        if (fileInput.current) {
          fileInput.current.value = "";
        }
      }
    },
    [workspaceId, loadDatasets],
  );

  if (isLoading) {
    return (
      <p className="table-status">
        <Loader2 size={13} className="spin" aria-hidden /> Открывается рабочее пространство…
      </p>
    );
  }

  return (
    <div className="workspace">
      <div className="workspace__actions">
        <button
          type="button"
          className="button button--primary"
          disabled={isUploading || !workspaceId}
          onClick={() => fileInput.current?.click()}
        >
          {isUploading ? (
            <>
              <Loader2 size={14} className="spin" aria-hidden /> Загружается…
            </>
          ) : (
            <>
              <FileUp size={14} aria-hidden /> Загрузить файл
            </>
          )}
        </button>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          accept=".csv,.tsv,.parquet,.json,.jsonl,.ndjson,.xlsx,.feather,.arrow,.ipc"
          onChange={(event) => void handleFiles(event.target.files)}
        />
        <span className="workspace__hint">
          CSV, TSV, Parquet, JSON, JSONL, XLSX, Feather, Arrow
        </span>
      </div>

      {error && (
        <div className="banner banner--error" role="alert">
          <div>
            <p className="banner__title">Ошибка</p>
            <p className="banner__text">{error}</p>
          </div>
        </div>
      )}

      {datasets.length === 0 ? (
        <div className="empty-state">
          <h2 className="empty-state__title">Пока ничего не загружено</h2>
          <p className="empty-state__text">
            Загрузите файл, чтобы посмотреть его содержимое. Исходный файл не изменяется:
            всё, что вы делаете дальше, применяется к копии.
          </p>
        </div>
      ) : (
        <ul className="dataset-list">
          {datasets.map((dataset) => (
            <li key={dataset.dataset_id}>
              <button
                type="button"
                className="dataset-card"
                aria-current={dataset.dataset_id === selectedDatasetId ? "true" : undefined}
                onClick={() => onSelectDataset(dataset)}
              >
                <span className="dataset-card__name">{dataset.name}</span>
                <span className="dataset-card__meta tabular">
                  {formatRowCount(dataset.row_count)} строк · {formatCount(dataset.column_count)}{" "}
                  колонок · {formatBytes(dataset.source.bytes)}
                </span>
                <DatasetFormatNote dataset={dataset} />
                {dataset.status !== "ready" && (
                  <span className="dataset-card__problem">
                    <AlertTriangle size={12} aria-hidden />
                    {dataset.status_reason ?? "Датасет недоступен для чтения."}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DatasetFormatNote({ dataset }: { dataset: Dataset }) {
  //#пользователь видит формат своего файла и формат, в котором с ним работают,
  //#без технических подробностей о том, где что лежит
  if (!dataset.normalized) {
    return <span className="dataset-card__format">{dataset.original_format.toUpperCase()}</span>;
  }

  return (
    <span className="dataset-card__format" title="Формат исходного файла не изменён">
      {dataset.original_format.toUpperCase()} → рабочий {dataset.working_format.toUpperCase()}
    </span>
  );
}
