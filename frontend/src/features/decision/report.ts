import type { EvaluateBody } from "../../api/endpoints";
import type { DecisionAnalysis } from "../../types/decision";

//#отчёт содержит всё, чтобы повторить расчёт: входную матрицу, веса, ограничения, seed
//#и отпечаток входа. Повторный POST input на /api/decision/evaluate даёт тот же результат

const STATUS_LABEL: Record<string, string> = {
  pareto: "Парето-оптимальна",
  feasible: "допустима",
  dominated: "доминируется",
  infeasible: "нарушает ограничения",
  unverified: "ограничение не проверено",
  incomplete: "нет измерений",
  pending: "—",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

export function reportJson(input: EvaluateBody, analysis: DecisionAnalysis): string {
  return JSON.stringify(
    { format: "dataarena.decision-report/1", created_at: new Date().toISOString(), input, analysis },
    null,
    2,
  );
}

function csvCell(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",;\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function reportCsv(analysis: DecisionAnalysis): string {
  const scored = analysis.criteria.filter((item) => item.role !== "unused");
  const robust = new Map(analysis.modes.robust.ranking.map((row) => [row.key, row]));
  const classic = new Map(analysis.modes.topsis.ranking.map((row) => [row.key, row]));
  const header = [
    "key",
    "label",
    ...scored.map((item) => item.key),
    "status",
    "topsis_closeness",
    "topsis_rank",
    "robust_closeness",
    "robust_rank",
    "local_win_share",
    "global_win_share",
    "noise_win_share",
  ];
  const rows = analysis.alternatives.map((alt) => [
    alt.key,
    alt.label,
    ...scored.map((item) => alt.values[item.key]?.value ?? ""),
    alt.status,
    classic.get(alt.key)?.closeness ?? "",
    classic.get(alt.key)?.rank ?? "",
    robust.get(alt.key)?.closeness ?? "",
    robust.get(alt.key)?.rank ?? "",
    analysis.robustness.local[alt.key] ?? "",
    analysis.robustness.global[alt.key] ?? "",
    analysis.robustness.noise?.[alt.key] ?? "",
  ]);
  return [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n") + "\n";
}

function fmt(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? "—" : Number(value.toFixed(digits)).toString();
}

export function reportMarkdown(analysis: DecisionAnalysis): string {
  const label = new Map(analysis.alternatives.map((alt) => [alt.key, alt.label]));
  const name = (key: string | null) => (key ? (label.get(key) ?? key) : "нет");
  const lines: string[] = [];
  lines.push("# Отчёт Decision Lab", "");
  lines.push(
    `Метод: ${analysis.method.version}, нормализация ${analysis.method.normalization}, seed ${analysis.method.seed}, ` +
      `сценариев ${analysis.method.samples}, концентрация ${analysis.method.concentration}, шаг сетки ${analysis.method.grid_step}.`,
  );
  lines.push(`Отпечаток входа (SHA-256): \`${analysis.method.input_sha256}\``, "");
  lines.push("## Критерии", "", "| Критерий | Направление | Вес | Эффективный вес | Роль |", "|---|---|---|---|---|");
  for (const item of analysis.criteria) {
    lines.push(
      `| ${item.label}${item.unit ? `, ${item.unit}` : ""} | ${item.direction} | ${fmt(item.weight)} | ${fmt(item.effective_weight)} | ${item.role} |`,
    );
  }
  lines.push("", "## Итог трёх режимов", "");
  lines.push(`- A. По основной метрике (${analysis.modes.primary.criterion}): **${name(analysis.modes.primary.winner)}**`);
  lines.push(`- B. Классический TOPSIS: **${name(analysis.modes.topsis.winner)}**`);
  lines.push(`- C. Ограничения + Парето + TOPSIS + устойчивость: **${name(analysis.modes.robust.winner)}**`);
  lines.push(`- ${analysis.modes.robust.verdict_text}`, "");
  lines.push("## Ранжирование (режим C)", "", "| Место | Альтернатива | C* | D⁺ | D⁻ | Доля побед (локально) |", "|---|---|---|---|---|---|");
  for (const row of analysis.modes.robust.ranking) {
    lines.push(
      `| ${row.rank} | ${name(row.key)} | ${fmt(row.closeness)} | ${fmt(row.d_plus)} | ${fmt(row.d_minus)} | ${fmt(analysis.robustness.local[row.key], 3)} |`,
    );
  }
  const excluded = analysis.alternatives.filter((alt) => alt.status !== "pareto");
  if (excluded.length) {
    lines.push("", "Исключены из ранжирования режима C:", "");
    for (const alt of excluded) lines.push(`- ${alt.label}: ${statusLabel(alt.status)}. ${alt.status_reason}`);
  }
  lines.push("", "## Объяснение", "", ...analysis.explanation.map((text) => `- ${text}`));
  if (analysis.warnings.length) lines.push("", "## Предупреждения", "", ...analysis.warnings.map((text) => `- ${text}`));
  lines.push(
    "",
    "_Доля сценариев, в которых альтернатива первая, характеризует устойчивость выбора к весам и шуму; " +
      "это не вероятность того, что модель лучше._",
    "",
  );
  return lines.join("\n");
}

export function download(fileName: string, content: string, type: string): void {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}
