import { useCallback, useEffect, useMemo, useState } from "react";
import { Lightbulb, Link2, Loader2, Play, TriangleAlert } from "lucide-react";
import { describeError } from "../../api/client";
import {
  analyseJoin,
  createJoin,
  fetchDatasets,
  previewJoin,
  suggestJoinKeys,
} from "../../api/endpoints";
import type {
  Dataset,
  JoinAnalysis,
  JoinRequest,
  JoinSuggestion,
  KeyPair,
  Page,
} from "../../types/dataset";
import { formatCell, formatCount, formatRatio, plural } from "../../utils/format";
import { SmartAssembly } from "./SmartAssembly";

export type DatasetBuilderProps = {
  workspaceId: string | null;
  onDatasetCreated: (dataset: Dataset) => void;
};

const KINDS: { value: string; label: string; hint: string }[] = [
  { value: "inner", label: "Только совпавшие", hint: "строки, у которых пара нашлась с обеих сторон" },
  { value: "left", label: "Все слева", hint: "все строки левого датасета, пара — если нашлась" },
  { value: "right", label: "Все справа", hint: "все строки правого датасета" },
  { value: "full", label: "Все с обеих сторон", hint: "ничего не теряется" },
];

export function DatasetBuilder({ workspaceId, onDatasetCreated }: DatasetBuilderProps) {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [left, setLeft] = useState<string>("");
  const [right, setRight] = useState<string>("");
  const [keys, setKeys] = useState<KeyPair[]>([]);
  const [kind, setKind] = useState("inner");
  const [suffix, setSuffix] = useState("_2");
  const [name, setName] = useState("");
  const [suggestions, setSuggestions] = useState<JoinSuggestion[]>([]);
  const [analysis, setAnalysis] = useState<JoinAnalysis | null>(null);
  const [preview, setPreview] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!workspaceId) {
      return;
    }

    const controller = new AbortController();

    fetchDatasets(workspaceId, controller.signal)
      .then((body) => {
        setDatasets(body.datasets);
        setLeft((previous) => previous || (body.datasets[0]?.dataset_id ?? ""));
        setRight((previous) => previous || (body.datasets[1]?.dataset_id ?? ""));
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(describeError(cause, "Не удалось загрузить список датасетов."));
        }
      });

    return () => controller.abort();
  }, [workspaceId]);

  const leftDataset = datasets.find((item) => item.dataset_id === left) ?? null;
  const rightDataset = datasets.find((item) => item.dataset_id === right) ?? null;

  const request = useMemo<JoinRequest | null>(
    () =>
      left && right && keys.length > 0
        ? {
            left_dataset_id: left,
            right_dataset_id: right,
            keys,
            kind,
            right_suffix: suffix,
          }
        : null,
    [left, right, keys, kind, suffix],
  );

  //#смена сторон обнуляет всё, что относилось к прежней паре: показывать анализ
  //#от другого соединения — значит соврать о текущем
  useEffect(() => {
    setKeys([]);
    setAnalysis(null);
    setPreview(null);
    setSuggestions([]);
  }, [left, right]);

  const ask = useCallback(async () => {
    if (!workspaceId || !left || !right) {
      return;
    }

    setBusy(true);

    try {
      const body = await suggestJoinKeys(workspaceId, left, right);
      setSuggestions(body.suggestions);
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "Не удалось подобрать ключи."));
    } finally {
      setBusy(false);
    }
  }, [workspaceId, left, right]);

  const check = useCallback(async () => {
    if (!workspaceId || !request) {
      return;
    }

    setBusy(true);

    try {
      const [measured, page] = await Promise.all([
        analyseJoin(workspaceId, request),
        previewJoin(workspaceId, request),
      ]);
      setAnalysis(measured);
      setPreview(page);
      setError(null);
    } catch (cause) {
      setAnalysis(null);
      setPreview(null);
      setError(describeError(cause, "Соединение проверить не удалось."));
    } finally {
      setBusy(false);
    }
  }, [workspaceId, request]);

  const run = useCallback(async () => {
    if (!workspaceId || !request) {
      return;
    }

    setBusy(true);

    try {
      const dataset = await createJoin(workspaceId, {
        ...request,
        name: name.trim() || `${leftDataset?.name ?? "слева"} + ${rightDataset?.name ?? "справа"}`,
      });
      setError(null);
      onDatasetCreated(dataset);
    } catch (cause) {
      setError(describeError(cause, "Соединение не выполнено."));
    } finally {
      setBusy(false);
    }
  }, [workspaceId, request, name, leftDataset, rightDataset, onDatasetCreated]);

  if (error && datasets.length < 2) {
    //#список не загрузился: совет «загрузите второй файл» был бы неправдой
    return (
      <div className="banner banner--error" role="alert">
        <p className="banner__title">Не получилось</p>
        <p className="banner__text">{error}</p>
      </div>
    );
  }

  if (!workspaceId || datasets.length < 2) {
    return (
      <div className="empty-state">
        <h2 className="empty-state__title">Нужно минимум два датасета</h2>
        <p className="empty-state__text">
          Загрузите второй файл в разделе «Файлы» — соединять пока нечего.
        </p>
      </div>
    );
  }

  return (
    <div className="builder">
      {error && (
        <div className="banner banner--error" role="alert">
          <p className="banner__title">Не получилось</p>
          <p className="banner__text">{error}</p>
        </div>
      )}

      <SmartAssembly
        workspaceId={workspaceId}
        onUse={(leftId, rightId, candidate) => {
          setLeft(leftId);
          setRight(rightId);
          setKeys(candidate.keys.map((key) => ({ left: key.left, right: key.right })));
          setAnalysis(null);
          setPreview(null);
        }}
      />

      <div className="builder__sides">
        <SidePicker label="Слева" datasets={datasets} value={left} onChange={setLeft} />
        <Link2 size={16} aria-hidden className="builder__link" />
        <SidePicker label="Справа" datasets={datasets} value={right} onChange={setRight} />
      </div>

      <div className="builder__keys">
        <div className="builder__keys-head">
          <h2>Ключ соединения</h2>
          <button type="button" className="button" disabled={busy} onClick={() => void ask()}>
            <Lightbulb size={14} aria-hidden /> Подобрать
          </button>
        </div>

        {suggestions.length > 0 && (
          <ul className="builder__suggestions">
            {suggestions.slice(0, 5).map((item) => (
              <li key={`${item.left}:${item.right}`}>
                <button
                  type="button"
                  className="suggestion"
                  onClick={() => setKeys([{ left: item.left, right: item.right }])}
                >
                  <span className="suggestion__pair">
                    {item.left} = {item.right}
                  </span>
                  <span className={`suggestion__strength suggestion__strength--${item.strength}`}>
                    уверенность {item.strength} · связь {item.expected_relation}
                  </span>
                  <span className="suggestion__reasons">{item.reasons.join(" ")}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <KeyEditor
          left={leftDataset}
          right={rightDataset}
          keys={keys}
          onChange={setKeys}
        />
      </div>

      <div className="builder__options">
        <label className="step-form__field">
          <span>Что оставить</span>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            {KINDS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} — {item.hint}
              </option>
            ))}
          </select>
        </label>

        <label className="step-form__field">
          <span>Суффикс для одинаковых имён</span>
          <input value={suffix} onChange={(event) => setSuffix(event.target.value)} />
        </label>

        <label className="step-form__field">
          <span>Имя результата</span>
          <input
            value={name}
            placeholder={`${leftDataset?.name ?? ""} + ${rightDataset?.name ?? ""}`}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      </div>

      <div className="builder__actions">
        <button
          type="button"
          className="button"
          disabled={!request || busy}
          onClick={() => void check()}
        >
          {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null} Проверить
        </button>
        <button
          type="button"
          className="button button--primary"
          //#соединение нельзя выполнить, не посмотрев на прогноз: именно в нём написано,
          //#во что превратится датасет
          disabled={!analysis || busy}
          onClick={() => void run()}
        >
          <Play size={14} aria-hidden /> Создать датасет
        </button>
      </div>

      {analysis && <AnalysisPanel analysis={analysis} />}
      {preview && <PreviewTable page={preview} />}
    </div>
  );
}

function SidePicker({
  label,
  datasets,
  value,
  onChange,
}: {
  label: string;
  datasets: Dataset[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="step-form__field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {datasets.map((dataset) => (
          <option key={dataset.dataset_id} value={dataset.dataset_id}>
            {dataset.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function KeyEditor({
  left,
  right,
  keys,
  onChange,
}: {
  left: Dataset | null;
  right: Dataset | null;
  keys: KeyPair[];
  onChange: (keys: KeyPair[]) => void;
}) {
  const leftColumns = left?.schema.columns.map((column) => column.name) ?? [];
  const rightColumns = right?.schema.columns.map((column) => column.name) ?? [];
  const rows = keys.length > 0 ? keys : [{ left: leftColumns[0] ?? "", right: rightColumns[0] ?? "" }];

  return (
    <div className="builder__key-rows">
      {rows.map((pair, index) => (
        <div key={index} className="builder__key-row">
          <select
            value={pair.left}
            onChange={(event) => {
              const next = [...rows];
              next[index] = { ...pair, left: event.target.value };
              onChange(next);
            }}
          >
            {leftColumns.map((column) => (
              <option key={column} value={column}>
                {column}
              </option>
            ))}
          </select>
          <span aria-hidden>=</span>
          <select
            value={pair.right}
            onChange={(event) => {
              const next = [...rows];
              next[index] = { ...pair, right: event.target.value };
              onChange(next);
            }}
          >
            {rightColumns.map((column) => (
              <option key={column} value={column}>
                {column}
              </option>
            ))}
          </select>
          {rows.length > 1 && (
            <button
              type="button"
              className="button"
              onClick={() => onChange(rows.filter((_, position) => position !== index))}
            >
              Убрать
            </button>
          )}
        </div>
      ))}

      <button
        type="button"
        className="button"
        onClick={() =>
          onChange([...rows, { left: leftColumns[0] ?? "", right: rightColumns[0] ?? "" }])
        }
      >
        Добавить колонку ключа
      </button>
    </div>
  );
}

function AnalysisPanel({ analysis }: { analysis: JoinAnalysis }) {
  return (
    <section className={`analysis${analysis.row_explosion ? " analysis--explosion" : ""}`}>
      <h2 className="analysis__title">
        Получится {formatCount(analysis.estimated_rows)}{" "}
        {plural(analysis.estimated_rows, "строка", "строки", "строк")} · связь{" "}
        {analysis.cardinality}
      </h2>

      <dl className="column-stats__grid">
        <div>
          <dt>слева</dt>
          <dd>
            {formatCount(analysis.left_rows)} · без пары {formatCount(analysis.unmatched_left_rows)}
          </dd>
        </div>
        <div>
          <dt>справа</dt>
          <dd>
            {formatCount(analysis.right_rows)} · без пары{" "}
            {formatCount(analysis.unmatched_right_rows)}
          </dd>
        </div>
        {analysis.keys.map((key) => (
          <div key={`${key.pair.left}:${key.pair.right}`}>
            <dt>
              {key.pair.left} = {key.pair.right}
            </dt>
            <dd>
              {key.left_type} / {key.right_type} · уникальность{" "}
              {formatRatio(key.left_unique_ratio)} и {formatRatio(key.right_unique_ratio)}
            </dd>
          </div>
        ))}
      </dl>

      {analysis.warnings.length > 0 && (
        <ul className="analysis__warnings">
          {analysis.warnings.map((warning) => (
            <li key={warning}>
              <TriangleAlert size={13} aria-hidden /> {warning}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PreviewTable({ page }: { page: Page }) {
  return (
    <section className="builder__preview">
      <h2>Как это будет выглядеть</h2>
      <div className="builder__preview-scroll">
        <table className="preview-table">
          <thead>
            <tr>
              {page.columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {page.rows.slice(0, 10).map((row, index) => (
              <tr key={index}>
                {page.columns.map((column) => (
                  <td key={column}>{formatCell(row[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
