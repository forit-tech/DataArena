import { useMemo, useRef, useState } from "react";
import { Download, FileUp, Plus, RefreshCw, Trash2 } from "lucide-react";
import { describeError } from "../../api/client";
import { fetchModelArenaMatrix, fetchModelArenaRuns, importDecisionMatrix } from "../../api/endpoints";
import type { ConstraintDraft, CriterionDraft, DecisionAnalysis, ModelArenaRun, ScaleDraft } from "../../types/decision";
import { ShareBars, StabilityStrips, TradeoffPlot } from "./charts";
import { colorFor } from "./palette";
import { download, reportCsv, reportJson, reportMarkdown, statusLabel } from "./report";
import { DEMO_MATRIX, PROFILES, applyProfile, useDecisionLab, whyNotReady } from "./useDecisionLab";
import type { LabState } from "./useDecisionLab";

//#тона состава весов нейтральнее цветов моделей: это критерии, а не альтернативы
const CRITERION_TONES = ["#9fb8a9", "#c2a877", "#9aaabf", "#b99a8f", "#aaa3bb", "#b3b08b"];

const VERDICT_LABEL: Record<string, string> = {
  stable: "устойчивая",
  conditional: "условная",
  unstable: "неустойчивая",
  none: "нет рекомендации",
};

const SOURCE_LABEL: Record<string, string> = {
  modelarena: "прогон ModelArena",
  matrix: "JSON-матрица",
  csv: "CSV",
  demo: "демонстрационные данные",
};

export function DecisionLab() {
  const lab = useDecisionLab();
  const { state, update, analysis } = lab;
  const colors = useMemo(
    () => new Map(state.alternatives.map((alt, index) => [alt.key, colorFor(index)])),
    [state.alternatives],
  );

  return (
    <div className="decision">
      <SourceBar lab={lab} />

      {state.criteria.length === 0 ? (
        <div className="empty-state">
          <h2 className="empty-state__title">Сравните модели по нескольким критериям</h2>
          <p className="empty-state__text">
            Загрузите лидерборд прогона ModelArena (JSON) или таблицу CSV, где строка — модель, а столбцы —
            измеренные характеристики: качество, время, размер. Затем задайте приоритеты и ограничения —
            расчёт обновится сразу.
          </p>
        </div>
      ) : (
        <div className="decision__grid">
          <aside className="decision__controls" aria-label="Настройки выбора">
            <CriteriaPanel state={state} update={update} analysis={analysis} />
            <ConstraintsPanel state={state} update={update} />
            <ScalesPanel state={state} update={update} analysis={analysis} />
            <AlternativesPanel state={state} update={update} colors={colors} />
            <SettingsPanel state={state} update={update} />
          </aside>

          <section className="decision__results" aria-live="polite" aria-busy={lab.loading}>
            {lab.error && (
              <div className="banner banner--error" role="alert">
                <div>
                  <p className="banner__title">Расчёт не выполнен</p>
                  <p className="banner__text">{lab.error}</p>
                </div>
              </div>
            )}
            {!lab.body && <p className="decision-panel decision-muted">{whyNotReady(state)}</p>}
            {lab.body && !analysis && !lab.error && <p className="decision-panel decision-muted">Считаю…</p>}
            {analysis && lab.body && (
              <Results analysis={analysis} colors={colors} state={state} loading={lab.loading} />
            )}
          </section>
        </div>
      )}
    </div>
  );
}

type Lab = ReturnType<typeof useDecisionLab>;
type Update = Lab["update"];

function SourceBar({ lab }: { lab: Lab }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [runs, setRuns] = useState<ModelArenaRun[] | null>(null);
  const { state, analysis, body } = lab;

  const run = async (action: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setMessage(null);
    try {
      await action();
    } catch (error) {
      setMessage(describeError(error, fallback));
    } finally {
      setBusy(false);
    }
  };

  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

  return (
    <div className="decision-source">
      <div className="decision-source__actions">
        <button type="button" className="button button--primary" disabled={busy} onClick={() => fileInput.current?.click()}>
          <FileUp size={14} aria-hidden /> Загрузить файл
        </button>
        <input
          ref={fileInput}
          type="file"
          accept=".json,.csv,.tsv"
          hidden
          data-testid="decision-file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void run(async () => lab.load(await importDecisionMatrix(file)), "Файл не удалось разобрать.");
          }}
        />
        <button
          type="button"
          className="button"
          disabled={busy}
          onClick={() => void run(async () => setRuns((await fetchModelArenaRuns()).runs), "ModelArena недоступна.")}
        >
          <RefreshCw size={14} aria-hidden /> Прогоны ModelArena
        </button>
        {runs && (
          <select
            className="decision-input"
            aria-label="Прогон ModelArena"
            defaultValue=""
            onChange={(event) => {
              const id = event.target.value;
              if (id) void run(async () => lab.load(await fetchModelArenaMatrix(id)), "Лидерборд не загрузился.");
            }}
          >
            <option value="" disabled>
              {runs.length ? "Выберите прогон" : "Прогонов нет"}
            </option>
            {runs.map((item) => (
              <option key={item.run_id} value={item.run_id}>
                {item.run_id}, {item.task_type ?? "?"}, цель {item.target_column ?? "?"} ({item.state})
              </option>
            ))}
          </select>
        )}
        <button type="button" className="button" disabled={busy} onClick={() => lab.load(DEMO_MATRIX)}>
          Демо-набор
        </button>
        {state.criteria.length > 0 && (
          <button type="button" className="button" onClick={lab.reset}>
            <Trash2 size={14} aria-hidden /> Очистить
          </button>
        )}
      </div>

      <div className="decision-source__actions">
        <span className="decision-muted">Отчёт:</span>
        {(["json", "csv", "md"] as const).map((kind) => (
          <button
            key={kind}
            type="button"
            className="button"
            disabled={!analysis || !body}
            onClick={() => {
              if (!analysis || !body) return;
              if (kind === "json") download(`decision-${stamp}.json`, reportJson(body, analysis), "application/json");
              if (kind === "csv") download(`decision-${stamp}.csv`, reportCsv(analysis), "text/csv");
              if (kind === "md") download(`decision-${stamp}.md`, reportMarkdown(analysis), "text/markdown");
            }}
          >
            <Download size={14} aria-hidden /> {kind.toUpperCase()}
          </button>
        ))}
      </div>

      {message && (
        <p className="decision-source__error" role="alert">
          {message}
        </p>
      )}
      {state.source && (
        <div className={`decision-source__notes${state.source === "demo" ? " decision-source__notes--demo" : ""}`}>
          <strong>Источник: {SOURCE_LABEL[state.source] ?? state.source}.</strong>{" "}
          {state.notes.join(" ")}
        </div>
      )}
    </div>
  );
}

function CriteriaPanel({ state, update, analysis }: { state: LabState; update: Update; analysis: DecisionAnalysis | null }) {
  const enabled = state.criteria.filter((item) => item.enabled && item.weight > 0);
  const total = enabled.reduce((sum, item) => sum + item.weight, 0);
  const patch = (key: string, change: Partial<CriterionDraft>) =>
    update((prev) => ({ criteria: prev.criteria.map((item) => (item.key === key ? { ...item, ...change } : item)) }));
  const effective = new Map(analysis?.criteria.map((item) => [item.key, item.effective_weight]));

  return (
    <div className="decision-panel">
      <h2 className="decision-panel__title">Критерии и веса</h2>
      <div className="decision-profiles" role="group" aria-label="Профили приоритетов">
        {PROFILES.map((profile) => (
          <button
            key={profile.id}
            type="button"
            className="decision-chip"
            title={profile.hint}
            onClick={() => update((prev) => ({ criteria: applyProfile(prev.criteria, profile, prev.primary) }))}
          >
            {profile.label}
          </button>
        ))}
      </div>

      <div className="weight-bar" aria-label="Состав эффективных весов">
        {enabled.map((item, index) => (
          <span
            key={item.key}
            className="weight-bar__part"
            style={{ flexGrow: item.weight, background: CRITERION_TONES[index % CRITERION_TONES.length] }}
            title={`${item.label}: ${((item.weight / total) * 100).toFixed(0)}%`}
          >
            {item.weight / total >= 0.12 ? item.label : ""}
          </span>
        ))}
      </div>

      <ul className="criteria">
        {state.criteria.filter((item) => item.enabled).map((item) => (
          <li key={item.key} className={`criteria__row${item.enabled ? "" : " criteria__row--off"}`}>
            <label className="criteria__head">
              <input type="checkbox" checked={item.enabled} onChange={(e) => patch(item.key, { enabled: e.target.checked })} />
              <span className="criteria__label">
                {item.label}
                {item.unit && <span className="decision-muted">, {item.unit}</span>}
              </span>
            </label>
            <button
              type="button"
              className="criteria__direction"
              aria-label={`Направление «${item.label}»: ${item.direction === "max" ? "больше — лучше" : "меньше — лучше"}`}
              title="Переключить направление"
              onClick={() => patch(item.key, { direction: item.direction === "max" ? "min" : "max" })}
            >
              {item.direction === "max" ? "↑ max" : "↓ min"}
            </button>
            <input
              type="range"
              min={0}
              max={10}
              step={0.5}
              value={Math.min(item.weight, 10)}
              disabled={!item.enabled}
              aria-label={`Вес «${item.label}»`}
              onChange={(e) => patch(item.key, { weight: Number(e.target.value) })}
            />
            <input
              type="number"
              className="decision-input criteria__weight tabular"
              min={0}
              step={0.1}
              value={item.weight}
              disabled={!item.enabled}
              aria-label={`Вес «${item.label}» числом`}
              onChange={(e) => {
                const value = Number(e.target.value);
                if (Number.isFinite(value) && value >= 0) patch(item.key, { weight: value });
              }}
            />
            <span className="criteria__effective tabular" title="Эффективный вес после нормировки (Σ = 1)">
              {!item.enabled ? "выкл" : item.weight === 0 ? "огр." : `${((effective.get(item.key) ?? item.weight / total) * 100).toFixed(0)}%`}
            </span>
          </li>
        ))}
      </ul>
      {state.criteria.some((item) => !item.enabled) && (
        <details className="criteria-more">
          <summary>Ещё критерии ({state.criteria.filter((item) => !item.enabled).length})</summary>
          <ul className="criteria-more__list">
            {state.criteria
              .filter((item) => !item.enabled)
              .map((item) => (
                <li key={item.key}>
                  <label>
                    <input type="checkbox" checked={false} onChange={() => patch(item.key, { enabled: true })} />
                    {item.label}
                    {item.unit && <span className="decision-muted">, {item.unit}</span>}
                  </label>
                </li>
              ))}
          </ul>
        </details>
      )}
      <p className="decision-hint">
        Вес задаётся в любом масштабе и нормируется до суммы 1. Вес 0 — критерий не оценивается, но по нему можно
        поставить ограничение.
      </p>

      <label className="decision-field">
        Основная метрика для режима A
        <select
          className="decision-input"
          value={state.primary ?? ""}
          onChange={(e) => update({ primary: e.target.value || null })}
        >
          <option value="">автоматически (первая метрика качества)</option>
          {state.criteria
            .filter((item) => item.enabled)
            .map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
              </option>
            ))}
        </select>
      </label>
    </div>
  );
}

function ConstraintsPanel({ state, update }: { state: LabState; update: Update }) {
  const available = state.criteria.filter((item) => item.enabled);
  const set = (index: number, change: Partial<ConstraintDraft>) =>
    update((prev) => ({ constraints: prev.constraints.map((item, i) => (i === index ? { ...item, ...change } : item)) }));

  return (
    <div className="decision-panel">
      <h2 className="decision-panel__title">Жёсткие ограничения</h2>
      {state.constraints.length === 0 && (
        <p className="decision-hint">Ограничений нет. Например: инференс ≤ 50 мс, размер ≤ 100 МБ.</p>
      )}
      <ul className="constraints">
        {state.constraints.map((item, index) => {
          const unit = state.criteria.find((c) => c.key === item.criterion)?.unit;
          return (
            <li key={index} className="constraints__row">
              <select
                className="decision-input"
                aria-label="Критерий ограничения"
                value={item.criterion}
                onChange={(e) => set(index, { criterion: e.target.value })}
              >
                {available.map((c) => (
                  <option key={c.key} value={c.key}>
                    {c.label}
                  </option>
                ))}
              </select>
              <select
                className="decision-input"
                aria-label="Знак ограничения"
                value={item.op}
                onChange={(e) => set(index, { op: e.target.value as "<=" | ">=" })}
              >
                <option value="<=">≤</option>
                <option value=">=">≥</option>
              </select>
              <input
                type="number"
                className="decision-input tabular"
                aria-label="Граница ограничения"
                value={Number.isFinite(item.bound) ? item.bound : ""}
                step="any"
                onChange={(e) => set(index, { bound: e.target.value === "" ? Number.NaN : Number(e.target.value) })}
              />
              <span className="decision-muted">{unit}</span>
              <button
                type="button"
                className="icon-button"
                aria-label="Удалить ограничение"
                onClick={() => update((prev) => ({ constraints: prev.constraints.filter((_, i) => i !== index) }))}
              >
                <Trash2 size={13} aria-hidden />
              </button>
            </li>
          );
        })}
      </ul>
      <button
        type="button"
        className="button"
        disabled={available.length === 0}
        onClick={() => {
          const first = available.find((c) => c.group !== "quality") ?? available[0]!;
          const values = state.alternatives.map((alt) => alt.values[first.key]?.value).filter((v): v is number => v != null);
          const bound = values.length ? Number(((Math.min(...values) + Math.max(...values)) / 2).toPrecision(3)) : 0;
          update((prev) => ({
            constraints: [...prev.constraints, { criterion: first.key, op: first.direction === "max" ? ">=" : "<=", bound }],
          }));
        }}
      >
        <Plus size={14} aria-hidden /> Добавить ограничение
      </button>
    </div>
  );
}

const SCALE_SOURCE: Record<string, string> = {
  natural: "естественная",
  budget: "из бюджета",
  user: "ваша",
  observed: "по данным",
};

function ScalesPanel({ state, update, analysis }: { state: LabState; update: Update; analysis: DecisionAnalysis | null }) {
  const resolved = analysis?.modes.fixed?.scales ?? [];
  const overrides = new Map((state.scales ?? []).map((item) => [item.criterion, item]));
  const label = new Map(state.criteria.map((c) => [c.key, c]));
  const set = (draft: ScaleDraft) =>
    update((prev) => ({ scales: [...(prev.scales ?? []).filter((s) => s.criterion !== draft.criterion), draft] }));
  const reset = (criterion: string) =>
    update((prev) => ({ scales: (prev.scales ?? []).filter((s) => s.criterion !== criterion) }));

  return (
    <details className="decision-panel">
      <summary className="decision-panel__title">Шкалы режима D</summary>
      {resolved.length === 0 ? (
        <p className="decision-hint">Шкалы появятся после первого расчёта.</p>
      ) : (
        <ul className="scales">
          {resolved.map((scale) => {
            const draft = overrides.get(scale.criterion) ?? scale;
            const criterion = label.get(scale.criterion);
            return (
              <li key={scale.criterion} className="scales__row">
                <div className="scales__head">
                  <span>{criterion?.label ?? scale.criterion}</span>
                  <span className={`scale-source scale-source--${scale.source}`} title={scale.note}>
                    {SCALE_SOURCE[scale.source]}
                  </span>
                </div>
                <div className="scales__fields">
                  <select
                    className="decision-input"
                    aria-label={`Тип шкалы «${criterion?.label}»`}
                    value={draft.kind}
                    onChange={(e) => set({ ...draft, criterion: scale.criterion, kind: e.target.value as ScaleDraft["kind"] })}
                  >
                    <option value="linear">линейная</option>
                    <option value="log">логарифм.</option>
                  </select>
                  <label className="scales__bound">
                    0 при
                    <input
                      type="number"
                      step="any"
                      className="decision-input tabular"
                      aria-label={`Худшее значение «${criterion?.label}»`}
                      value={draft.worst}
                      onChange={(e) => set({ ...draft, criterion: scale.criterion, worst: Number(e.target.value) })}
                    />
                  </label>
                  <label className="scales__bound">
                    1 при
                    <input
                      type="number"
                      step="any"
                      className="decision-input tabular"
                      aria-label={`Лучшее значение «${criterion?.label}»`}
                      value={draft.best}
                      onChange={(e) => set({ ...draft, criterion: scale.criterion, best: Number(e.target.value) })}
                    />
                  </label>
                  {overrides.has(scale.criterion) && (
                    <button type="button" className="icon-button" aria-label="Вернуть шкалу по умолчанию" onClick={() => reset(scale.criterion)}>
                      <RefreshCw size={12} aria-hidden />
                    </button>
                  )}
                </div>
                <p className="decision-hint scales__note">{scale.note}</p>
              </li>
            );
          })}
        </ul>
      )}
      <p className="decision-hint">
        Режим D переводит значения в полезность от 0 до 1 по шкале, заданной до сравнения. Поэтому место модели не
        зависит от того, какие ещё модели в списке. Шкала «по данным» эту независимость теряет: задайте свою шкалу или
        бюджет через ограничение ≤.
      </p>
    </details>
  );
}

function AlternativesPanel({ state, update, colors }: { state: LabState; update: Update; colors: Map<string, string> }) {
  return (
    <div className="decision-panel">
      <h2 className="decision-panel__title">
        Модели <span className="decision-muted">{state.alternatives.filter((a) => a.enabled).length} из {state.alternatives.length}</span>
      </h2>
      <ul className="alts">
        {state.alternatives.map((alt) => (
          <li key={alt.key}>
            <label className="alts__row">
              <input
                type="checkbox"
                checked={alt.enabled}
                onChange={(e) =>
                  update((prev) => ({
                    alternatives: prev.alternatives.map((item) => (item.key === alt.key ? { ...item, enabled: e.target.checked } : item)),
                  }))
                }
              />
              <span className="dot" style={{ background: colors.get(alt.key) }} />
              <span>{alt.label}</span>
              {alt.meta.family && <span className="decision-muted">{alt.meta.family}</span>}
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SettingsPanel({ state, update }: { state: LabState; update: Update }) {
  const s = state.sensitivity;
  const num = (key: keyof typeof s, value: string, min: number, max: number) => {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= min && parsed <= max) update({ sensitivity: { ...s, [key]: parsed } });
  };
  return (
    <details className="decision-panel">
      <summary className="decision-panel__title">Параметры анализа</summary>
      <div className="settings">
        <label className="decision-field">
          Нормализация
          <select className="decision-input" value={state.normalization} onChange={(e) => update({ normalization: e.target.value as "vector" | "minmax" })}>
            <option value="vector">векторная (классический TOPSIS)</option>
            <option value="minmax">min-max</option>
          </select>
        </label>
        <label className="decision-field">
          Сценариев Монте-Карло
          <input className="decision-input tabular" type="number" min={0} max={20000} step={500} value={s.samples} onChange={(e) => num("samples", e.target.value, 0, 20000)} />
        </label>
        <label className="decision-field">
          Seed
          <input className="decision-input tabular" type="number" min={0} value={s.seed} onChange={(e) => num("seed", e.target.value, 0, 2 ** 31 - 1)} />
        </label>
        <label className="decision-field">
          Концентрация весов κ
          <input className="decision-input tabular" type="number" min={1} step={5} value={s.concentration} onChange={(e) => num("concentration", e.target.value, 0.1, 10000)} />
        </label>
        <label className="decision-field">
          Порог устойчивости
          <input className="decision-input tabular" type="number" min={0.05} max={1} step={0.05} value={s.stability_threshold} onChange={(e) => num("stability_threshold", e.target.value, 0.01, 1)} />
        </label>
      </div>
      <p className="decision-hint">
        κ задаёт разброс сценариев вокруг ваших весов: чем больше, тем ближе сценарии к заявленным весам. Один и тот же seed
        даёт один и тот же результат.
      </p>
    </details>
  );
}

function Results({
  analysis,
  colors,
  state,
  loading,
}: {
  analysis: DecisionAnalysis;
  colors: Map<string, string>;
  state: LabState;
  loading: boolean;
}) {
  const labels = useMemo(() => new Map(analysis.alternatives.map((alt) => [alt.key, alt.label])), [analysis]);
  const scored = analysis.criteria.filter((item) => item.role === "scored");
  const [xKey, setX] = useState<string | null>(null);
  const [yKey, setY] = useState<string | null>(null);
  const [shareKind, setShareKind] = useState<"local" | "global" | "noise">("local");
  const quality = scored.find((item) => item.group === "quality") ?? scored[0];
  const resource = scored.find((item) => item.key !== quality?.key) ?? scored[0];
  const x = xKey && analysis.criteria.some((c) => c.key === xKey) ? xKey : (resource?.key ?? "");
  const y = yKey && analysis.criteria.some((c) => c.key === yKey) ? yKey : (quality?.key ?? "");
  const [focus, setFocus] = useState<"robust" | "fixed">("robust");
  const [tab, setTab] = useState<"ranking" | "tradeoff" | "stability" | "why" | "matrix">("ranking");
  const robust = analysis.modes.robust;
  const fixed = analysis.modes.fixed;
  //#переключатель решает, по какому режиму строятся вердикт, таблица и устойчивость:
  //#C — Парето + TOPSIS, D — фиксированные шкалы. Остальные режимы видны в карточках
  const focused = focus === "fixed" && fixed ? fixed : robust;
  const winnerName = focused.winner ? labels.get(focused.winner) : null;
  const name = (key: string | null) => (key ? (labels.get(key) ?? key) : "—");
  const classicRank = new Map(analysis.modes.topsis.ranking.map((row) => [row.key, row]));
  const robustRank = new Map(focused.ranking.map((row) => [row.key, row]));
  const localShares = focus === "fixed" && fixed ? fixed.local : analysis.robustness.local;
  const noiseShares = focus === "fixed" && fixed ? fixed.noise : analysis.robustness.noise;
  const intervals = focus === "fixed" && fixed ? fixed.intervals : analysis.robustness.intervals;
  const order = [...analysis.alternatives].sort((a, b) => {
    const ra = robustRank.get(a.key)?.rank ?? 999;
    const rb = robustRank.get(b.key)?.rank ?? 999;
    return ra - rb || (classicRank.get(a.key)?.rank ?? 999) - (classicRank.get(b.key)?.rank ?? 999);
  });
  const shares =
    shareKind === "local"
      ? localShares
      : shareKind === "global"
        ? focus === "fixed"
          ? {}
          : analysis.robustness.global
        : (noiseShares ?? {});
  const modeLetter = focus === "fixed" ? "D" : "C";

  return (
    <div className={`decision-results${loading ? " decision-results--stale" : ""}`}>
      <div className={`verdict verdict--${focused.verdict}`}>
        <div className="verdict__main">
          <div className="verdict__switch" role="radiogroup" aria-label="Режим рекомендации">
            <button type="button" role="radio" aria-checked={focus === "robust"} onClick={() => setFocus("robust")}>
              Режим C
            </button>
            <button type="button" role="radio" aria-checked={focus === "fixed"} disabled={!fixed} onClick={() => setFocus("fixed")}>
              Режим D
            </button>
          </div>
          <span className="verdict__winner" data-testid="robust-winner" key={`${focus}-${focused.winner}`}>
            {winnerName ?? "Нет допустимой модели"}
          </span>
          <span className="verdict__text">{focused.verdict_text}</span>
        </div>
        <span className="verdict__badge">{VERDICT_LABEL[focused.verdict]}</span>
      </div>

      <table className="modes-table">
        <tbody>
          <ModeRow
            letter="A"
            title="По одной метрике"
            winner={name(analysis.modes.primary.winner)}
            color={colors.get(analysis.modes.primary.winner ?? "")}
            note={
              analysis.modes.primary.winner_violates.length
                ? `нарушает: ${analysis.modes.primary.winner_violates.join("; ")}`
                : analysis.modes.primary.indistinguishable.length
                  ? `неотличима от ${analysis.modes.primary.indistinguishable.length} других по разбросу фолдов`
                  : ""
            }
            warn={analysis.modes.primary.winner_violates.length > 0}
          />
          <ModeRow
            letter="B"
            title="Классический TOPSIS"
            winner={name(analysis.modes.topsis.winner)}
            color={colors.get(analysis.modes.topsis.winner ?? "")}
            note={analysis.modes.topsis.winner_violates.length ? `нарушает: ${analysis.modes.topsis.winner_violates.join("; ")}` : ""}
            warn={analysis.modes.topsis.winner_violates.length > 0}
          />
          <ModeRow
            letter="C"
            title="Ограничения, Парето, TOPSIS"
            winner={name(robust.winner)}
            color={colors.get(robust.winner ?? "")}
            note={`${robust.pareto.length} из ${robust.feasible_count} допустимых на фронте Парето`}
            active={focus === "robust"}
            onSelect={() => setFocus("robust")}
          />
          {fixed && (
            <ModeRow
              letter="D"
              title="Фиксированные шкалы"
              winner={name(fixed.winner)}
              color={colors.get(fixed.winner ?? "")}
              note="не зависит от состава набора"
              active={focus === "fixed"}
              onSelect={() => setFocus("fixed")}
            />
          )}
        </tbody>
      </table>

      <div className="result-tabs" role="tablist" aria-label="Подробности">
        {(
          [
            ["ranking", "Ранжирование"],
            ["tradeoff", "Компромисс"],
            ["stability", "Устойчивость"],
            ["why", analysis.warnings.length ? `Почему так (${analysis.warnings.length})` : "Почему так"],
            ["matrix", "Матрица"],
          ] as const
        ).map(([id, text]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
            {text}
          </button>
        ))}
      </div>

      <div className="result-pane" role="tabpanel">
        {tab === "ranking" && (
        <div className="decision-table-wrap">
          <table className="decision-table">
            <thead>
              <tr>
                <th>{modeLetter}</th>
                <th>Модель</th>
                <th title={`Коэффициент близости к идеалу в режиме ${modeLetter}`}>Близость C*</th>
                <th>D⁺</th>
                <th>D⁻</th>
                <th title="Место в классическом TOPSIS (режим B)">B</th>
                <th title="Доля сценариев весов вокруг заявленных, где модель первая">Побед</th>
                <th>Статус</th>
              </tr>
            </thead>
            <tbody>
              {order.map((alt) => {
                const r = robustRank.get(alt.key);
                return (
                  <tr key={alt.key} className={r ? "" : "decision-table__row--out"}>
                    <td className="tabular">{r?.rank ?? "—"}</td>
                    <td>
                      <span className="dot" style={{ background: colors.get(alt.key) }} /> {alt.label}
                    </td>
                    <td>
                      {r ? (
                        <span className="closeness">
                          <span className="closeness__bar" style={{ width: `${r.closeness * 90}px`, background: colors.get(alt.key) }} />
                          <span className="tabular">{r.closeness.toFixed(3)}</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="tabular">{r ? r.d_plus.toFixed(4) : "—"}</td>
                    <td className="tabular">{r ? r.d_minus.toFixed(4) : "—"}</td>
                    <td className="tabular">{classicRank.get(alt.key)?.rank ?? "—"}</td>
                    <td className="tabular">
                      {localShares[alt.key] != null ? `${(localShares[alt.key]! * 100).toFixed(0)}%` : "—"}
                    </td>
                    <td>
                      <span className={`status status--${alt.status}`} title={alt.status_reason}>
                        {statusLabel(alt.status)}
                      </span>
                      {alt.status_reason && <span className="decision-muted status__reason">{alt.status_reason}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        )}
        {tab === "tradeoff" && (
          <>
          <div className="decision-panel__head">
            <span className="decision-muted">Оси:</span>
            <select className="decision-input" aria-label="Ось X" value={x} onChange={(e) => setX(e.target.value)}>
              {analysis.criteria.map((c) => (
                <option key={c.key} value={c.key}>
                  X: {c.label}
                </option>
              ))}
            </select>
            <select className="decision-input" aria-label="Ось Y" value={y} onChange={(e) => setY(e.target.value)}>
              {analysis.criteria.map((c) => (
                <option key={c.key} value={c.key}>
                  Y: {c.label}
                </option>
              ))}
            </select>
          </div>
          <TradeoffPlot analysis={analysis} xKey={x} yKey={y} colors={colors} constraints={state.constraints} />
          <p className="decision-hint">
            ● — на фронте Парето, ○ — доминируется, × — недопустима. Пунктир — граница ограничения. Фронт Парето
            строится по всем оцениваемым критериям, а не только по двум осям.
          </p>
          </>
        )}
        {tab === "stability" && (
          <>
          <StabilityStrips analysis={analysis} intervals={intervals} colors={colors} />
          <p className="decision-hint">
            Полоса — кто первый, если менять вес одного критерия от 0 до 1 при сохранении пропорций остальных. Белая
            метка — текущий вес, рамка — диапазон, где рекомендация не меняется.
          </p>
          <div className="decision-tabs" role="tablist">
            {(
              [
                ["local", "вокруг ваших весов"],
                ["global", "все веса равновероятны"],
                ["noise", "шум измерений"],
              ] as const
            ).map(([kind, text]) => (
              <button
                key={kind}
                type="button"
                role="tab"
                aria-selected={shareKind === kind}
                className="decision-chip"
                disabled={(kind === "noise" && !noiseShares) || (kind === "global" && focus === "fixed")}
                onClick={() => setShareKind(kind)}
              >
                {text}
              </button>
            ))}
          </div>
          {Object.keys(shares).length ? (
            <ShareBars shares={shares} colors={colors} labels={labels} />
          ) : (
            <p className="decision-muted">Нет данных.</p>
          )}
          {analysis.robustness.noise_note && <p className="decision-hint">{analysis.robustness.noise_note}</p>}
          <p className="decision-hint">Доля сценариев — мера устойчивости выбора, а не вероятность того, что модель лучше.</p>
          </>
        )}
        {tab === "why" && (
          <>
        <InfluenceTable analysis={analysis} />
        <ul className="explain">
          {(focus === "fixed" && fixed?.explanation ? fixed.explanation : analysis.explanation).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        {analysis.warnings.length > 0 && (
          <ul className="warnings">
            {analysis.warnings.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
          </>
        )}
        {tab === "matrix" && (
          <>
        <div className="decision-table-wrap">
          <table className="decision-table">
            <thead>
              <tr>
                <th>Модель</th>
                {analysis.criteria.map((c) => (
                  <th key={c.key} title={`${c.direction === "max" ? "больше — лучше" : "меньше — лучше"}; роль: ${c.role}`}>
                    {c.label}
                    {c.unit ? `, ${c.unit}` : ""} {c.direction === "max" ? "↑" : "↓"}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {analysis.alternatives.map((alt) => (
                <tr key={alt.key}>
                  <td>
                    <span className="dot" style={{ background: colors.get(alt.key) }} /> {alt.label}
                  </td>
                  {analysis.criteria.map((c) => {
                    const m = alt.values[c.key];
                    const violated = alt.constraints.some((k) => k.criterion === c.key && k.satisfied === false);
                    return (
                      <td key={c.key} className={`tabular${violated ? " cell--violated" : ""}`} title={m?.note || undefined}>
                        {m?.value == null ? (
                          <span className="cell--missing">нет измерения</span>
                        ) : (
                          <>
                            {Number(m.value.toPrecision(6))}
                            {m.std != null && <span className="decision-muted"> ±{Number(m.std.toPrecision(2))}</span>}
                          </>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="decision-hint tabular">
          Метод {analysis.method.version}, seed {analysis.method.seed}, отпечаток входа{" "}
          {analysis.method.input_sha256.slice(0, 12)}…
        </p>
          </>
        )}
      </div>
    </div>
  );
}

function ModeRow({
  letter,
  title,
  winner,
  color,
  note,
  warn = false,
  active = false,
  onSelect,
}: {
  letter: string;
  title: string;
  winner: string;
  color: string | undefined;
  note: string;
  warn?: boolean;
  active?: boolean;
  onSelect?: () => void;
}) {
  return (
    <tr className={`mode-row${active ? " mode-row--active" : ""}${onSelect ? " mode-row--selectable" : ""}`} onClick={onSelect}>
      <td className="mode-row__letter">{letter}</td>
      <td className="mode-row__title">{title}</td>
      <td className="mode-row__winner" key={winner}>
        <span className="dot" style={{ background: color }} /> {winner}
      </td>
      <td className={warn ? "mode-row__note mode-row__note--warn" : "mode-row__note"}>{note}</td>
    </tr>
  );
}

function InfluenceTable({ analysis }: { analysis: DecisionAnalysis }) {
  //#одна и та же матрица и одни веса, а фактическое влияние критерия у B, C и D разное:
  //#на этом сравнении держится исследовательский вывод о нормализации
  const columns = [
    { key: "topsis", label: "B", values: analysis.modes.topsis.influence ?? {} },
    { key: "robust", label: "C", values: analysis.modes.robust.influence ?? {} },
    { key: "fixed", label: "D", values: analysis.modes.fixed?.influence ?? {} },
  ].filter((column) => Object.keys(column.values).length > 0);
  const rows = analysis.criteria.filter((c) => c.role === "scored");
  if (rows.length < 2 || columns.length === 0) return null;
  return (
    <table className="decision-table influence">
      <thead>
        <tr>
          <th>Критерий</th>
          <th>Заявленный вес</th>
          {columns.map((column) => (
            <th
              key={column.key}
              title={`Фактическое влияние в режиме ${column.label}: доля размаха взвешенного нормированного столбца`}
            >
              Влияние {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.key}>
            <td>{c.label}</td>
            <td className="tabular">{(c.effective_weight * 100).toFixed(0)}%</td>
            {columns.map((column) => {
              const actual = column.values[c.key] ?? 0;
              const low = c.effective_weight >= 0.15 && actual < c.effective_weight / 3;
              return (
                <td key={column.key} className={`tabular${low ? " cell--violated" : ""}`}>
                  <span className="closeness">
                    <span className="closeness__bar" style={{ width: `${actual * 60}px` }} />
                    {(actual * 100).toFixed(0)}%
                  </span>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
