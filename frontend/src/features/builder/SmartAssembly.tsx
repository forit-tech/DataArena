import { useState } from "react";
import { Bot, ChevronDown, Loader2, Sparkles, Target } from "lucide-react";
import { describeError } from "../../api/client";
import { askAiAboutPair, discoverAssembly } from "../../api/endpoints";
import type { AiAssemblyAdvice } from "../../types/ai";
import { ModelPicker } from "../ai/ModelPicker";
import { useAiModel } from "../ai/useAiModel";
import type { AssemblyDiscovery, PairVerdict, RelationCandidate, RelationVerdict } from "../../types/assembly";
import { formatCount, formatRatio, plural } from "../../utils/format";

const PAIR_VERDICT: Record<PairVerdict, { label: string; className: string }> = {
  join: { label: "Можно соединять", className: "verdict-chip--safe" },
  aggregate: { label: "Не соединять построчно", className: "verdict-chip--danger" },
  review: { label: "Проверить ключ", className: "verdict-chip--warn" },
  none: { label: "Связи нет", className: "verdict-chip--muted" },
};

const RELATION_VERDICT: Record<RelationVerdict, string> = {
  safe: "безопасно",
  weak: "сомнительно",
  dangerous: "размножит строки",
  none: "нет пар",
};

const TASK: Record<string, string> = { regression: "регрессия", classification: "классификация" };

export type SmartAssemblyProps = {
  workspaceId: string;
  //#подставить найденную связь в форму соединения: дальше работает обычная проверка и предпросмотр
  onUse: (leftId: string, rightId: string, candidate: RelationCandidate) => void;
};

export function SmartAssembly({ workspaceId, onUse }: SmartAssemblyProps) {
  const [result, setResult] = useState<AssemblyDiscovery | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openDataset, setOpenDataset] = useState<string | null>(null);
  const ai = useAiModel();
  const [aiAnswers, setAiAnswers] = useState<Record<string, AiAssemblyAdvice | string>>({});
  const [aiBusy, setAiBusy] = useState<string | null>(null);

  const askAi = async (pairKey: string, leftId: string, rightId: string) => {
    setAiBusy(pairKey);
    try {
      const answer = await askAiAboutPair(workspaceId, leftId, rightId, ai.model);
      setAiAnswers((previous) => ({ ...previous, [pairKey]: answer }));
    } catch (reason) {
      setAiAnswers((previous) => ({ ...previous, [pairKey]: describeError(reason, "Локальная модель не ответила.") }));
    } finally {
      setAiBusy(null);
    }
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await discoverAssembly(workspaceId));
    } catch (reason) {
      setError(describeError(reason, "Не удалось подобрать сборку."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="smart">
      <div className="smart__head">
        <button type="button" className="button button--primary" disabled={busy} onClick={() => void run()}>
          {busy ? <Loader2 size={14} className="spin" aria-hidden /> : <Sparkles size={14} aria-hidden />}
          {result ? "Подобрать заново" : "Подобрать сборку автоматически"}
        </button>
        {result && <span className="smart__note">{result.note}</span>}
        {result && ai.available && (
          <ModelPicker models={ai.status?.models ?? []} value={ai.model} onChange={ai.setModel} />
        )}
      </div>

      {error && (
        <p className="repair__error" role="alert">
          {error}
        </p>
      )}

      {result && (
        <>
          <ul className="smart__pairs">
            {result.pairs.map((pair) => {
              const verdict = PAIR_VERDICT[pair.verdict];
              return (
                <li key={`${pair.left_dataset_id}-${pair.right_dataset_id}`} className="smart-pair">
                  <div className="smart-pair__head">
                    <span className="smart-pair__names">
                      {pair.left_name} <span className="decision-muted">и</span> {pair.right_name}
                    </span>
                    <span className={`verdict-chip ${verdict.className}`}>{verdict.label}</span>
                  </div>
                  <p className="smart-pair__advice">{pair.advice}</p>
                  {ai.available && (
                    <AiPairAnswer
                      busy={aiBusy === `${pair.left_dataset_id}-${pair.right_dataset_id}`}
                      answer={aiAnswers[`${pair.left_dataset_id}-${pair.right_dataset_id}`]}
                      leftName={pair.left_name}
                      rightName={pair.right_name}
                      onAsk={() =>
                        void askAi(`${pair.left_dataset_id}-${pair.right_dataset_id}`, pair.left_dataset_id, pair.right_dataset_id)
                      }
                    />
                  )}
                  {pair.candidates.length > 0 && (
                    <table className="smart-pair__table">
                      <thead>
                        <tr>
                          <th>Ключ</th>
                          <th>Связь</th>
                          <th>Строк станет</th>
                          <th>Совпало</th>
                          <th>Оценка</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {pair.candidates.map((candidate) => (
                          <tr key={candidate.keys.map((k) => `${k.left}=${k.right}`).join("+")} title={candidate.explanation}>
                            <td>{candidate.keys.map((k) => (k.left === k.right ? k.left : `${k.left} = ${k.right}`)).join(" + ")}</td>
                            <td className="tabular">{candidate.cardinality}</td>
                            <td className="tabular">{formatCount(candidate.estimated_rows)}</td>
                            <td className="tabular">{formatRatio(candidate.match_share)}</td>
                            <td>
                              <span className={`relation relation--${candidate.verdict}`}>{RELATION_VERDICT[candidate.verdict]}</span>
                            </td>
                            <td>
                              {candidate.verdict !== "none" && (
                                <button
                                  type="button"
                                  className="button button--ghost"
                                  onClick={() => onUse(pair.left_dataset_id, pair.right_dataset_id, candidate)}
                                >
                                  Подставить
                                </button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </li>
              );
            })}
          </ul>

          <h3 className="smart__subtitle">Для чего подходят датасеты</h3>
          <ul className="smart__datasets">
            {result.datasets.map((dataset) => {
              const open = openDataset === dataset.dataset_id;
              return (
                <li key={dataset.dataset_id} className="smart-dataset">
                  <button
                    type="button"
                    className="smart-dataset__toggle"
                    aria-expanded={open}
                    onClick={() => setOpenDataset(open ? null : dataset.dataset_id)}
                  >
                    <span className="smart-dataset__name">{dataset.name}</span>
                    <span className="decision-muted tabular">
                      {formatCount(dataset.rows)} {plural(dataset.rows, "строка", "строки", "строк")}
                    </span>
                    <span className="smart-dataset__purposes">
                      {dataset.purposes.map((purpose) => (
                        <span key={purpose.purpose} className="purpose-chip" title={purpose.reason}>
                          {purpose.title}
                        </span>
                      ))}
                    </span>
                    <ChevronDown size={15} aria-hidden className="smart-dataset__chevron" />
                  </button>
                  {open && (
                    <div className="smart-dataset__body">
                      {dataset.targets.length === 0 ? (
                        <p className="decision-muted">Подходящих кандидатов в цель не найдено.</p>
                      ) : (
                        <ul className="targets">
                          {dataset.targets.map((target) => (
                            <li key={target.column} className="target">
                              <span className="target__head">
                                <Target size={14} aria-hidden />
                                <strong>{target.column}</strong>
                                <span className="purpose-chip">{TASK[target.task]}</span>
                                {target.classes !== null && <span className="decision-muted">классов: {target.classes}</span>}
                                {target.minority_share !== null && (
                                  <span className="decision-muted">редкий класс: {formatRatio(target.minority_share)}</span>
                                )}
                              </span>
                              {target.leakage_suspects.map((suspect) => (
                                <span key={suspect.column} className="target__leak">
                                  Возможная утечка — «{suspect.column}»: {suspect.reason}
                                </span>
                              ))}
                              {target.warnings.map((warning) => (
                                <span key={warning} className="target__warning">
                                  {warning}
                                </span>
                              ))}
                            </li>
                          ))}
                        </ul>
                      )}
                      <p className="decision-hint">
                        Перед обучением решите сами: будет ли каждый признак известен в момент прогноза. Это по данным не
                        определить. Обучение — в ModelArena через пакет из раздела «Конвейер».
                      </p>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

const RECOMMENDATION: Record<string, string> = {
  join: "соединять",
  aggregate: "сначала агрегировать",
  keep_separate: "не соединять",
  review: "нужна проверка",
};

function AiPairAnswer({
  busy,
  answer,
  leftName,
  rightName,
  onAsk,
}: {
  busy: boolean;
  answer: AiAssemblyAdvice | string | undefined;
  leftName: string;
  rightName: string;
  onAsk: () => void;
}) {
  return (
    <div className="ai-box">
      <div className="ai-box__head">
        <button type="button" className="button" disabled={busy} onClick={onAsk}>
          {busy ? <Loader2 size={14} className="spin" aria-hidden /> : <Bot size={14} aria-hidden />}
          Объяснить с помощью ИИ
        </button>
      </div>
      {typeof answer === "string" && <p className="repair__error">{answer}</p>}
      {answer && typeof answer !== "string" && (
        <div className="ai-answer">
          <p className="ai-answer__label">
            ИИ · {answer.model.split("/").pop()} · {answer.seconds} с · рекомендация: {RECOMMENDATION[answer.recommendation]}
          </p>
          <p>{answer.summary}</p>
          {answer.recommendation === "aggregate" && answer.aggregate_table !== "none" && (
            <p className="decision-muted">
              Свести «{answer.aggregate_table === "left" ? leftName : rightName}»
              {answer.group_by.length > 0 && ` по ${answer.group_by.join(", ")}`}
              {answer.measures.length > 0 && `, агрегировать ${answer.measures.join(", ")}`}.
            </p>
          )}
          {answer.ml_target && <p className="decision-muted">Кандидат в цель для обучения: {answer.ml_target}</p>}
          {answer.risks.length > 0 && (
            <ul className="ai-answer__risks">
              {answer.risks.map((risk) => (
                <li key={risk}>{risk}</li>
              ))}
            </ul>
          )}
          {answer.rejected.map((note) => (
            <p key={note} className="target__warning">
              Проверка: {note}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
