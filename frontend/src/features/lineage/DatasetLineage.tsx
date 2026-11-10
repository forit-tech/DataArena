import { useEffect, useState } from "react";
import { ArrowDown, CircleHelp, FileText, GitMerge, TriangleAlert } from "lucide-react";
import { describeError } from "../../api/client";
import { fetchLineage } from "../../api/endpoints";
import type {
  ColumnHistory,
  ColumnRef,
  Dataset,
  LineageParent,
  LineageView,
  LineageWarning,
} from "../../types/dataset";
import { formatCount } from "../../utils/format";

export type DatasetLineageProps = {
  dataset: Dataset;
};

const UNKNOWN_REASON: Record<string, string> = {
  missing_record: "датасет создан до того, как появились записи о происхождении",
  corrupted_record: "запись о происхождении повреждена и не читается",
  oversized_record: "запись о происхождении больше допустимого размера",
};

const WARNING_TEXT: Record<LineageWarning, string> = {
  provenance_unknown: "Происхождение колонок неизвестно: не известно, как создан датасет.",
  join_column_map_missing:
    "Датасет собран до появления карты колонок: какая колонка пришла с какой стороны, неизвестно.",
  join_parent_snapshots_missing:
    "Датасет собран до появления снимков родителей: их состояние на момент сборки неизвестно.",
  parent_history_truncated:
    "История колонок родителя была слишком большой и сохранена без шагов — только источники.",
};

const ROLE_LABEL: Record<LineageParent["role"], string> = { left: "LEFT", right: "RIGHT" };

//#откуда пришли значения: файл, если он известен, иначе датасет
function refSource(ref: ColumnRef): string {
  return ref.source_file ?? ref.dataset_name;
}

function sourceCell(column: ColumnHistory): string {
  if (column.origin === "unknown" || column.origins.length === 0) {
    return "неизвестно";
  }

  return [...new Set(column.origins.map(refSource))].join(", ");
}

function sourceColumnCell(column: ColumnHistory): string {
  if (column.origin === "unknown" || column.origins.length === 0) {
    return "—";
  }

  return column.origins.map((ref) => ref.column).join(", ");
}

export function DatasetLineage({ dataset }: DatasetLineageProps) {
  const [view, setView] = useState<LineageView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    setView(null);
    setError(null);
    setSelected(null);

    fetchLineage(dataset.workspace_id, dataset.dataset_id, controller.signal)
      .then(setView)
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(describeError(cause, "Не удалось получить происхождение."));
        }
      });

    return () => controller.abort();
  }, [dataset.workspace_id, dataset.dataset_id]);

  if (error) {
    return (
      <div className="banner banner--error" role="alert">
        <p className="banner__title">Не удалось показать происхождение</p>
        <p className="banner__text">{error}</p>
      </div>
    );
  }

  if (!view) {
    return <p className="lineage__loading">Собирается…</p>;
  }

  const column = view.columns.find((item) => item.name === selected) ?? null;

  return (
    <div className="lineage">
      <Origin view={view} />

      <section className="lineage__block">
        <h2 className="lineage__title">
          Шаги этого датасета <span className="lineage__count">{formatCount(view.events.length)}</span>
        </h2>

        {view.events.length === 0 ? (
          <p className="lineage__text lineage__muted">Шагов нет: датасет в том виде, в каком создан.</p>
        ) : (
          <ol className="lineage__steps">
            {view.events.map((event) => (
              <li key={event.step_id || event.position}>
                <span>{event.description}</span>
                <span className="lineage__muted">
                  {event.changes.length > 0 ? ` · меняет: ${event.changes.join(", ")}` : " · значения не меняет"}
                  {event.reads.length > 0 ? ` · читает: ${event.reads.join(", ")}` : ""}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="lineage__block">
        <h2 className="lineage__title">
          Колонки <span className="lineage__count">{formatCount(view.columns.length)}</span>
        </h2>

        <div className="lineage__scroll">
          <table className="preview-table">
            <thead>
              <tr>
                <th>Колонка</th>
                <th>Откуда</th>
                <th>Исходная колонка</th>
                <th title="Шаги конвейеров, менявшие значения или имя колонки; соединение не считается">
                  Шагов изменили
                </th>
              </tr>
            </thead>
            <tbody>
              {view.columns.map((item) => (
                <tr
                  key={item.name}
                  className={item.name === selected ? "lineage__row--selected" : undefined}
                >
                  <td>
                    <button
                      type="button"
                      className="lineage__column"
                      aria-pressed={item.name === selected}
                      onClick={() => setSelected(item.name === selected ? null : item.name)}
                    >
                      {item.name}
                    </button>
                    {item.origin === "step" && <span className="lineage__badge">создана шагом</span>}
                  </td>
                  <td className={item.origin === "unknown" ? "lineage__cell--unknown" : undefined}>
                    {sourceCell(item)}
                  </td>
                  <td>{sourceColumnCell(item)}</td>
                  <td className="tabular">
                    {item.events.filter((event) => event.kind === "step").length}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {column && <ColumnChain column={column} datasetName={view.name} steps={view.events.length} />}
      </section>
    </div>
  );
}

function Origin({ view }: { view: LineageView }) {
  if (view.status === "legacy_unknown") {
    return (
      <section className="lineage__block">
        <h2 className="lineage__title">Происхождение недоступно</h2>
        <p className="lineage__text lineage__text--unknown">
          <CircleHelp size={14} aria-hidden />
          Как появился этот датасет, неизвестно: {UNKNOWN_REASON[view.unknown_reason ?? ""] ?? "записи нет"}.
        </p>
        <p className="lineage__hash tabular" title="Отпечаток байтов датасета">
          {view.artifact.file_name} · sha256 {view.artifact.sha256.slice(0, 16)}…
        </p>
      </section>
    );
  }

  const join = view.join;

  return (
    <section className="lineage__block">
      <h2 className="lineage__title">Откуда взялся датасет</h2>

      {join ? (
        <>
          <p className="lineage__text">
            <GitMerge size={14} aria-hidden /> Создан соединением <strong>{join.kind.toUpperCase()} JOIN</strong>
          </p>

          <ul className="lineage__parents">
            {view.parents.map((parent) => (
              <li
                key={parent.role}
                className={`lineage__node${parent.current.available ? "" : " lineage__node--missing"}`}
              >
                <span className="lineage__role">{ROLE_LABEL[parent.role]}</span>
                {parent.current.available ? <FileText size={13} aria-hidden /> : <TriangleAlert size={13} aria-hidden />}
                {parent.dataset_name || parent.dataset_id}
                {!parent.current.available && <span className="lineage__missing">датасета больше нет</span>}
                {parent.current.changed_since_join && (
                  <span className="lineage__missing">изменён после сборки — история показана на момент сборки</span>
                )}
                {parent.snapshot && parent.snapshot.steps.length > 0 && (
                  <span className="lineage__muted">
                    {" "}
                    · на момент сборки: {parent.snapshot.steps.map((step) => step.description).join(" → ")}
                  </span>
                )}
              </li>
            ))}
          </ul>

          <p className="lineage__join">
            Ключ:{" "}
            {join.keys
              .map((key) => `${view.parents[0]?.dataset_name ?? "left"}.${key.left} = ${view.parents[1]?.dataset_name ?? "right"}.${key.right}`)
              .join(" AND ")}
          </p>
        </>
      ) : (
        <p className="lineage__text">
          Загружен файлом <strong>{view.artifact.file_name}</strong> ({view.artifact.format}).
        </p>
      )}

      <p className="lineage__hash tabular" title="Отпечаток байтов датасета">
        sha256 {view.artifact.sha256.slice(0, 16)}…
      </p>

      {view.warnings.length > 0 && (
        <ul className="lineage__warnings">
          {view.warnings.map((warning) => (
            <li key={warning}>
              <TriangleAlert size={13} aria-hidden /> {WARNING_TEXT[warning] ?? warning}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ColumnChain({
  column,
  datasetName,
  steps,
}: {
  column: ColumnHistory;
  datasetName: string;
  steps: number;
}) {
  return (
    <div className="lineage__detail" aria-label={`История колонки ${column.name}`}>
      <h3 className="lineage__subtitle">{column.name}</h3>

      {column.origin === "unknown" || column.origins.length === 0 ? (
        <p className="lineage__text lineage__text--unknown">
          <CircleHelp size={14} aria-hidden /> Откуда пришла колонка, неизвестно.
        </p>
      ) : (
        <ol className="lineage__chain-list">
          {column.origins.map((ref) => (
            <li key={`${ref.dataset_id}:${ref.column}`} className="lineage__link lineage__link--source">
              {refSource(ref)} · {ref.column}
            </li>
          ))}
          {column.events.map((event, index) => (
            <li key={`${event.dataset_id}:${event.position}:${index}`} className="lineage__link">
              <ArrowDown size={12} aria-hidden />
              <span>{event.description}</span>
              <span className="lineage__muted"> · {event.dataset_name}</span>
            </li>
          ))}
          <li className="lineage__link lineage__link--result">
            <ArrowDown size={12} aria-hidden /> {datasetName}.{column.name}
          </li>
        </ol>
      )}

      {column.read_by.length > 0 && (
        <p className="lineage__text lineage__muted">
          Только читали (значения не меняли): шаг {column.read_by.join(", ")} из {steps}.
        </p>
      )}
    </div>
  );
}
