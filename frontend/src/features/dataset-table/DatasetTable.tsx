import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Loader2, Search, Stethoscope, X } from "lucide-react";
import type { ColumnSchema, Dataset, SemanticType, SortDirection } from "../../types/dataset";
import { formatCell, formatCount, plural } from "../../utils/format";
import { VirtualRows } from "./VirtualRows";
import { EMPTY_QUERY, PAGE_SIZE, useTablePage } from "./useTablePage";
import type { TableQuery } from "./useTablePage";

const ROW_HEIGHT = 28;
const VIEWPORT_HEIGHT = 560;
const DEFAULT_COLUMN_WIDTH = 160;
//#колонка номера строки: узкая и не изменяемая по ширине
const ORDINAL_WIDTH = 72;

const TYPE_LABEL: Record<SemanticType, string> = {
  id: "ID",
  numeric: "ЧИС",
  categorical: "КАТ",
  datetime: "ДАТА",
  boolean: "ЛОГ",
  text: "ТЕКСТ",
};

export type DatasetTableProps = {
  dataset: Dataset;
  //#строки находки диагностики: сюда приходит идентификатор, а отбор делает сервер.
  //#Второй таблицы для диагностики нет — это та же самая
  finding?: { finding_id: string; title: string; affected_rows: number | null } | null;
  onBackToHealth?: () => void;
  onClearFinding?: () => void;
};

export function DatasetTable({
  dataset,
  finding = null,
  onBackToHealth,
  onClearFinding,
}: DatasetTableProps) {
  const [query, setQuery] = useState<TableQuery>({ ...EMPTY_QUERY, finding: finding?.finding_id ?? null });
  const [searchDraft, setSearchDraft] = useState("");
  const [widths, setWidths] = useState<Record<string, number>>({});

  const { page, isLoading, error, reload } = useTablePage(
    dataset.workspace_id,
    dataset.dataset_id,
    query,
  );

  //#смена находки сбрасывает страницу и прежние фильтры: иначе пользователь увидел бы
  //#строки новой находки, суженные условиями, которые он ставил для другой
  useEffect(() => {
    setQuery({ ...EMPTY_QUERY, finding: finding?.finding_id ?? null });
    setSearchDraft("");
  }, [finding?.finding_id]);

  const columns = dataset.schema.columns;
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

        <TableStatus
          isLoading={isLoading}
          rowsOnPage={rows.length}
          offset={page?.offset ?? 0}
          totalRows={page?.total_rows ?? null}
          totalIsExact={page?.total_is_exact ?? true}
        />
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
              />
            ))}
          </div>

          {rows.length === 0 && !isLoading ? (
            <EmptyRows hasQuery={Boolean(query.search) || query.filters.length > 0} />
          ) : (
            <VirtualRows
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

      <Pagination
        offset={page?.offset ?? 0}
        limit={PAGE_SIZE}
        totalRows={page?.total_rows ?? null}
        isLoading={isLoading}
        onChange={(offset) => setQuery((previous) => ({ ...previous, offset }))}
      />
    </div>
  );
}

function TableStatus({
  isLoading,
  rowsOnPage,
  offset,
  totalRows,
  totalIsExact,
}: {
  isLoading: boolean;
  rowsOnPage: number;
  offset: number;
  totalRows: number | null;
  totalIsExact: boolean;
}) {
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

  const from = rowsOnPage === 0 ? 0 : offset + 1;
  const to = offset + rowsOnPage;
  //#total_is_exact=false означает «строк больше»: показывать это как точное число было бы враньём
  const total = totalIsExact ? formatCount(totalRows) : `более ${formatCount(totalRows)}`;

  return (
    <span className="table-status tabular">
      {formatCount(from)}–{formatCount(to)} из {total}
    </span>
  );
}

function ColumnHeader({
  column,
  width,
  sort,
  onSort,
  onResize,
}: {
  column: ColumnSchema;
  width: number;
  sort: SortDirection | undefined;
  onSort: () => void;
  onResize: (width: number) => void;
}) {
  return (
    <div className="table-th" style={{ width }}>
      <button type="button" className="table-th__button" onClick={onSort} title="Сортировать">
        <span className={`type-badge type-badge--${column.semantic_type}`}>
          {TYPE_LABEL[column.semantic_type]}
        </span>
        <span className="table-th__name">{column.name}</span>
        {sort === "asc" && <ArrowUp size={12} aria-hidden />}
        {sort === "desc" && <ArrowDown size={12} aria-hidden />}
      </button>
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
  const hasPrevious = offset > 0;
  const hasNext = totalRows !== null && offset + limit < totalRows;

  return (
    <div className="table-pagination">
      <button
        type="button"
        className="app-nav__item"
        disabled={!hasPrevious || isLoading}
        onClick={() => onChange(Math.max(0, offset - limit))}
      >
        Назад
      </button>
      <button
        type="button"
        className="app-nav__item"
        disabled={!hasNext || isLoading}
        onClick={() => onChange(offset + limit)}
      >
        Вперёд
      </button>
    </div>
  );
}
