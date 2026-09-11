//#форматирование значений для таблицы и карточек

const NUMBER_FORMAT = new Intl.NumberFormat("ru-RU");

export function formatCount(value: number): string {
  return NUMBER_FORMAT.format(value);
}

export function formatBytes(bytes: number): string {
  //#единицы подписаны по-русски: это интерфейс на русском, а не лог
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let unit = 0;

  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }

  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

export function formatRatio(ratio: number): string {
  //#доли показываются процентами с одним знаком: «0.0483» читается хуже, чем «4,8%»
  return `${(ratio * 100).toFixed(1).replace(".", ",")}%`;
}

const MAX_CELL_LENGTH = 200;

export function formatCell(value: unknown): string {
  //#значение уже приведено backend к JSON-безопасному виду, здесь остаётся только показ
  if (value === null || value === undefined) {
    return "";
  }

  if (typeof value === "boolean") {
    return value ? "да" : "нет";
  }

  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : String(value);
  }

  const text = typeof value === "string" ? value : JSON.stringify(value);

  //#очень длинное значение обрезается для показа: полное остаётся в title ячейки,
  //#а рисовать мегабайт текста в строке высотой 28 пикселей бессмысленно
  return text.length > MAX_CELL_LENGTH ? `${text.slice(0, MAX_CELL_LENGTH)}…` : text;
}

export function formatRowCount(rowCount: number | null): string {
  //#null означает «точное число ещё не считалось», а не «строк нет»:
  //#показывать здесь ноль было бы прямой ложью
  return rowCount === null ? "не подсчитано" : formatCount(rowCount);
}

export function formatDuration(milliseconds: number): string {
  //#миллисекунды до секунды показываются как есть: «0,0 с» ничего не сообщает о 12 мс
  if (milliseconds < 1000) {
    return `${milliseconds} мс`;
  }

  return `${(milliseconds / 1000).toFixed(1).replace(".", ",")} с`;
}

//#согласование числительных: «9 Предупреждение» и «5 Сигнал» по-русски неверны,
//#а подпись под числом читают чаще, чем само число
export function plural(count: number, one: string, few: string, many: string): string {
  const hundred = Math.abs(count) % 100;

  if (hundred >= 11 && hundred <= 14) {
    return many;
  }

  const ten = Math.abs(count) % 10;

  if (ten === 1) {
    return one;
  }

  if (ten >= 2 && ten <= 4) {
    return few;
  }

  return many;
}
