import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, BarChart3, ChevronLeft, ChevronRight, Loader2, Search, Stethoscope, X } from "lucide-react";
import type { RowSource } from "../../api/endpoints";
import type { ColumnSchema, Dataset, SemanticType, SortDirection } from "../../types/dataset";
import { formatCell, formatCount, plural } from "../../utils/format";
import { ColumnStats } from "./ColumnStats";
import { VirtualRows } from "./VirtualRows";
import { EMPTY_QUERY, PAGE_SIZE, useTablePage } from "./useTablePage";
import type { TableQuery } from "./useTablePage";

const ROW_HEIGHT = 30;
const VIEWPORT_HEIGHT = 560;
const DEFAULT_COLUMN_WIDTH = 160;
//#колонка номера строки: узкая и не изменяемая по ширине
const ORDINAL_WIDTH = 72;

//#короткие общепринятые обозначения типов; у чисел различаются целые и дробные,
//#потому что от этого зависит, какие шаги конвейера к колонке применимы
const TYPE_LABEL: Record<SemanticType, string> = {
  id: "ID",
  numeric: "NUM",
  categorical: "CAT",
  datetime: "DATE",
  boolean: "BOOL",
  text: "STR",
};

function typeLabel(column: ColumnSchema): string {
  if (column.semantic_type === "numeric") {
    const physical = column.physical_type.toLowerCase();
    if (physical.startsWith("int") || physical.startsWith("uint")) return "INT";
    if (physical.startsWith("float") || physical.startsWith("decimal")) return "FLOAT";
  }
  return TYPE_LABEL[column.semantic_type];
}

export type DatasetTableProps = {
  dataset: Dataset;
  //#строки находки диагностики: сюда приходит идентификатор, а отбор делает сервер.
  //#Второй таблицы для диагностики нет — это та же самая
  finding?: { finding_id: string; title: string; affected_rows: number | null } | null;
  onBackToHealth?: () => void;
  onClearFinding?: () => void;
  //#какие строки показывать: исходные или результат конвейера. Экран один и тот же —
  //#сортировка, фильтры и поиск не должны существовать в двух реализациях
  source?: RowSource;
  //#типы колонок результата конвейера: шаг мог сменить тип, и значок исходника был бы неправдой
  resultTypes?: Record<string, string> | undefined;
};

//#значок по физическому типу — для колонок, которых нет в схеме исходника или чей тип сменён
function semanticOf(physical: string): SemanticType {
  if (/^(U?Int|Float|Decimal)/.test(physical)) {
    return "numeric";
  }

  if (/^(Date|Datetime|Time|Duration)/.test(physical)) {
    return "datetime";
  }

  if (physical === "Boolean") {
    return "boolean";
  }

  if (/^(Categorical|Enum)/.test(physical)) {
    return "categorical";
  }

  return "text";
}

export function DatasetTable({
  dataset,
  finding = null,
  onBackToHealth,
  onClearFinding,
  source = "dataset",
  resultTypes,
}: DatasetTableProps) {
  const [query, setQuery] = useState<TableQuery>({ ...EMPTY_QUERY, finding: finding?.finding_id ?? null });
  const [searchDraft, setSearchDraft] = useState("");
  const [widths, setWidths] = useState<Record<string, number>>({});
  //#колонка, по которой открыта панель показателей. Живёт здесь, а не в шапке:
  //#панель одна на таблицу, и вторая колонка её переключает, а не открывает вторую
  const [statsColumn, setStatsColumn] = useState<string | null>(null);

  const { page, isLoading, error, reload } = useTablePage(
    dataset.workspace_id,
    dataset.dataset_id,
    query,
    source,
  );

  //#смена находки сбрасывает страницу и прежние фильтры: иначе пользователь увидел бы
  //#строки новой находки, суженные условиями, которые он ставил для другой
  useEffect(() => {
    setQuery({ ...EMPTY_QUERY, finding: finding?.finding_id ?? null });
    setSearchDraft("");
  }, [finding?.finding_id]);

  //#колонки — те, что прислал сервер вместе со строками. Прежде шапка всегда строилась
  //#по схеме исходника: в результате конвейера удалённая колонка показывалась сплошными
  //#пропусками, а созданная или переименованная не показывалась вовсе
  const columns = useMemo<ColumnSchema[]>(() => {
    if (!page?.columns) {
      return source === "dataset" ? dataset.schema.columns : [];
    }

    const known = new Map(dataset.schema.columns.map((column) => [column.name, column]));

    return page.columns.map((name, position) => {
      const original = known.get(name);
      const physical = resultTypes?.[name];

      if (original && (source === "dataset" || physical === undefined || physical === original.physical_type)) {
        return original;
      }

      return {
        name,
        position,
        physical_type: physical ?? "",
        logical_type: physical ?? "",
        semantic_type: semanticOf(physical ?? ""),
        nullable: true,
      };
    });
  }, [page?.columns, dataset.schema.columns, source, resultTypes]);
  const ordinals = page?.row_ordinals ?? [];

  const toggleSort = useCallback((column: string) => {
    setQuery((previous) => {
      const existing = previous.sort.find((item) => item.column === column);
      const next: SortDirection | null =
        existing === undefined ? "asc" : existing.direction === "asc" ? "desc" : null;

      return {
        ...previous,
        offset: 0,
        sort: next === null ? [] : [{ column, direction: next }],
      };
    });
  }, []);

  const applySearch = useCallback(() => {
    setQuery((previous) => ({ ...previous, offset: 0, search: searchDraft }));
  }, [searchDraft]);

  const columnWidth = useCallback(
    (name: string) => widths[name] ?? DEFAULT_COLUMN_WIDTH,
    [widths],
  );

  const totalWidth = useMemo(
    () => columns.reduce((sum, column) => sum + columnWidth(column.name), 0),
    [columns, columnWidth],
  );

  const rows = page?.rows ?? [];

  const tableHeader = (
          <div className="table-header" style={{ width: totalWidth + ORDINAL_WIDTH }}>
      <div className="table-th table-th--ordinal" style={{ width: ORDINAL_WIDTH }}>
        <span className="table-th__name" title="Номер строки в файле">
          №
        </span>
      </div>
      {columns.map((column) => (
        <ColumnHeader
          key={column.name}
          column={column}
          width={columnWidth(column.name)}
          sort={query.sort.find((item) => item.column === column.name)?.direction}
          onSort={() => toggleSort(column.name)}
          onResize={(width) =>
            setWidths((previous) => ({ ...previous, [column.name]: width }))
          }
          statsOpen={statsColumn === column.name}
          //#показатели считаются по исходнику: для колонки результата они были бы
          //#показателями другой колонки или ответом «не найдено»
          onStats={
            source === "dataset"
              ? () => setStatsColumn((previous) => (previous === column.name ? null : column.name))
              : undefined
          }
        />
      ))}
    </div>
  );

  return (
    <div className="table-shell">
      {finding && (
        <div className="table-finding" role="status">
          <Stethoscope size={14} aria-hidden />
          <span className="table-finding__text">
            Показаны строки находки: <strong>{finding.title}</strong>
            {finding.affected_rows !== null &&
              ` — ${formatCount(finding.affected_rows)} ${plural(finding.affected_rows, "строка", "строки", "строк")}`}
          </span>

          {onBackToHealth && (
            <button type="button" className="button" onClick={onBackToHealth}>
              Вернуться к диагностике
            </button>
          )}

          {onClearFinding && (
            <button type="button" className="button" onClick={onClearFinding}>
              Показать весь датасет
            </button>
          )}
        </div>
      )}

      <div className="table-toolbar">
        <div className="table-search">
          <Search size={13} aria-hidden />
          <input
            type="search"
            value={searchDraft}
            placeholder="Поиск по всем колонкам"
            onChange={(event) => setSearchDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                applySearch();
              }
            }}
          />
          {query.search && (
            <button
              type="button"
              className="icon-button"
              title="Сбросить поиск"
              onClick={() => {
                setSearchDraft("");
                setQuery((previous) => ({ ...previous, offset: 0, search: "" }));
              }}
            >
              <X size={13} aria-hidden />
            </button>
          )}
        </div>

      </div>

      {error ? (
        <div className="banner banner--error" role="alert">
          <div>
            <p className="banner__title">Страница не загрузилась</p>
            <p className="banner__text">{error}</p>
            <button type="button" className="app-nav__item" onClick={reload}>
              Повторить
            </button>
          </div>
        </div>
      ) : (
        <div className="table-frame">
          {rows.length === 0 && !isLoading ? (
            <div className="table-empty-wrap">
              {tableHeader}
              <EmptyRows hasQuery={Boolean(query.search) || query.filters.length > 0} />
            </div>
          ) : (
            <VirtualRows
              header={tableHeader}
              contentWidth={totalWidth + ORDINAL_WIDTH}
              rowCount={rows.length}
              rowHeight={ROW_HEIGHT}
              height={VIEWPORT_HEIGHT}
              renderRow={(index) => (
                <div className="table-row" style={{ width: totalWidth + ORDINAL_WIDTH }}>
                  {/*#номер строки в файле, а не на странице: после сортировки и внутри
                     #найденных строк он остаётся тем же и отвечает «какая это строка»*/}
                  <span className="table-ordinal" style={{ width: ORDINAL_WIDTH }}>
                    {ordinals[index] === undefined ? "" : formatCount(ordinals[index] + 1)}
                  </span>
                  {columns.map((column) => (
                    <Cell
                      key={column.name}
                      value={rows[index]?.[column.name]}
                      width={columnWidth(column.name)}
                      semantic={column.semantic_type}
                    />
                  ))}
                </div>
              )}
            />
          )}
        </div>
      )}

      {statsColumn !== null && (
        <ColumnStats
          workspaceId={dataset.workspace_id}
          datasetId={dataset.dataset_id}
          column={statsColumn}
          onClose={() => setStatsColumn(null)}
        />
      )}

      <div className="table-footer">
        <Pagination
          offset={page?.offset ?? 0}
          limit={PAGE_SIZE}
          totalRows={page?.total_rows ?? null}
          isLoading={isLoading}
          onChange={(offset) => setQuery((previous) => ({ ...previous, offset }))}
        />
        <TableStatus isLoading={isLoading} totalRows={page?.total_rows ?? null} />
      </div>
    </div>
  );
}

function TableStatus({ isLoading, totalRows }: { isLoading: boolean; totalRows: number | null }) {
  if (isLoading) {
    //#честное состояние ожидания: сортировка миллиона строк занимает время,
    //#и притворяться, что данные уже здесь, нельзя
    return (
      <span className="table-status">
        <Loader2 size={13} className="spin" aria-hidden />
        Загружается…
      </span>
    );
  }

  if (totalRows === null) {
    return <span className="table-status">строк: неизвестно</span>;
  }

  return (
    <span className="table-status tabular">
      {formatCount(totalRows)} {plural(totalRows, "строка", "строки", "строк")}
    </span>
  );
}

function ColumnHeader({
  column,
  width,
  sort,
  onSort,
  onResize,
  statsOpen,
  onStats,
}: {
  column: ColumnSchema;
  width: number;
  sort: SortDirection | undefined;
  onSort: () => void;
  onResize: (width: number) => void;
  statsOpen: boolean;
  onStats?: (() => void) | undefined;
}) {
  return (
    <div className="table-th" style={{ width }}>
      <button
        type="button"
        className="table-th__button"
        onClick={onSort}
        title="Сортировать"
        aria-label={`Сортировать по колонке ${column.name}`}
      >
        <span className={`type-badge type-badge--${column.semantic_type}`}>
          {typeLabel(column)}
        </span>
        <span className="table-th__name">{column.name}</span>
        {sort === "asc" && <ArrowUp size={12} aria-hidden />}
        {sort === "desc" && <ArrowDown size={12} aria-hidden />}
      </button>
      {onStats && (
        <button
          type="button"
          className={`table-th__stats${statsOpen ? " table-th__stats--open" : ""}`}
          onClick={onStats}
          title="Показатели колонки"
          aria-label={`Показатели колонки ${column.name}`}
          aria-pressed={statsOpen}
        >
          <BarChart3 size={12} aria-hidden />
        </button>
      )}
      <div
        className="table-th__resize"
        role="separator"
        aria-orientation="vertical"
        onPointerDown={(event) => {
          event.preventDefault();
          const startX = event.clientX;
          const startWidth = width;
          const move = (moveEvent: PointerEvent) => {
            onResize(Math.max(60, startWidth + moveEvent.clientX - startX));
          };
          const stop = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", stop);
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", stop);
        }}
      />
    </div>
  );
}

function Cell({
  value,
  width,
  semantic,
}: {
  value: unknown;
  width: number;
  semantic: SemanticType;
}) {
  //#пропуск показывается явно и отличается от пустой строки: это разные вещи в данных
  if (value === null || value === undefined) {
    return (
      <div className="table-td table-td--null" style={{ width }}>
        null
      </div>
    );
  }

  const isNumeric = semantic === "numeric";

  return (
    <div
      className={`table-td${isNumeric ? " table-td--numeric tabular" : ""}`}
      style={{ width }}
      title={String(value)}
    >
      {formatCell(value)}
    </div>
  );
}

function EmptyRows({ hasQuery }: { hasQuery: boolean }) {
  return (
    <div className="table-empty">
      {hasQuery
        ? "Ни одна строка не подошла под условия. Попробуйте изменить поиск или фильтр."
        : "В датасете нет строк."}
    </div>
  );
}

//#номера страниц как на обычных сайтах: первая, последняя, соседние с текущей,
//#пропуски — многоточием. На миллионе строк это пять тысяч страниц, и все сразу не нужны
function pageItems(current: number, pages: number): Array<number | "gap"> {
  const wanted = new Set([1, pages, current - 1, current, current + 1]);
  if (current <= 3) [2, 3, 4].forEach((page) => wanted.add(page));
  if (current >= pages - 2) [pages - 1, pages - 2, pages - 3].forEach((page) => wanted.add(page));
  const sorted = [...wanted].filter((page) => page >= 1 && page <= pages).sort((a, b) => a - b);
  const items: Array<number | "gap"> = [];
  sorted.forEach((page, index) => {
    if (index > 0 && page - sorted[index - 1]! > 1) items.push("gap");
    items.push(page);
  });
  return items;
}

function Pagination({
  offset,
  limit,
  totalRows,
  isLoading,
  onChange,
}: {
  offset: number;
  limit: number;
  totalRows: number | null;
  isLoading: boolean;
  onChange: (offset: number) => void;
}) {
  const pages = totalRows === null ? null : Math.max(1, Math.ceil(totalRows / limit));
  const current = Math.floor(offset / limit) + 1;
  const hasPrevious = offset > 0;
  const hasNext = totalRows !== null && offset + limit < totalRows;

  //#одна страница — листать нечего, и навигация не показывается вовсе
  if (pages === 1 && !hasPrevious) {
    return null;
  }

  return (
    <nav className="pager" aria-label="Страницы">
      <button
        type="button"
        className="pager__item"
        aria-label="Предыдущая страница"
        disabled={!hasPrevious || isLoading}
        onClick={() => onChange(Math.max(0, offset - limit))}
      >
        <ChevronLeft size={16} aria-hidden />
      </button>
      {pages !== null &&
        pageItems(current, pages).map((item, index) =>
          item === "gap" ? (
            <span key={`gap-${index}`} className="pager__gap">
              …
            </span>
          ) : (
            <button
              key={item}
              type="button"
              className="pager__item tabular"
              aria-current={item === current ? "page" : undefined}
              disabled={isLoading}
              onClick={() => onChange((item - 1) * limit)}
            >
              {formatCount(item)}
            </button>
          ),
        )}
      <button
        type="button"
        className="pager__item"
        aria-label="Следующая страница"
        disabled={!hasNext || isLoading}
        onClick={() => onChange(offset + limit)}
      >
        <ChevronRight size={16} aria-hidden />
      </button>
    </nav>
  );
}
