import type { AnalysedAlternative, ConstraintDraft, DecisionAnalysis, Interval } from "../../types/decision";

type PlotProps = {
  analysis: DecisionAnalysis;
  xKey: string;
  yKey: string;
  colors: Map<string, string>;
  constraints: ConstraintDraft[];
};

const W = 520;
const H = 300;
const PAD = { left: 56, right: 16, top: 14, bottom: 40 };

function scale(domain: [number, number], range: [number, number]) {
  const [d0, d1] = domain;
  const span = d1 - d0 || Math.abs(d0) || 1;
  return (value: number) => range[0] + ((value - d0) / span) * (range[1] - range[0]);
}

function ticks([low, high]: [number, number], count = 5): number[] {
  if (low === high) return [low];
  return Array.from({ length: count }, (_, i) => low + ((high - low) * i) / (count - 1));
}

function short(value: number): string {
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 0.01 || abs >= 10000)) return value.toExponential(1);
  return Number(value.toPrecision(3)).toString();
}

export function TradeoffPlot({ analysis, xKey, yKey, colors, constraints }: PlotProps) {
  const x = analysis.criteria.find((item) => item.key === xKey);
  const y = analysis.criteria.find((item) => item.key === yKey);
  const points = analysis.alternatives
    .map((alt) => ({ alt, xv: alt.values[xKey]?.value ?? null, yv: alt.values[yKey]?.value ?? null }))
    .filter((p): p is { alt: AnalysedAlternative; xv: number; yv: number } => p.xv !== null && p.yv !== null);

  if (!x || !y || points.length === 0) {
    return <p className="decision-muted">Нет альтернатив с измерениями по обоим выбранным критериям.</p>;
  }

  const pad = (lo: number, hi: number): [number, number] => {
    const span = hi - lo || Math.abs(hi) || 1;
    return [lo - span * 0.08, hi + span * 0.08];
  };
  const xs = points.map((p) => p.xv);
  const ys = points.map((p) => p.yv);
  const xDomain = pad(Math.min(...xs), Math.max(...xs));
  const yDomain = pad(Math.min(...ys), Math.max(...ys));
  const sx = scale(xDomain, [PAD.left, W - PAD.right]);
  const sy = scale(yDomain, [H - PAD.bottom, PAD.top]);
  const winner = analysis.modes.robust.winner;
  const axisLabel = (c: typeof x) => `${c.label}${c.unit ? `, ${c.unit}` : ""} ${c.direction === "max" ? "↑ лучше" : "↓ лучше"}`;

  return (
    <svg className="tradeoff" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Компромисс: ${x.label} и ${y.label}`}>
      {ticks(xDomain).map((t) => (
        <g key={`x${t}`}>
          <line x1={sx(t)} x2={sx(t)} y1={PAD.top} y2={H - PAD.bottom} className="tradeoff__grid" />
          <text x={sx(t)} y={H - PAD.bottom + 14} className="tradeoff__tick" textAnchor="middle">
            {short(t)}
          </text>
        </g>
      ))}
      {ticks(yDomain).map((t) => (
        <g key={`y${t}`}>
          <line x1={PAD.left} x2={W - PAD.right} y1={sy(t)} y2={sy(t)} className="tradeoff__grid" />
          <text x={PAD.left - 6} y={sy(t) + 3} className="tradeoff__tick" textAnchor="end">
            {short(t)}
          </text>
        </g>
      ))}
      {constraints.map((c, index) => {
        if (c.criterion === xKey && c.bound >= xDomain[0] && c.bound <= xDomain[1]) {
          return <line key={`c${index}`} x1={sx(c.bound)} x2={sx(c.bound)} y1={PAD.top} y2={H - PAD.bottom} className="tradeoff__limit" />;
        }
        if (c.criterion === yKey && c.bound >= yDomain[0] && c.bound <= yDomain[1]) {
          return <line key={`c${index}`} x1={PAD.left} x2={W - PAD.right} y1={sy(c.bound)} y2={sy(c.bound)} className="tradeoff__limit" />;
        }
        return null;
      })}
      <text x={(PAD.left + W - PAD.right) / 2} y={H - 6} className="tradeoff__axis" textAnchor="middle">
        {axisLabel(x)}
      </text>
      <text
        x={12}
        y={(PAD.top + H - PAD.bottom) / 2}
        className="tradeoff__axis"
        textAnchor="middle"
        transform={`rotate(-90 12 ${(PAD.top + H - PAD.bottom) / 2})`}
      >
        {axisLabel(y)}
      </text>
      {points.map(({ alt, xv, yv }) => {
        const cx = sx(xv);
        const cy = sy(yv);
        const color = colors.get(alt.key) ?? "var(--text-muted)";
        const excluded = alt.status === "infeasible" || alt.status === "incomplete" || alt.status === "unverified";
        return (
          <g key={alt.key}>
            <title>{`${alt.label}: ${x.label} ${xv}, ${y.label} ${yv} — ${alt.status_reason || "Парето-оптимальна"}`}</title>
            {alt.key === winner && <circle cx={cx} cy={cy} r={11} className="tradeoff__winner" />}
            {excluded ? (
              <path d={`M${cx - 5} ${cy - 5}L${cx + 5} ${cy + 5}M${cx + 5} ${cy - 5}L${cx - 5} ${cy + 5}`} stroke={color} strokeWidth={2} />
            ) : (
              <circle
                cx={cx}
                cy={cy}
                r={6}
                fill={alt.status === "pareto" ? color : "transparent"}
                stroke={color}
                strokeWidth={2}
              />
            )}
            <text x={cx + 9} y={cy - 8} className="tradeoff__label">
              {alt.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

type StripsProps = { analysis: DecisionAnalysis; intervals?: Interval[]; colors: Map<string, string> };

export function StabilityStrips({ analysis, intervals: given, colors }: StripsProps) {
  const label = new Map(analysis.alternatives.map((alt) => [alt.key, alt.label]));
  const intervals = given ?? analysis.robustness.intervals;
  if (intervals.length === 0) return <p className="decision-muted">Анализ интервалов недоступен: нет допустимых альтернатив.</p>;

  return (
    <div className="strips">
      {intervals.map((interval) => {
        const criterion = analysis.criteria.find((item) => item.key === interval.criterion);
        return (
          <div key={interval.criterion} className="strips__row">
            <div className="strips__name">
              <span>{criterion?.label ?? interval.criterion}</span>
              <span className="tabular decision-muted">
                {interval.current.toFixed(2)} ∈ [{interval.low.toFixed(2)}; {interval.high.toFixed(2)}]
              </span>
            </div>
            <div className="strips__track" role="img" aria-label={`Победитель при разных весах «${criterion?.label}»`}>
              {interval.segments.map((segment, index) => {
                const next = interval.segments[index + 1];
                const end = next ? next.start : segment.end;
                return (
                  <span
                    key={`${segment.start}-${segment.winner}`}
                    className="strips__segment"
                    style={{
                      left: `${segment.start * 100}%`,
                      width: `${Math.max((end - segment.start) * 100, 0.6)}%`,
                      background: colors.get(segment.winner),
                    }}
                    title={`вес ${segment.start.toFixed(2)}–${end.toFixed(2)}: первая ${label.get(segment.winner)}`}
                  />
                );
              })}
              <span
                className="strips__hold"
                style={{ left: `${interval.low * 100}%`, width: `${(interval.high - interval.low) * 100}%` }}
              />
              <span className="strips__marker" style={{ left: `${interval.current * 100}%` }} />
            </div>
          </div>
        );
      })}
      <div className="strips__scale tabular decision-muted">
        <span>вес 0</span>
        <span>0.5</span>
        <span>1</span>
      </div>
    </div>
  );
}

export function ShareBars({
  shares,
  colors,
  labels,
}: {
  shares: Record<string, number>;
  colors: Map<string, string>;
  labels: Map<string, string>;
}) {
  const rows = Object.entries(shares).sort((a, b) => b[1] - a[1]);
  return (
    <ul className="shares">
      {rows.map(([key, share]) => (
        <li key={key} className="shares__row">
          <span className="shares__name">{labels.get(key) ?? key}</span>
          <span className="shares__track">
            <span className="shares__fill" style={{ width: `${share * 100}%`, background: colors.get(key) }} />
          </span>
          <span className="tabular shares__value">{(share * 100).toFixed(1)}%</span>
        </li>
      ))}
    </ul>
  );
}
