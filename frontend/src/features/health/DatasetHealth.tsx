import { AlertOctagon, AlertTriangle, Info, Loader2, RefreshCw, Rows3 } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { formatCount, formatRatio, plural } from "../../utils/format";
import type { Dataset } from "../../types/dataset";
import type { Finding, Severity } from "../../types/health";
import { useDatasetHealth } from "./useDatasetHealth";

type SeverityMeta = {
  //#три формы, потому что подпись стоит под числом: «1 дефект», «2 дефекта», «5 дефектов»
  forms: [string, string, string];
  icon: LucideIcon;
  className: string;
};

const SEVERITY: Record<Severity, SeverityMeta> = {
  problem: { forms: ["Дефект", "Дефекта", "Дефектов"], icon: AlertOctagon, className: "finding--problem" },
  warning: {
    forms: ["Предупреждение", "Предупреждения", "Предупреждений"],
    icon: AlertTriangle,
    className: "finding--warning",
  },
  //#«сигнал», а не «замечание»: высокая кардинальность или выброс могут быть
  //#совершенно нормальными, и слово не должно подталкивать это «чинить»
  notice: { forms: ["Сигнал", "Сигнала", "Сигналов"], icon: Info, className: "finding--notice" },
};

export type DatasetHealthProps = {
  dataset: Dataset;
  onShowRows: (finding: Finding) => void;
};

export function DatasetHealth({ dataset, onShowRows }: DatasetHealthProps) {
  const { report, isLoading, error, reload } = useDatasetHealth(
    dataset.workspace_id,
    dataset.dataset_id,
  );

  if (isLoading) {
    //#честное состояние расчёта: полосы с процентами нет, потому что за ней
    //#не стояло бы ничего измеримого
    return (
      <div className="empty-state" role="status">
        <Loader2 className="spin" size={20} aria-hidden />
        <h2 className="empty-state__title">Считаем диагностику</h2>
        <p className="empty-state__text">
          Проверки идут по всему датасету на сервере. Прежний отчёт не показывается:
          он относился бы к другим данным.
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="banner banner--error" role="alert">
        <div>
          <p className="banner__title">Диагностика недоступна</p>
          <p className="banner__text">{error}</p>
        </div>
        <button type="button" className="button" onClick={reload}>
          <RefreshCw size={14} aria-hidden />
          Повторить
        </button>
      </div>
    );
  }

  if (!report) {
    return null;
  }

  if (report.findings.length === 0) {
    return (
      <div className="empty-state">
        <h2 className="empty-state__title">Проверки ничего не нашли</h2>
        <p className="empty-state__text">
          {report.total_rows === 0
            ? "В датасете нет строк, поэтому проверять нечего."
            : `Просмотрено ${formatCount(report.total_rows)} ${plural(report.total_rows, "строка", "строки", "строк")}: пропусков, дубликатов и других отклонений не обнаружено.`}
        </p>
      </div>
    );
  }

  return (
    <div className="health">
      <header className="health__summary">
        {(["problem", "warning", "notice"] as Severity[]).map((level) => {
          const meta = SEVERITY[level];
          const Icon = meta.icon;

          return (
            <div key={level} className={`health__count ${meta.className}`}>
              <Icon size={15} aria-hidden />
              <span className="health__count-value">{report.summary[level]}</span>
              <span className="health__count-label">
                {plural(report.summary[level], ...meta.forms)}
              </span>
            </div>
          );
        })}

        {/*#сводки «качество 87/100» здесь нет и не будет: одно число скрывает,
           #какой именно дефект важен*/}
        <span className="health__meta">
          {formatCount(report.total_rows)} {plural(report.total_rows, "строка", "строки", "строк")}{" "}
          · проверено{" "}
          {new Date(report.computed_at).toLocaleString("ru-RU")}
        </span>

        <button type="button" className="button" onClick={reload}>
          <RefreshCw size={14} aria-hidden />
          Пересчитать
        </button>
      </header>

      <ul className="health__list">
        {report.findings.map((finding) => (
          <FindingCard key={finding.finding_id} finding={finding} onShowRows={onShowRows} />
        ))}
      </ul>
    </div>
  );
}

function FindingCard({
  finding,
  onShowRows,
}: {
  finding: Finding;
  onShowRows: (finding: Finding) => void;
}) {
  const meta = SEVERITY[finding.severity];
  const Icon = meta.icon;

  return (
    <li className={`finding ${meta.className}`}>
      <div className="finding__head">
        <Icon size={15} aria-hidden />
        <h3 className="finding__title">{finding.title}</h3>
        <span className="finding__code">{finding.code}</span>
      </div>

      <div className="finding__facts">
        {finding.columns.length > 0 && (
          <span className="finding__column">{finding.columns.join(", ")}</span>
        )}

        {finding.affected_rows !== null && (
          <span>
            {formatCount(finding.affected_rows)}{" "}
            {plural(finding.affected_rows, "строка", "строки", "строк")}
            {finding.affected_ratio !== null && ` · ${formatRatio(finding.affected_ratio)}`}
          </span>
        )}

        {/*#точность подписана словом, а не значком: выборочное число, принятое
           #за точное, приводит к неверному решению по данным*/}
        {finding.exactness === "sampled" ? (
          <span className="finding__exactness finding__exactness--sampled">
            оценка по выборке
            {finding.sampled_rows !== null && ` из ${formatCount(finding.sampled_rows)} строк`}
          </span>
        ) : (
          <span className="finding__exactness">точный подсчёт</span>
        )}
      </div>

      <p className="finding__explanation">{finding.explanation}</p>

      {finding.evidence.values.length > 0 && (
        <p className="finding__evidence">
          Например: {finding.evidence.values.map((value) => String(value)).join(", ")}
        </p>
      )}

      <div className="finding__actions">
        {/*#кнопка появляется только когда есть осмысленное подмножество строк.
           #У находки на весь датасет её нет: «показать все строки» — это просто
           #открыть датасет*/}
        {finding.has_affected_rows && (
          <button
            type="button"
            className="button button--primary"
            onClick={() => onShowRows(finding)}
          >
            <Rows3 size={14} aria-hidden />
            Показать строки
          </button>
        )}

        {finding.suggested_action && (
          <span className="finding__suggestion">{finding.suggested_action.hint}</span>
        )}
      </div>
    </li>
  );
}
