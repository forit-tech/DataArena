import { useCallback, useEffect, useState } from "react";
import { Download, Loader2, Package } from "lucide-react";
import { describeError, getBlob } from "../../api/client";
import { exportUrl, fetchWritableFormats, packageUrl } from "../../api/endpoints";
import type { Dataset, FormatInfo } from "../../types/dataset";
import { rowsLabel } from "../../utils/format";

export type ExportPanelProps = {
  dataset: Dataset;
};

type Pending = "export" | "package" | null;

//#файл сохраняется браузером под именем, которое прислал сервер
function save(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");

  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function ExportPanel({ dataset }: ExportPanelProps) {
  const [formats, setFormats] = useState<FormatInfo[]>([]);
  const [chosen, setChosen] = useState("csv");
  const [pending, setPending] = useState<Pending>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    fetchWritableFormats(controller.signal)
      .then((body) => setFormats(body.formats))
      .catch((cause: unknown) => {
        //#список форматов приходит с сервера вместе с ограничениями. Если он не пришёл,
        //#выдумывать его в браузере нельзя — но и молчать тоже: без списка кнопка
        //#выгрузки выглядела бы рабочей
        if (!controller.signal.aborted) {
          setError(describeError(cause, "Не удалось получить список форматов."));
        }
      });

    return () => controller.abort();
  }, []);

  //#файл запрашивается кодом, а не ссылкой: ошибку сервера (формат не вмещает строки,
  //#результат пуст) ссылка сохраняла бы как «выгрузку» с текстом ошибки внутри,
  //#и человек узнал бы об этом, только открыв файл
  const download = useCallback(async (kind: Exclude<Pending, null>, url: string, fallback: string) => {
    setPending(kind);
    setError(null);

    try {
      const { blob, fileName } = await getBlob(url);
      save(blob, fileName ?? fallback);
    } catch (cause) {
      setError(describeError(cause, kind === "export" ? "Выгрузка не удалась." : "Пакет не собран."));
    } finally {
      setPending(null);
    }
  }, []);

  const current = formats.find((item) => item.key === chosen) ?? null;
  const note = current
    ? [
        current.preserves_schema ? "Типы колонок сохраняются." : "Типы колонок сохраняются не полностью.",
        current.preserves_nulls ? "" : "Пропуски станут пустыми значениями.",
        current.row_limit ? `Не больше ${rowsLabel(current.row_limit)}.` : "",
        ...current.warnings,
      ]
        .filter(Boolean)
        .join(" ")
    : undefined;

  return (
    <section className="export">
      <h2 className="export__title">Забрать результат</h2>

      <div className="export__row">
        <select
          className="export__format"
          aria-label="Формат выгрузки"
          //#особенности формата — в подсказке, а не отдельным абзацем под кнопками
          title={note}
          value={chosen}
          onChange={(event) => setChosen(event.target.value)}
        >
            {formats.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
              </option>
            ))}
        </select>

        <button
          type="button"
          className="button button--primary"
          disabled={pending !== null || formats.length === 0}
          onClick={() =>
            void download(
              "export",
              exportUrl(dataset.workspace_id, dataset.dataset_id, chosen),
              `export.${chosen}`,
            )
          }
        >
          {pending === "export" ? (
            <Loader2 size={14} className="spin" aria-hidden />
          ) : (
            <Download size={14} aria-hidden />
          )}{" "}
          {pending === "export" ? "Готовится…" : "Выгрузить"}
        </button>

        <button
          type="button"
          className="button"
          disabled={pending !== null}
          title="Пакет dataarena.package/1: данные, схема, профиль, происхождение и рецепт"
          onClick={() =>
            void download("package", packageUrl(dataset.workspace_id, dataset.dataset_id), "dataset.dapkg")
          }
        >
          {pending === "package" ? (
            <Loader2 size={14} className="spin" aria-hidden />
          ) : (
            <Package size={14} aria-hidden />
          )}{" "}
          {pending === "package" ? "Собирается…" : "Пакет для ModelArena"}
        </button>
      </div>

      {error && (
        <div className="banner banner--error" role="alert">
          <p className="banner__title">Файл не получен</p>
          <p className="banner__text">{error}</p>
        </div>
      )}

    </section>
  );
}
