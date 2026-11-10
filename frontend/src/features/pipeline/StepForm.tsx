import { useMemo, useState } from "react";
import { Check, Eye, X } from "lucide-react";
import type { OperationVocabulary, StepImpact } from "../../types/dataset";
import { formatCount, plural } from "../../utils/format";

export type DraftStep = {
  step_id: string;
  kind: string;
  params: Record<string, unknown>;
};

export type StepFormProps = {
  vocabulary: OperationVocabulary;
  columns: string[];
  types: Record<string, string>;
  impact: StepImpact | null;
  onPreview: (draft: DraftStep) => void;
  onSubmit: (draft: DraftStep) => Promise<void> | void;
  onCancel: () => void;
};

//#какие поля нужны каждой операции. Список здесь, а не в разметке: иначе форма
//#«заполните параметры» существовала бы отдельно от того, что сервер на самом деле ждёт
type Field = "column" | "columns" | "to" | "value" | "operator" | "case" | "cast" | "from" | "mode";

const FIELDS: Record<string, Field[]> = {
  drop_columns: ["columns"],
  rename_column: ["column", "to"],
  reorder_columns: ["columns"],
  duplicate_column: ["column", "to"],
  cast_column: ["column", "cast"],
  filter_rows: ["column", "operator", "value"],
  drop_rows: ["column", "operator", "value"],
  sort_rows: ["column"],
  replace_values: ["column", "from", "value", "mode"],
  fill_missing: ["column", "value"],
  normalize_nulls: ["columns"],
  deduplicate: ["columns"],
  trim_whitespace: ["columns"],
  change_case: ["columns", "case"],
  parse_number: ["column"],
  parse_datetime: ["column"],
};

const OPERATORS: { value: string; label: string }[] = [
  { value: "eq", label: "равно" },
  { value: "ne", label: "не равно" },
  { value: "gt", label: "больше" },
  { value: "gte", label: "больше или равно" },
  { value: "lt", label: "меньше" },
  { value: "lte", label: "меньше или равно" },
  { value: "contains", label: "содержит" },
  { value: "is_null", label: "пропуск" },
  { value: "is_not_null", label: "не пропуск" },
];

export function StepForm({
  vocabulary,
  columns,
  types,
  impact,
  onPreview,
  onSubmit,
  onCancel,
}: StepFormProps) {
  const [kind, setKind] = useState(vocabulary.operations[0]?.kind ?? "drop_columns");
  const [column, setColumn] = useState(columns[0] ?? "");
  const [selected, setSelected] = useState<string[]>([]);
  const [target, setTarget] = useState("");
  const [value, setValue] = useState("");
  const [source, setSource] = useState("");
  const [operator, setOperator] = useState("eq");
  const [caseMode, setCaseMode] = useState(vocabulary.case_modes[0] ?? "lower");
  const [castTarget, setCastTarget] = useState(vocabulary.cast_targets[0] ?? "string");
  const [mode, setMode] = useState("exact");

  const fields = useMemo(() => FIELDS[kind] ?? [], [kind]);

  const draft = useMemo<DraftStep>(() => {
    const params: Record<string, unknown> = {};

    if (fields.includes("column")) params.column = column;
    if (fields.includes("columns")) {
      //#«убрать дубликаты» без колонок означает «по всем»: пустой список здесь
      //#не ошибка, а осмысленное значение
      params.columns = kind === "deduplicate" && selected.length === 0 ? undefined : selected;
      if (params.columns === undefined) delete params.columns;
      if (kind === "reorder_columns") {
        params.order = selected.length > 0 ? selected : columns;
        delete params.columns;
      }
    }
    if (fields.includes("to")) params.to = target;
    if (fields.includes("cast")) params.to = castTarget;
    if (fields.includes("value")) params.value = value;
    if (fields.includes("from")) params.from = source;
    if (fields.includes("operator")) params.operator = operator;
    if (fields.includes("case")) params.case = caseMode;
    if (fields.includes("mode")) params.mode = mode;

    return { step_id: `s${Date.now().toString(36)}`, kind, params };
  }, [
    fields,
    kind,
    column,
    selected,
    columns,
    target,
    castTarget,
    value,
    source,
    operator,
    caseMode,
    mode,
  ]);

  const needsValue = !["is_null", "is_not_null"].includes(operator);

  return (
    <form
      className="step-form"
      onSubmit={(event) => {
        event.preventDefault();
        void onSubmit(draft);
      }}
    >
      <label className="step-form__field">
        <span>Операция</span>
        <select value={kind} onChange={(event) => setKind(event.target.value)}>
          {vocabulary.operations.map((operation) => (
            <option key={operation.kind} value={operation.kind}>
              {operation.title}
            </option>
          ))}
        </select>
      </label>

      {fields.includes("column") && (
        <label className="step-form__field">
          <span>Колонка</span>
          <select value={column} onChange={(event) => setColumn(event.target.value)}>
            {columns.map((name) => (
              <option key={name} value={name}>
                {name}
                {types[name] ? ` · ${types[name]}` : ""}
              </option>
            ))}
          </select>
        </label>
      )}

      {fields.includes("columns") && (
        <fieldset className="step-form__field step-form__field--wide">
          <legend>
            Колонки
            {kind === "deduplicate" && " (пусто — по всем)"}
            {kind === "reorder_columns" && " (порядок выбора задаёт порядок колонок)"}
          </legend>
          <div className="step-form__columns">
            {columns.map((name) => (
              <label key={name} className="step-form__check">
                <input
                  type="checkbox"
                  checked={selected.includes(name)}
                  onChange={(event) =>
                    setSelected((previous) =>
                      event.target.checked
                        ? [...previous, name]
                        : previous.filter((item) => item !== name),
                    )
                  }
                />
                {name}
              </label>
            ))}
          </div>
        </fieldset>
      )}

      {fields.includes("operator") && (
        <label className="step-form__field">
          <span>Условие</span>
          <select value={operator} onChange={(event) => setOperator(event.target.value)}>
            {OPERATORS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      )}

      {fields.includes("from") && (
        <label className="step-form__field">
          <span>Что заменить</span>
          <input value={source} onChange={(event) => setSource(event.target.value)} />
        </label>
      )}

      {fields.includes("value") && needsValue && (
        <label className="step-form__field">
          <span>{kind === "replace_values" ? "Чем заменить" : "Значение"}</span>
          <input value={value} onChange={(event) => setValue(event.target.value)} />
        </label>
      )}

      {fields.includes("mode") && (
        <label className="step-form__field">
          <span>Совпадение</span>
          <select value={mode} onChange={(event) => setMode(event.target.value)}>
            <option value="exact">значение целиком</option>
            <option value="substring">часть значения</option>
          </select>
        </label>
      )}

      {fields.includes("to") && (
        <label className="step-form__field">
          <span>Новое имя</span>
          <input value={target} onChange={(event) => setTarget(event.target.value)} />
        </label>
      )}

      {fields.includes("cast") && (
        <label className="step-form__field">
          <span>Новый тип</span>
          <select value={castTarget} onChange={(event) => setCastTarget(event.target.value)}>
            {vocabulary.cast_targets.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}

      {fields.includes("case") && (
        <label className="step-form__field">
          <span>Регистр</span>
          <select value={caseMode} onChange={(event) => setCaseMode(event.target.value)}>
            {vocabulary.case_modes.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}

      {impact && (
        <p className="step-form__impact" role="status">
          {impact.rows_removed > 0
            ? `Удалит ${formatCount(impact.rows_removed)} ${plural(impact.rows_removed, "строку", "строки", "строк")} из ${formatCount(impact.rows_before)}.`
            : `Строк останется столько же: ${formatCount(impact.rows_after)}.`}
          {impact.values_lost > 0 &&
            ` Значений станет пропусками: ${formatCount(impact.values_lost)}.`}
          {impact.columns_after !== impact.columns_before &&
            ` Колонок было ${formatCount(impact.columns_before)}, станет ${formatCount(impact.columns_after)}.`}
        </p>
      )}

      <div className="step-form__actions">
        <button type="button" className="button" onClick={() => onPreview(draft)}>
          <Eye size={14} aria-hidden /> Посмотреть последствия
        </button>
        <button type="submit" className="button button--primary">
          <Check size={14} aria-hidden /> Добавить
        </button>
        <button type="button" className="button" onClick={onCancel}>
          <X size={14} aria-hidden /> Отмена
        </button>
      </div>
    </form>
  );
}
