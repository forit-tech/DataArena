import { useEffect, useState } from "react";
import { Loader2, X } from "lucide-react";
import { describeError } from "../../api/client";
import { fetchColumnStatistics } from "../../api/endpoints";
import type { ColumnStatistics } from "../../types/dataset";
import { formatCell, formatCount, formatRatio, formatStat, rowsLabel } from "../../utils/format";

//#подписи числовых показателей заданы здесь, а не собираются из ключей ответа:
//#«std» в интерфейсе на русском означает ровно ничего
const NUMERIC_LABELS: [string, string][] = [
  ["min", "минимум"],
  ["q1", "нижняя четверть"],
  ["median", "медиана"],
  ["mean", "среднее"],
  ["q3", "верхняя четверть"],
  ["max", "максимум"],
  ["std", "разброс"],
];

export type ColumnStatsProps = {
  workspaceId: string;
  datasetId: string;
  column: string;
  onClose: () => void;
};

export function ColumnStats({ workspaceId, datasetId, column, onClose }: ColumnStatsProps) {
  const [statistics, setStatistics] = useState<ColumnStatistics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    setStatistics(null);
    setError(null);

    fetchColumnStatistics(workspaceId, datasetId, column, controller.signal)
      .then(setStatistics)
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(describeError(cause, "Не удалось посчитать показатели колонки."));
        }
      });

    //#панель закрывается или переключается на другую колонку — ответ прежней больше
    //#не нужен и не должен перезаписать новую
    return () => controller.abort();
  }, [workspaceId, datasetId, column]);

  return (
    <aside className="column-stats" aria-label={`Статистика колонки ${column}`}>
      <div className="column-stats__head">
        <div>
          <p className="column-stats__name">{column}</p>
          {statistics && <p className="column-stats__dtype">{statistics.dtype}</p>}
        </div>
        <button type="button" className="button" onClick={onClose} aria-label="Закрыть статистику">
          <X size={14} aria-hidden />
        </button>
      </div>

      {error && (
        <div className="banner banner--error" role="alert">
          <p className="banner__title">Статистика не посчиталась</p>
          <p className="banner__text">{error}</p>
        </div>
      )}

      {!statistics && !error && (
        <p className="column-stats__loading">
          <Loader2 size={14} className="spin" aria-hidden /> Считается по всей колонке…
        </p>
      )}

      {statistics && (
        <>
          <dl className="column-stats__grid">
            <div>
              <dt>всего</dt>
              <dd>{rowsLabel(statistics.row_count)}</dd>
            </div>
            <div>
              <dt>пропусков</dt>
              <dd>
                {formatCount(statistics.null_count)} · {formatRatio(statistics.null_ratio)}
              </dd>
            </div>
            <div>
              <dt>различных</dt>
              <dd>
                {formatCount(statistics.unique_count)} · {formatRatio(statistics.unique_ratio)}
              </dd>
            </div>
          </dl>

          {statistics.numeric && (
            <dl className="column-stats__grid">
              {NUMERIC_LABELS.map(([key, label]) => {
                const value = statistics.numeric?.[key];

                //#показатель, которого нет, не показывается вовсе: «—» в таблице чисел
                //#читается как значение
                return value === null || value === undefined ? null : (
                  <div key={key}>
                    <dt>{label}</dt>
                    <dd className="tabular">{formatStat(value)}</dd>
                  </div>
                );
              })}
            </dl>
          )}

          {statistics.histogram && !statistics.histogram.constant && (
            <Histogram histogram={statistics.histogram} />
          )}

          {statistics.top_values && statistics.top_values.length > 0 && (
            <div className="column-stats__section">
              <p className="column-stats__title">Самые частые значения</p>
              <ul className="column-stats__values">
                {statistics.top_values.map((item, index) => (
                  <li key={index}>
                    <span className="column-stats__value" title={formatCell(item.value)}>
                      {formatCell(item.value)}
                    </span>
                    <span className="column-stats__count tabular">
                      {formatCount(item.count)} · {formatRatio(item.ratio)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </aside>
  );
}

function Histogram({ histogram }: { histogram: { bins: number[]; counts: number[] } }) {
  const peak = Math.max(...histogram.counts, 1);

  return (
    <div className="column-stats__section">
      <p className="column-stats__title">Распределение</p>
      <div className="histogram" role="img" aria-label="Распределение значений по корзинам">
        {histogram.counts.map((count, index) => (
          <span
            key={index}
            className="histogram__bar"
            style={{ height: `${Math.round((count / peak) * 100)}%` }}
            title={`${formatStat(histogram.bins[index])} … ${formatStat(
              histogram.bins[index + 1],
            )}: ${rowsLabel(count)}`}
          />
        ))}
      </div>
      <p className="column-stats__range tabular">
        {formatStat(histogram.bins[0])} … {formatStat(histogram.bins[histogram.bins.length - 1])}
      </p>
    </div>
  );
}
