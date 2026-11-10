import { useRef, useState } from "react";
import { Check, X } from "lucide-react";

//#функции формулы — тот же список, что разрешает сервер (backend/services/pipeline/formula.py)
const FUNCTIONS: Array<{ name: string; template: string; hint: string }> = [
  { name: "round", template: "round(, 2)", hint: "округлить" },
  { name: "iif", template: "iif(, , )", hint: "если — то — иначе" },
  { name: "coalesce", template: "coalesce(, 0)", hint: "первое непустое" },
  { name: "abs", template: "abs()", hint: "модуль" },
  { name: "min", template: "min(, )", hint: "меньшее" },
  { name: "max", template: "max(, )", hint: "большее" },
  { name: "concat", template: "concat(, ' ', )", hint: "склеить текст" },
  { name: "upper", template: "upper()", hint: "ВЕРХНИЙ регистр" },
  { name: "lower", template: "lower()", hint: "нижний регистр" },
  { name: "strip", template: "strip()", hint: "убрать пробелы" },
  { name: "len", template: "len()", hint: "длина текста" },
  { name: "year", template: "year()", hint: "год даты" },
  { name: "month", template: "month()", hint: "месяц даты" },
  { name: "num", template: "num()", hint: "в число" },
];

export type FormulaFormProps = {
  columns: string[];
  busy: boolean;
  onSubmit: (name: string, expression: string) => Promise<boolean>;
  onCancel: () => void;
};

export function FormulaForm({ columns, busy, onSubmit, onCancel }: FormulaFormProps) {
  const [name, setName] = useState("");
  const [expression, setExpression] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);

  //#вставка в позицию курсора: колонка — в квадратных скобках, если в имени есть пробел или знак
  const insert = (text: string) => {
    const element = input.current;
    const start = element?.selectionStart ?? expression.length;
    const end = element?.selectionEnd ?? expression.length;
    const next = expression.slice(0, start) + text + expression.slice(end);
    setExpression(next);
    requestAnimationFrame(() => {
      element?.focus();
      const caret = start + (text.includes("(") ? text.indexOf("(") + 1 : text.length);
      element?.setSelectionRange(caret, caret);
    });
  };

  const reference = (column: string) => (/^[\p{L}_][\p{L}\p{N}_]*$/u.test(column) ? column : `[${column}]`);

  return (
    <form
      className="formula"
      onSubmit={(event) => {
        event.preventDefault();
        void onSubmit(name.trim(), expression.trim()).then((ok) => {
          if (ok) {
            setName("");
            setExpression("");
          }
        });
      }}
    >
      <div className="formula__row">
        <label className="formula__field">
          <span>Название новой колонки</span>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="total" required />
        </label>
        <label className="formula__field formula__field--wide">
          <span>Формула</span>
          <textarea
            ref={input}
            className="formula__input"
            value={expression}
            onChange={(e) => setExpression(e.target.value)}
            placeholder="price * qty   или   iif([Price ($)] > 1000, 'дорого', 'обычно')"
            rows={2}
            required
          />
        </label>
      </div>

      <div className="formula__chips" aria-label="Колонки">
        {columns.map((column) => (
          <button key={column} type="button" className="formula-chip" onClick={() => insert(reference(column))}>
            {column}
          </button>
        ))}
      </div>
      <div className="formula__chips" aria-label="Функции">
        {FUNCTIONS.map((item) => (
          <button
            key={item.name}
            type="button"
            className="formula-chip formula-chip--fn"
            title={item.hint}
            onClick={() => insert(item.template)}
          >
            {item.name}
          </button>
        ))}
      </div>

      <div className="formula__actions">
        <button type="submit" className="button button--primary" disabled={busy || !name.trim() || !expression.trim()}>
          <Check size={14} aria-hidden /> Добавить колонку
        </button>
        <button type="button" className="button button--ghost" onClick={onCancel}>
          <X size={14} aria-hidden /> Отмена
        </button>
        <span className="repair__hint">Арифметика + − × ÷, сравнения, and / or, функции выше. Код не выполняется.</span>
      </div>
    </form>
  );
}
