//#цвет закреплён за альтернативой по её позиции во входе: одна и та же модель одного цвета
//#в полосах устойчивости, на графике и в таблице
const PALETTE = ["#7fae96", "#c99a3e", "#7c9cc4", "#c8574a", "#a99cc0", "#b9b36a", "#5fa7a8", "#d08b6e", "#9a968a", "#c47fa0"];

export function colorFor(index: number): string {
  return PALETTE[index % PALETTE.length]!;
}
