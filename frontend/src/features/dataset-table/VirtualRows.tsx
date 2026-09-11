import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

//#собственная виртуализация вместо библиотеки
//
//#задача здесь ровно одна: не рисовать невидимые строки. Готовая библиотека принесла бы
//#30–60 кБ и собственную модель колонок, с которой пришлось бы воевать при resize и reorder.
//#Граница решения названа заранее: если своя реализация не даст плавный скролл на миллионе
//#строк, она заменяется на @tanstack/react-virtual, и это записывается в README как решение.

const OVERSCAN = 8;

export type VirtualRowsProps = {
  rowCount: number;
  rowHeight: number;
  height: number;
  renderRow: (index: number) => ReactNode;
  onScrollNearEnd?: () => void;
};

export function VirtualRows({
  rowCount,
  rowHeight,
  height,
  renderRow,
  onScrollNearEnd,
}: VirtualRowsProps) {
  const viewport = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  useEffect(() => {
    //#при смене датасета или запроса прокрутка возвращается наверх: иначе пользователь
    //#видит пустое место там, где в новых данных строк уже нет
    //
    //#присваивание scrollTop вместо scrollTo: последнего нет в jsdom и в части встроенных
    //#браузеров, и вызов молча ронял бы весь компонент
    if (viewport.current) {
      viewport.current.scrollTop = 0;
    }

    setScrollTop(0);
  }, [rowCount]);

  const firstVisible = Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN);
  const visibleCount = Math.ceil(height / rowHeight) + OVERSCAN * 2;
  const lastVisible = Math.min(rowCount, firstVisible + visibleCount);

  const rows: ReactNode[] = [];

  for (let index = firstVisible; index < lastVisible; index += 1) {
    rows.push(
      <div
        key={index}
        className="vrow"
        style={{ transform: `translateY(${index * rowHeight}px)`, height: rowHeight }}
      >
        {renderRow(index)}
      </div>,
    );
  }

  return (
    <div
      ref={viewport}
      className="vviewport"
      style={{ height }}
      onScroll={(event) => {
        const element = event.currentTarget;
        setScrollTop(element.scrollTop);

        //#подгрузка следующей страницы начинается заранее, а не в самом низу:
        //#иначе пользователь упирается в конец и ждёт
        const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;

        if (remaining < rowHeight * 10) {
          onScrollNearEnd?.();
        }
      }}
    >
      <div className="vspacer" style={{ height: rowCount * rowHeight }}>
        {rows}
      </div>
    </div>
  );
}
