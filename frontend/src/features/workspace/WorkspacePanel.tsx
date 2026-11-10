import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, FileUp, Loader2, X } from "lucide-react";
import { describeError } from "../../api/client";
import { deleteDataset, fetchDatasets, uploadDataset } from "../../api/endpoints";
import type { Dataset } from "../../types/dataset";
import { columnsLabel, formatBytes, rowsLabel } from "../../utils/format";

const SUPPORTED = [".csv", ".tsv", ".parquet", ".json", ".jsonl", ".ndjson", ".xlsx", ".feather", ".arrow", ".ipc"];

function isSupported(name: string): boolean {
  const lower = name.toLowerCase();
  return SUPPORTED.some((extension) => lower.endsWith(extension));
}

function readEntry(entry: FileSystemEntry): Promise<File[]> {
  if (entry.isFile) {
    return new Promise((resolve) => (entry as FileSystemFileEntry).file((file) => resolve([file]), () => resolve([])));
  }
  if (!entry.isDirectory) {
    return Promise.resolve([]);
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  //#readEntries отдаёт содержимое папки порциями: читать нужно, пока не вернётся пустая
  const readBatch = (): Promise<FileSystemEntry[]> =>
    new Promise((resolve) => reader.readEntries(resolve, () => resolve([])));
  return (async () => {
    const files: File[] = [];
    for (let batch = await readBatch(); batch.length > 0; batch = await readBatch()) {
      for (const child of batch) {
        files.push(...(await readEntry(child)));
      }
    }
    return files;
  })();
}

//#перетащить можно и файлы, и папки: папка обходится вместе с вложенными,
//#и из неё берутся только поддерживаемые форматы
async function droppedFiles(transfer: DataTransfer): Promise<{ files: File[]; skipped: number }> {
  //#записи берутся синхронно, до первого await: после него браузер очищает DataTransfer
  const entries = Array.from(transfer.items ?? [])
    .map((item) => item.webkitGetAsEntry?.() ?? null)
    .filter((entry): entry is FileSystemEntry => entry !== null);
  const all = entries.length > 0 ? (await Promise.all(entries.map(readEntry))).flat() : Array.from(transfer.files);
  const files = all.filter((file) => isSupported(file.name));
  return { files, skipped: all.length - files.length };
}

export type WorkspacePanelProps = {
  //#рабочее пространство открывается один раз на всё приложение: раздел SQL работает
  //#с тем же самым, и держать идентификатор внутри одной панели значило бы открыть второе
  workspaceId: string | null;
  //#рабочее пространство не открылось: без этого панель показывала «Открывается…» вечно
  workspaceError?: string | null;
  onRetryWorkspace?: () => void;
  onSelectDataset: (dataset: Dataset) => void;
  selectedDatasetId: string | null;
  onDatasetDeleted?: (datasetId: string) => void;
};

export function WorkspacePanel({
  workspaceId,
  workspaceError = null,
  onRetryWorkspace,
  onSelectDataset,
  selectedDatasetId,
  onDatasetDeleted,
}: WorkspacePanelProps) {
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
    async (files: FileList | File[] | null, skipped = 0) => {
      const list = files ? Array.from(files) : [];
      if (list.length === 0 || !workspaceId) {
        return;
      }

      setIsUploading(true);
      setError(null);
      const failed: string[] = [];

      //#файлы загружаются по одному последовательно: параллельная отправка нескольких
      //#больших файлов забивает канал и делает время ожидания непредсказуемым.
      //#Ошибка одного файла не останавливает остальные: раньше неподходящий файл
      //#в середине выбора молча отменял загрузку всех следующих
      for (const file of list) {
        try {
          await uploadDataset(workspaceId, file);
        } catch (caught) {
          failed.push(`${file.name}: ${describeError(caught, "не удалось загрузить")}`);
        }
      }

      try {
        await loadDatasets(workspaceId);
      } finally {
        setIsUploading(false);
        if (fileInput.current) {
          fileInput.current.value = "";
        }
      }

      if (skipped > 0) {
        failed.push(`Пропущено файлов неподдерживаемого формата: ${skipped}.`);
      }
      if (failed.length > 0) {
        setError(failed.join(String.fromCharCode(10)));
      }
    },
    [workspaceId, loadDatasets],
  );

  const remove = useCallback(
    async (dataset: Dataset) => {
      if (!workspaceId) return;
      //#удаление безвозвратно: файл, шаги конвейера и диагностика исчезают вместе с датасетом
      const confirmed = window.confirm(
        `Удалить «${dataset.name}»? Файл, шаги конвейера и диагностика будут удалены без возможности восстановить.`,
      );
      if (!confirmed) return;
      try {
        await deleteDataset(workspaceId, dataset.dataset_id);
        setDatasets((previous) => previous.filter((item) => item.dataset_id !== dataset.dataset_id));
        onDatasetDeleted?.(dataset.dataset_id);
      } catch (caught) {
        setError(describeError(caught, "Не удалось удалить датасет."));
      }
    },
    [workspaceId, onDatasetDeleted],
  );

  const [isDragging, setIsDragging] = useState(false);
  const dragDepth = useRef(0);

  if (!workspaceId && workspaceError) {
    return (
      <div className="banner banner--error" role="alert">
        <div>
          <p className="banner__title">Рабочее пространство не открылось</p>
          <p className="banner__text">{workspaceError}</p>
          {onRetryWorkspace && (
            <button type="button" className="button" onClick={onRetryWorkspace}>
              Попробовать снова
            </button>
          )}
        </div>
      </div>
    );
  }

  if (isLoading) {
    return (
      <p className="table-status">
        <Loader2 size={13} className="spin" aria-hidden /> Открывается рабочее пространство…
      </p>
    );
  }

  return (
    <div
      className={`workspace${isDragging ? " workspace--dragging" : ""}`}
      //#файлы можно перетащить в раздел мышью: без этого браузер открывал файл
      //#вместо загрузки, и добавить несколько файлов разом было нельзя
      onDragEnter={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        dragDepth.current += 1;
        setIsDragging(true);
      }}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) event.preventDefault();
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setIsDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        dragDepth.current = 0;
        setIsDragging(false);
        void droppedFiles(event.dataTransfer).then(({ files, skipped }) => {
          if (files.length === 0) {
            setError(
              skipped > 0
                ? `Подходящих файлов нет: пропущено ${skipped}. Поддерживаются CSV, TSV, Parquet, JSON, JSONL, XLSX, Feather, Arrow.`
                : null,
            );
            return;
          }
          void handleFiles(files, skipped);
        });
      }}
    >
      <div className="workspace__actions">
        <button
          title="CSV, TSV, Parquet, JSON, JSONL, XLSX, Feather, Arrow"
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
      </div>

      {error && (
        <div className="banner banner--error" role="alert">
          <div>
            <p className="banner__title">Не все файлы загрузились</p>
            <p className="banner__text banner__text--lines">{error}</p>
          </div>
        </div>
      )}

      {datasets.length === 0 ? (
        <div className="empty-state">
          <h2 className="empty-state__title">Пока ничего не загружено</h2>
          <p className="empty-state__text">
            Нажмите «Загрузить файл» или перетащите сюда файлы или целую папку.
          </p>
        </div>
      ) : (
        <ul className="dataset-list">
          {datasets.map((dataset) => (
            <li key={dataset.dataset_id} className="dataset-item">
              <button
                type="button"
                className="dataset-card__remove"
                aria-label={`Удалить ${dataset.name}`}
                title="Удалить"
                onClick={() => void remove(dataset)}
              >
                <X size={14} aria-hidden />
              </button>
              <button
                type="button"
                className="dataset-card"
                aria-current={dataset.dataset_id === selectedDatasetId ? "true" : undefined}
                onClick={() => onSelectDataset(dataset)}
              >
                <span className="dataset-card__name">{dataset.name}</span>
                <span className="dataset-card__meta tabular">
                  {rowsLabel(dataset.row_count)} · {columnsLabel(dataset.column_count)} ·{" "}
                  {formatBytes(dataset.source.bytes)}
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
