import { describe, expect, it } from "vitest";
import { columnsLabel, rowsLabel } from "./format";

describe("подписи к числам", () => {
  it.each([
    [1, "1 строка"],
    [2, "2 строки"],
    [5, "5 строк"],
    [11, "11 строк"],
    [21, "21 строка"],
    [0, "0 строк"],
  ])("%i → %s", (count, expected) => {
    expect(rowsLabel(count)).toBe(expected);
  });

  it("не подсчитанное число строк не притворяется нулём", () => {
    //#null означает «точное число ещё не считалось», а не «строк нет»
    expect(rowsLabel(null)).toBe("не подсчитано строк");
  });

  it.each([
    [1, "1 колонка"],
    [2, "2 колонки"],
    [6, "6 колонок"],
  ])("%i → %s", (count, expected) => {
    expect(columnsLabel(count)).toBe(expected);
  });
});

//#существительное, приклеенное к числу прямо в разметке. Именно так и появились
//#«2 колонок» и «1 строк»: функция согласования в проекте была, но мимо неё писали
//#текст руками. Проверять только саму функцию бесполезно — ломается не она.
//
//#граница слова \b в JavaScript знает только латиницу и после кириллицы не срабатывает:
//#первая версия этой проверки была зелёной всегда. Вместо неё — явное «дальше не буква»
const GLUED_NOUN =
  /\}(?:\s*\{"\s*"\})?\s*(строка|строки|строк|колонка|колонки|колонок)(?![а-яё])/i;

const SOURCES = import.meta.glob("../**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("числительные в разметке", () => {
  it("ни одно существительное не приклеено к числу вручную", () => {
    const files = Object.entries(SOURCES).filter(([path]) => !path.endsWith(".test.tsx"));

    //#защита от зелёного по неправильной причине: если обход ничего не нашёл,
    //#проверка не выполнялась вовсе, и молчание означает не порядок, а пустоту
    expect(files.length).toBeGreaterThan(5);

    const offenders = files
      .filter(([, text]) => GLUED_NOUN.test(text))
      .map(([path, text]) => `${path}: ${text.match(GLUED_NOUN)?.[0].trim()}`);

    expect(offenders, "подпись обязана идти через rowsLabel/columnsLabel/plural").toEqual([]);
  });
});
