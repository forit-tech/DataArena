import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Loader2,
  Plus,
  Redo2,
  Trash2,
  TriangleAlert,
  Undo2,
} from "lucide-react";
import { describeError } from "../../api/client";
import {
  fetchOperations,
  fetchPipeline,
  fetchStepImpact,
  redoPipeline,
  savePipeline,
  undoPipeline,
} from "../../api/endpoints";
import type {
  Dataset,
  OperationVocabulary,
  PipelineStep,
  PipelineView,
  StepImpact,
} from "../../types/dataset";
import { formatCount, plural } from "../../utils/format";
import { DatasetTable } from "../dataset-table/DatasetTable";
import { ExportPanel } from "./ExportPanel";
import { StepForm } from "./StepForm";
import type { DraftStep } from "./StepForm";

export type PipelineEditorProps = {
  dataset: Dataset;
};

function toPayload(step: PipelineStep | DraftStep) {
  return { step_id: step.step_id, kind: step.kind, params: step.params };
}

export function PipelineEditor({ dataset }: PipelineEditorProps) {
  const [view, setView] = useState<PipelineView | null>(null);
  const [vocabulary, setVocabulary] = useState<OperationVocabulary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorTitle, setErrorTitle] = useState("Шаг не применён");
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  //#последствия черновика: считаются на сервере по данным, а не угадываются в браузере
  const [impact, setImpact] = useState<StepImpact | null>(null);
  //#таблица показывает либо исходник, либо результат шагов: «до» и «после» — это один
  //#и тот же экран с переключателем, а не два разных представления данных
  const [side, setSide] = useState<"after" | "before">("after");

  const load = useCallback(async () => {
    try {
      setView(await fetchPipeline(dataset.workspace_id, dataset.dataset_id));
      setError(null);
    } catch (cause) {
      setErrorTitle("Конвейер не загрузился");
      setError(describeError(cause, "Не удалось загрузить конвейер."));
    }
  }, [dataset.workspace_id, dataset.dataset_id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();

    fetchOperations(controller.signal)
      .then(setVocabulary)
      .catch((cause: unknown) => {
        //#без словаря операций форма добавления не показывается: выдумывать список
        //#операций в браузере нельзя — он разойдётся с сервером. Но и молчать нельзя
        if (!controller.signal.aborted) {
          setErrorTitle("Список операций не загрузился");
          setError(describeError(cause, "Не удалось получить список операций."));
        }
      });

    return () => controller.abort();
  }, []);

  //#пустой список создаётся заново на каждый рендер и меняет зависимости обработчиков
  const steps = useMemo(() => view?.steps ?? [], [view]);

  const commit = useCallback(
    async (next: PipelineStep[] | DraftStep[]) => {
      setBusy(true);

      try {
        setView(
          await savePipeline(
            dataset.workspace_id,
            dataset.dataset_id,
            next.map((step) => toPayload(step)),
          ),
        );
        setError(null);
        return true;
      } catch (cause) {
        //#отказ показывается как есть: именно здесь приходит «шаг 2 не может быть
        //#выполнен: нет колонок такой-то» — сообщение, ради которого проверка и делалась
        setErrorTitle("Шаг не применён");
        setError(describeError(cause, "Конвейер не сохранён."));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [dataset.workspace_id, dataset.dataset_id],
  );

  const move = useCallback(
    async (index: number, direction: -1 | 1) => {
      const next = [...steps];
      const target = index + direction;

      if (target < 0 || target >= next.length) {
        return;
      }

      [next[index], next[target]] = [next[target]!, next[index]!];
      await commit(next);
    },
    [steps, commit],
  );

  const remove = useCallback(
    async (index: number) => {
      await commit(steps.filter((_, position) => position !== index));
    },
    [steps, commit],
  );

  const history = useCallback(
    async (action: "undo" | "redo") => {
      setBusy(true);

      try {
        const call = action === "undo" ? undoPipeline : redoPipeline;
        setView(await call(dataset.workspace_id, dataset.dataset_id));
        setError(null);
      } catch (cause) {
        setErrorTitle("История не изменена");
        setError(describeError(cause, "Не удалось изменить историю конвейера."));
      } finally {
        setBusy(false);
      }
    },
    [dataset.workspace_id, dataset.dataset_id],
  );

  const previewImpact = useCallback(
    async (draft: DraftStep) => {
      try {
        setImpact(
          await fetchStepImpact(
            dataset.workspace_id,
            dataset.dataset_id,
            toPayload(draft),
            steps.map((step) => toPayload(step)),
          ),
        );
      } catch (cause) {
        setImpact(null);
        setErrorTitle("Последствия не оценены");
        setError(describeError(cause, "Не удалось оценить последствия шага."));
      }
    },
    [dataset.workspace_id, dataset.dataset_id, steps],
  );

  const columns = useMemo(
    () => view?.result_columns ?? dataset.schema.columns.map((column) => column.name),
    [view, dataset.schema.columns],
  );

  return (
    <div className="pipeline">
      <div className="pipeline__head">
        <div className="pipeline__title">
          <h2>Шаги</h2>
          <p className="pipeline__hint">
            Исходный файл не меняется: шаги применяются заново при каждом показе.
          </p>
        </div>

        <div className="pipeline__history">
          <button
            type="button"
            className="button"
            disabled={!view?.can_undo || busy}
            onClick={() => void history("undo")}
          >
            <Undo2 size={14} aria-hidden /> Отменить
          </button>
          <button
            type="button"
            className="button"
            disabled={!view?.can_redo || busy}
            onClick={() => void history("redo")}
          >
            <Redo2 size={14} aria-hidden /> Повторить
          </button>
        </div>
      </div>

      {error && (
        <div className="banner banner--error" role="alert">
          <p className="banner__title">{errorTitle}</p>
          <p className="banner__text">{error}</p>
        </div>
      )}

      {steps.length === 0 && !adding && (
        <p className="pipeline__empty">
          Шагов пока нет. Данные показываются как есть, ничего не изменено.
        </p>
      )}

      <ol className="pipeline__steps">
        {steps.map((step, index) => (
          <li key={step.step_id} className="pipeline-step">
            <span className="pipeline-step__index tabular">{index + 1}</span>

            <div className="pipeline-step__body">
              <p className="pipeline-step__title">
                {step.title}
                {step.destructive && (
                  <span className="pipeline-step__destructive" title="Шаг убирает данные">
                    <TriangleAlert size={12} aria-hidden /> убирает данные
                  </span>
                )}
              </p>
              <p className="pipeline-step__description">{step.description}</p>
              {(step.added_columns.length > 0 || step.removed_columns.length > 0) && (
                <p className="pipeline-step__columns">
                  {step.added_columns.length > 0 && `+ ${step.added_columns.join(", ")}`}
                  {step.added_columns.length > 0 && step.removed_columns.length > 0 && " · "}
                  {step.removed_columns.length > 0 && `− ${step.removed_columns.join(", ")}`}
                </p>
              )}
            </div>

            <div className="pipeline-step__actions">
              <button
                type="button"
                className="button"
                disabled={index === 0 || busy}
                aria-label={`Поднять шаг ${index + 1}`}
                onClick={() => void move(index, -1)}
              >
                <ArrowUp size={13} aria-hidden />
              </button>
              <button
                type="button"
                className="button"
                disabled={index === steps.length - 1 || busy}
                aria-label={`Опустить шаг ${index + 1}`}
                onClick={() => void move(index, 1)}
              >
                <ArrowDown size={13} aria-hidden />
              </button>
              <button
                type="button"
                className="button"
                disabled={busy}
                aria-label={`Удалить шаг ${index + 1}`}
                onClick={() => void remove(index)}
              >
                <Trash2 size={13} aria-hidden />
              </button>
            </div>
          </li>
        ))}
      </ol>

      {adding && vocabulary ? (
        <StepForm
          vocabulary={vocabulary}
          columns={columns}
          types={view?.result_types ?? {}}
          impact={impact}
          onPreview={(draft) => void previewImpact(draft)}
          onCancel={() => {
            setAdding(false);
            setImpact(null);
          }}
          onSubmit={async (draft) => {
            const saved = await commit([...steps, draft]);

            if (saved) {
              setAdding(false);
              setImpact(null);
            }
          }}
        />
      ) : (
        <button
          type="button"
          className="button button--primary"
          disabled={!vocabulary || busy}
          onClick={() => setAdding(true)}
        >
          <Plus size={14} aria-hidden /> Добавить шаг
        </button>
      )}

      <ExportPanel dataset={dataset} />

      <div className="pipeline__result">
        <div className="pipeline__result-head">
          <h2>{side === "after" ? "Результат" : "Исходные данные"}</h2>
          <div className="pipeline__sides">
            <button
              type="button"
              className="button"
              aria-pressed={side === "before"}
              onClick={() => setSide("before")}
            >
              До
            </button>
            <button
              type="button"
              className="button"
              aria-pressed={side === "after"}
              onClick={() => setSide("after")}
            >
              После
            </button>
          </div>
          {busy && <Loader2 size={14} className="spin" aria-hidden />}
        </div>

        <p className="pipeline__columns">
          {formatCount(columns.length)}{" "}
          {plural(columns.length, "колонка", "колонки", "колонок")} после шагов
        </p>

        <DatasetTable
          key={`${side}:${steps.length}:${view?.can_undo}:${view?.can_redo}`}
          dataset={dataset}
          source={side === "after" ? "pipeline" : "dataset"}
          resultTypes={side === "after" ? view?.result_types : undefined}
        />
      </div>
    </div>
  );
}
