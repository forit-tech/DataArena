import { useEffect, useState } from "react";
import { Bot, Check, Loader2, Sparkles } from "lucide-react";
import { describeError } from "../../api/client";
import { applyRepair, askAiAboutFinding, fetchRepair, previewRepair } from "../../api/endpoints";
import type { AiFindingAdvice } from "../../types/ai";
import { ModelPicker } from "../ai/ModelPicker";
import { useAiModel } from "../ai/useAiModel";
import type { RepairAdvice, RepairApplied, RepairPreview, RepairSafety } from "../../types/health";
import { formatCount, plural } from "../../utils/format";

//#подписи безопасности — смысловые, а не цветовые: человек должен понимать,
//#что именно произойдёт с данными, до нажатия
const SAFETY: Record<RepairSafety, { label: string; className: string }> = {
  safe: { label: "Ничего не теряется", className: "repair-safety--safe" },
  lossy: { label: "Удаляет строки или значения", className: "repair-safety--lossy" },
  decision: { label: "Нужно ваше решение", className: "repair-safety--decision" },
};

const CONFIDENCE: Record<string, string> = { low: "низкая", medium: "средняя", high: "высокая" };

export type RepairPanelProps = {
  workspaceId: string;
  datasetId: string;
  findingId: string;
  onApplied: (applied: RepairApplied) => void;
  onSkip: () => void;
};

export function RepairPanel({ workspaceId, datasetId, findingId, onApplied, onSkip }: RepairPanelProps) {
  const [advice, setAdvice] = useState<RepairAdvice | null>(null);
  const [optionId, setOptionId] = useState<string | null>(null);
  const [preview, setPreview] = useState<RepairPreview | null>(null);
  const [busy, setBusy] = useState<"advice" | "preview" | "apply" | null>("advice");
  const [error, setError] = useState<string | null>(null);
  const ai = useAiModel();
  const [aiAdvice, setAiAdvice] = useState<AiFindingAdvice | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);

  const askAi = async () => {
    setAiBusy(true);
    setAiError(null);
    try {
      const result = await askAiAboutFinding(workspaceId, datasetId, findingId, ai.model);
      setAiAdvice(result);
      //#вариант, выбранный моделью, подсвечивается и сразу попадает в предпросмотр:
      //#он уже проверен по списку, а применять его всё равно решает человек
      if (result.recommended_option !== "none") setOptionId(result.recommended_option);
    } catch (reason) {
      setAiError(describeError(reason, "Локальная модель не ответила."));
    } finally {
      setAiBusy(false);
    }
  };

  useEffect(() => {
    let alive = true;
    setBusy("advice");
    fetchRepair(workspaceId, datasetId, findingId)
      .then((result) => {
        if (!alive) return;
        setAdvice(result);
        //#первым выбран безопасный вариант, если он есть: самое частое и наименее рискованное
        const first = result.options.find((option) => option.safety === "safe") ?? result.options[0];
        setOptionId(first?.option_id ?? null);
      })
      .catch((reason: unknown) => alive && setError(describeError(reason, "Не удалось получить совет.")))
      .finally(() => alive && setBusy(null));
    return () => {
      alive = false;
    };
  }, [workspaceId, datasetId, findingId]);

  useEffect(() => {
    if (!optionId) {
      setPreview(null);
      return;
    }
    let alive = true;
    setBusy("preview");
    setPreview(null);
    previewRepair(workspaceId, datasetId, findingId, optionId)
      .then((result) => alive && setPreview(result))
      .catch((reason: unknown) => alive && setError(describeError(reason, "Не удалось построить предпросмотр.")))
      .finally(() => alive && setBusy(null));
    return () => {
      alive = false;
    };
  }, [workspaceId, datasetId, findingId, optionId]);

  const apply = async () => {
    if (!optionId || !preview) return;
    setBusy("apply");
    setError(null);
    try {
      onApplied(await applyRepair(workspaceId, datasetId, findingId, optionId, preview.steps_fingerprint));
    } catch (reason) {
      setError(describeError(reason, "Исправление не применено."));
      setBusy(null);
    }
  };

  if (busy === "advice") {
    return (
      <div className="repair" role="status">
        <Loader2 size={14} className="spin" aria-hidden /> Готовим совет…
      </div>
    );
  }

  if (!advice) {
    return <div className="repair repair__error">{error}</div>;
  }

  const option = advice.options.find((item) => item.option_id === optionId) ?? null;

  return (
    <div className="repair">
      <dl className="repair__explain">
        <dt>Причина</dt>
        <dd>{advice.explanation.cause}</dd>
        <dt>Чем грозит</dt>
        <dd>{advice.explanation.consequence}</dd>
        <dt>Что делать</dt>
        <dd>{advice.explanation.approach}</dd>
      </dl>
      <p className="repair__source">
        <Sparkles size={12} aria-hidden /> Совет по правилам DataArena — без внешней модели
      </p>

      {ai.available && (
        <div className="ai-box">
          <div className="ai-box__head">
            <button type="button" className="button" disabled={aiBusy} onClick={() => void askAi()}>
              {aiBusy ? <Loader2 size={14} className="spin" aria-hidden /> : <Bot size={14} aria-hidden />}
              Спросить локальную модель
            </button>
            <ModelPicker models={ai.status?.models ?? []} value={ai.model} onChange={ai.setModel} />
            {aiBusy && <span className="repair__hint">Модель работает на этом компьютере — обычно 10–40 секунд.</span>}
          </div>
          {aiError && <p className="repair__error">{aiError}</p>}
          {aiAdvice && (
            <div className="ai-answer" data-testid="ai-answer">
              <p className="ai-answer__label">
                ИИ · {aiAdvice.model.split("/").pop()} · {aiAdvice.seconds} с · уверенность: {CONFIDENCE[aiAdvice.confidence]}
              </p>
              <p>{aiAdvice.summary}</p>
              {aiAdvice.reasoning && <p className="decision-muted">{aiAdvice.reasoning}</p>}
              {aiAdvice.risks.length > 0 && (
                <ul className="ai-answer__risks">
                  {aiAdvice.risks.map((risk) => (
                    <li key={risk}>{risk}</li>
                  ))}
                </ul>
              )}
              <p className="decision-muted">
                {aiAdvice.recommended_option === "none"
                  ? "Модель советует ничего не менять без вашего решения."
                  : "Выбранный моделью вариант подсвечен ниже — применять или нет, решаете вы."}
              </p>
              {aiAdvice.rejected.map((note) => (
                <p key={note} className="target__warning">
                  Проверка: {note}
                </p>
              ))}
            </div>
          )}
        </div>
      )}

      {advice.options.length === 0 ? (
        <p className="repair__manual">{advice.manual_reason}</p>
      ) : (
        <>
          <div className="repair__options" role="radiogroup" aria-label="Варианты исправления">
            {advice.options.map((item) => (
              <button
                key={item.option_id}
                type="button"
                role="radio"
                aria-checked={item.option_id === optionId}
                className="repair-option"
                onClick={() => setOptionId(item.option_id)}
              >
                <span className="repair-option__title">{item.title}</span>
                <span className={`repair-safety ${SAFETY[item.safety].className}`}>{SAFETY[item.safety].label}</span>
                {item.note && <span className="repair-option__note">{item.note}</span>}
              </button>
            ))}
          </div>

          {busy === "preview" && (
            <p className="repair__status" role="status">
              <Loader2 size={14} className="spin" aria-hidden /> Считаем, что изменится…
            </p>
          )}

          {preview && option && (
            <div className="repair__preview" data-testid="repair-preview">
              <ul className="repair__steps">
                {option.steps.map((step, index) => (
                  <li key={`${step.kind}-${index}`}>{step.description}</li>
                ))}
              </ul>
              <p className="repair__numbers tabular">
                {formatCount(preview.changed_rows)} {plural(preview.changed_rows, "строка изменится", "строки изменятся", "строк изменится")}
                {preview.rows_removed > 0 && `, удалится ${formatCount(preview.rows_removed)} ${plural(preview.rows_removed, "строка", "строки", "строк")}`}
                {preview.values_lost > 0 && `, ${formatCount(preview.values_lost)} ${plural(preview.values_lost, "значение станет", "значения станут", "значений станут")} пропусками`}
                {preview.columns_removed.length > 0 && `, удалятся колонки: ${preview.columns_removed.join(", ")}`}
              </p>
              {preview.samples.length > 0 && (
                <table className="repair__samples">
                  <thead>
                    <tr>
                      <th>Было</th>
                      <th>Станет</th>
                      <th>Строк</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.samples.map((sample, index) => (
                      <tr key={index}>
                        <td className="tabular">{sample.before === null ? <em>пропуск</em> : `«${sample.before}»`}</td>
                        <td className="tabular">{sample.after === null ? <em>пропуск</em> : `«${sample.after}»`}</td>
                        <td className="tabular">{formatCount(sample.rows)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}

      {error && (
        <p className="repair__error" role="alert">
          {error}
        </p>
      )}

      <div className="repair__actions">
        {advice.options.length > 0 && (
          <button
            type="button"
            className="button button--primary"
            disabled={!preview || busy !== null}
            onClick={() => void apply()}
          >
            {busy === "apply" ? <Loader2 size={14} className="spin" aria-hidden /> : <Check size={14} aria-hidden />}
            Исправить
          </button>
        )}
        <button type="button" className="button button--ghost" onClick={onSkip}>
          Пропустить
        </button>
        <span className="repair__hint">Исходный файл не меняется: исправление — шаг конвейера, его можно откатить.</span>
      </div>
    </div>
  );
}
